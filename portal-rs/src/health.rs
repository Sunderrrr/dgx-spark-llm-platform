//! Health of the served model, parity with dgx-portal/vllm_health.py
//! `vllm_health` (the /api/modelhealth route and the home's `modelhealth`).
//!
//! Three readings keep state between probes, per process — exactly as each
//! gunicorn worker does on the Flask side, so they never match across
//! backends: the instantaneous throughput (previous counter + time), and the
//! age of the engine's tasks (first time a task id was seen).
//! The cross-relaunch token total is computed READ-ONLY here (see
//! Database::generated_tokens_total): the removed Flask backend was its only writer.
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use sqlx::PgPool;

use crate::db::Database;
use crate::ranking::round_to;
use crate::models;

/// Sum of a Prometheus metric's samples (exact name, labels ignored), or None
/// when the metric is absent. Pure.
pub fn prom_sum(text: &str, metric: &str) -> Option<f64> {
    let mut total = 0.0;
    let mut found = false;
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix(metric) {
            if rest.starts_with(' ') || rest.starts_with('{') {
                if let Some(v) = line.rsplit(' ').next().and_then(|v| v.parse::<f64>().ok()) {
                    total += v;
                    found = true;
                }
            }
        }
    }
    found.then_some(total)
}

/// `--flag N` in an argument string. Pure.
pub fn arg_int(args: &str, flag: &str) -> Option<i64> {
    let words: Vec<&str> = args.split_whitespace().collect();
    let key = format!("--{flag}");
    words.windows(2).find(|w| w[0] == key).and_then(|w| {
        // The regex `--flag\s+(\d+)` takes the leading digits of the value.
        let digits: String = w[1].chars().take_while(char::is_ascii_digit).collect();
        digits.parse().ok()
    })
}

fn ctx_flag(engine: &str) -> &'static str {
    match engine {
        "llamacpp" => "ctx-size",
        "ds4" => "ctx",
        "exllamav3" => "max-seq-len",
        _ => "max-model-len",
    }
}

/// Configured concurrent sessions (vllm_health.max_seqs_of). Pure.
pub fn max_seqs_of(args: &str, engine: &str) -> Option<i64> {
    if engine == "ds4" {
        return Some(1);
    }
    let flag = match engine {
        "llamacpp" => "parallel",
        "exllamav3" => "max-batch-size",
        _ => "max-num-seqs",
    };
    match arg_int(args, flag) {
        // llama.cpp serves 4 slots when --parallel is absent.
        None if engine == "llamacpp" => Some(4),
        n => n,
    }
}

/// Real usable context PER REQUEST (vllm_health.effective_ctx). Pure.
pub fn effective_ctx(args: &str, engine: &str) -> Option<i64> {
    if engine == "llamacpp" {
        if let Some(per_slot) = arg_int(args, "kv-unified-per-slot").filter(|n| *n > 0) {
            return Some(per_slot);
        }
    }
    let ctx = arg_int(args, ctx_flag(engine))?;
    if engine == "llamacpp" {
        let words: Vec<&str> = args.split_whitespace().collect();
        if words.contains(&"--kv-unified") && !words.contains(&"--no-kv-unified") {
            return Some(ctx);
        }
        let by = arg_int(args, "parallel").filter(|n| *n != 0).unwrap_or(1);
        return Some(ctx.div_euclid(by));
    }
    Some(ctx)
}

/// (input, output) split advertised to clients (vllm_health.ctx_split). Pure.
pub fn ctx_split(args: &str, engine: &str) -> (i64, i64) {
    let slot = effective_ctx(args, engine).filter(|n| *n != 0).unwrap_or(32768);
    if matches!(engine, "llamacpp" | "ds4" | "exllamav3") {
        let out_reserve = match arg_int(args, "n-predict") {
            Some(n) if n > 0 && n < slot => n,
            _ => 65536.min(slot.div_euclid(3)),
        };
        return ((slot - out_reserve).max(1024), out_reserve);
    }
    (slot, (slot.div_euclid(2)).min(262144))
}

/// The engine's base URL (VLLM_API_URL without its /v1).
fn engine_base() -> String {
    let api = models::engine_api();
    match api.rfind("/v1") {
        Some(i) => api[..i].to_string(),
        None => api,
    }
}

fn client(timeout: u64) -> Option<reqwest::Client> {
    reqwest::Client::builder().timeout(Duration::from_secs(timeout)).build().ok()
}

fn now_s() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

static SLOT_TASKS: Mutex<Option<HashMap<String, f64>>> = Mutex::new(None);

/// vllm_health._slots_activity: what the engine does right now, session by
/// session; None when it publishes no /slots (vLLM) or does not answer.
async fn slots_activity() -> Option<Value> {
    let slots: Value = client(4)?.get(format!("{}/slots", engine_base())).send().await.ok()?.json().await.ok()?;
    let slots = slots.as_array()?;
    let now = now_s();
    let actives: Vec<&Value> = slots.iter().filter(|s| s.get("is_processing").and_then(Value::as_bool).unwrap_or(false)).collect();
    let mut guard = SLOT_TASKS.lock().unwrap_or_else(|e| e.into_inner());
    let tasks = guard.get_or_insert_with(HashMap::new);
    let mut seen = Vec::new();
    let (mut oldest, mut ingested, mut processed): (Option<f64>, i64, i64) = (None, 0, 0);
    for s in &actives {
        let Some(t) = s.get("id_task").filter(|t| !t.is_null()) else { continue };
        let key = t.to_string();
        let start = *tasks.entry(key.clone()).or_insert(now);
        seen.push(key);
        let age = now - start;
        oldest = Some(oldest.map_or(age, |o: f64| o.max(age)));
        ingested += s.get("n_prompt_tokens").and_then(Value::as_i64).unwrap_or(0);
        processed += s.get("n_prompt_tokens_processed").and_then(Value::as_i64).unwrap_or(0);
    }
    // A task no longer processed leaves the tracking (it would grow forever).
    tasks.retain(|k, _| seen.contains(k));
    Some(json!({
        "busy": actives.len(),
        "total": slots.len(),
        "oldest_s": oldest.map(|o| o.round() as i64),
        "prompt_total": ingested,
        "prompt_processed": processed,
    }))
}

/// Throughput state: (time, previous counter) per engine family.
static LLAMA_TPS: Mutex<(f64, Option<f64>)> = Mutex::new((0.0, None));
static VLLM_TPS: Mutex<(f64, f64)> = Mutex::new((0.0, 0.0));
static CACHE: Mutex<Option<(Instant, Value)>> = Mutex::new(None);

async fn measured_ttft(db: &Database) -> Option<f64> {
    let v = db.get_setting_set("ttft_ms_ewma").await?;
    v.parse::<f64>().ok().map(|ms| round_to(ms / 1000.0, 2))
}

async fn prefill_last(db: &Database) -> Option<f64> {
    let v: f64 = db.get_setting("prefill_rate_last").await.ok()??.parse().ok()?;
    let at: f64 = db.get_setting("prefill_rate_at").await.ok()??.parse().ok()?;
    (now_s() - at < 120.0).then(|| round_to(v, 1))
}

/// stats.flow_from_spendlogs: ratio-of-sums figures for an engine without /metrics.
async fn flow_from_spendlogs(pool: Option<&PgPool>) -> Option<(i64, i64, i64, Option<f64>)> {
    let pool = pool?;
    let (requests, generated, entry, seconds): (i64, Option<i64>, Option<i64>, Option<f64>) = sqlx::query_as(
        "SELECT COUNT(*), SUM(COALESCE(completion_tokens, 0))::bigint, SUM(COALESCE(prompt_tokens, 0))::bigint, \
         COALESCE(SUM(EXTRACT(EPOCH FROM (\"endTime\" - \"startTime\"))), 0)::float8 \
         FROM \"LiteLLM_SpendLogs\" WHERE \"endTime\" IS NOT NULL",
    )
    .fetch_one(pool)
    .await
    .ok()?;
    let (generated, seconds) = (generated.unwrap_or(0), seconds.unwrap_or(0.0));
    let tps_avg = (seconds >= 300.0).then(|| round_to(generated as f64 / seconds, 1));
    Some((requests, generated, entry.unwrap_or(0), tps_avg))
}

/// _health_without_metrics: TabbyAPI publishes no /metrics.
async fn health_without_metrics(db: &Database, pool: Option<&PgPool>, model: &str, engine: &str, args: &str) -> Value {
    let flow = flow_from_spendlogs(pool).await;
    let (max_seqs, (ctx_in, ctx_out)) = (max_seqs_of(args, engine), ctx_split(args, engine));
    let cumulative = match flow {
        Some((_, generated, _, _)) => db.generated_tokens_total(model, generated).await,
        None => None,
    };
    json!({
        "up": true, "model": model, "engine": engine, "metrics": false,
        "running": null, "waiting": null,
        "max_seqs": max_seqs, "ctx_in": ctx_in, "ctx_out": ctx_out,
        "tps": db.live_decode_rate().await,
        "ttft": measured_ttft(db).await,
        "requests": flow.map(|f| f.0),
        "tokens_generated": null,
        "tokens_generated_total": cumulative,
        "tps_avg": flow.and_then(|f| f.3),
        "tokens_prompt": flow.map(|f| f.2),
        "tps_prefill": prefill_last(db).await,
        "slots": slots_activity().await,
    })
}

/// vllm_health(): cached 1 s, a single /metrics scrape even with several polls.
pub async fn vllm_health(db: &Database, pool: Option<&PgPool>) -> Value {
    if let Some((at, v)) = CACHE.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
        if at.elapsed() < Duration::from_secs(1) {
            return v.clone();
        }
    }
    let out = uncached(db, pool).await;
    *CACHE.lock().unwrap_or_else(|e| e.into_inner()) = Some((Instant::now(), out.clone()));
    out
}

async fn uncached(db: &Database, pool: Option<&PgPool>) -> Value {
    let running = models::running_models().await;
    let Some(model) = running.first().cloned() else {
        return json!({"up": false, "model": null});
    };
    let (engine, args) = match db.model_config(&model).await {
        Ok(Some((engine, args))) => (
            engine.filter(|e| !e.is_empty()).unwrap_or_else(|| "vllm".to_string()),
            args.unwrap_or_default(),
        ),
        _ => ("vllm".to_string(), String::new()),
    };
    let engine = engine.as_str();
    let text = match client(4) {
        Some(c) => match c.get(format!("{}/metrics", engine_base())).send().await {
            // TabbyAPI answers 404 with a body: the « no /metrics » case.
            Ok(r) if r.status().as_u16() < 400 => r.text().await.ok(),
            _ => None,
        },
        None => None,
    };
    let Some(text) = text else {
        return health_without_metrics(db, pool, &model, engine, &args).await;
    };

    let llama = engine == "llamacpp";
    let (gen_m, running_m, waiting_m) = if llama {
        ("llamacpp:tokens_predicted_total", "llamacpp:requests_processing", "llamacpp:requests_deferred")
    } else {
        ("vllm:generation_tokens_total", "vllm:num_requests_running", "vllm:num_requests_waiting")
    };
    let gen = prom_sum(&text, gen_m).unwrap_or(0.0);
    let now = now_s();
    let running_now = prom_sum(&text, running_m).unwrap_or(0.0) as i64;
    let tps: Option<f64> = if llama {
        // n_decode_total is the only counter advancing DURING generation; one
        // step yields one token per active slot, hence × requests_processing.
        let dec = prom_sum(&text, "llamacpp:n_decode_total").unwrap_or(0.0);
        let mut st = LLAMA_TPS.lock().unwrap_or_else(|e| e.into_inner());
        let tps = match st.1 {
            Some(p_dec) if now > st.0 && dec >= p_dec => {
                let step_rate = (dec - p_dec) / (now - st.0);
                if step_rate > 0.0 { round_to(step_rate * running_now.max(1) as f64, 1) } else { 0.0 }
            }
            _ => 0.0,
        };
        *st = (now, Some(dec));
        Some(tps)
    } else {
        let st = VLLM_TPS.lock().unwrap_or_else(|e| e.into_inner());
        (st.0 != 0.0 && now > st.0 && gen >= st.1).then(|| round_to((gen - st.1) / (now - st.0), 1))
    };
    *VLLM_TPS.lock().unwrap_or_else(|e| e.into_inner()) = (now, gen);

    // vLLM has its own TTFT histogram; llama.cpp has none, the relay's measure stands in.
    let ttft = if llama {
        measured_ttft(db).await
    } else {
        let sum = prom_sum(&text, "vllm:time_to_first_token_seconds_sum").unwrap_or(0.0);
        let cnt = prom_sum(&text, "vllm:time_to_first_token_seconds_count").unwrap_or(0.0);
        (cnt != 0.0).then(|| round_to(sum / cnt, 2))
    };
    let max_seqs = max_seqs_of(&args, engine);
    let (ctx_in, ctx_out) = ctx_split(&args, engine);

    let generated = if gen != 0.0 { gen as i64 } else { 0 };
    let (tps_avg, entry, tps_prefill) = if llama {
        let seconds = prom_sum(&text, "llamacpp:tokens_predicted_seconds_total").unwrap_or(0.0);
        let prefill = prom_sum(&text, "llamacpp:prompt_tokens_seconds");
        (
            // Under 300 s of cumulative generation an average means nothing.
            (seconds >= 300.0).then(|| round_to(gen / seconds, 1)),
            prom_sum(&text, "llamacpp:prompt_tokens_total").map(|v| v as i64),
            prefill.filter(|p| *p != 0.0).map(|p| round_to(p, 1)),
        )
    } else {
        (None, prom_sum(&text, "vllm:prompt_tokens_total").map(|v| v as i64), None)
    };
    let cumulative = db.generated_tokens_total(&model, generated).await;
    let slots = match slots_activity().await {
        Some(s) => s,
        // vLLM publishes no /slots: the counters still say how many work.
        None => match max_seqs {
            Some(total) => json!({"busy": running_now, "total": total, "oldest_s": null, "prompt_total": 0, "prompt_processed": 0}),
            None => Value::Null,
        },
    };
    json!({
        "up": true, "model": model, "engine": engine, "metrics": true,
        "running": running_now,
        "waiting": prom_sum(&text, waiting_m).unwrap_or(0.0) as i64,
        "max_seqs": max_seqs, "ctx_in": ctx_in, "ctx_out": ctx_out,
        "tps": tps,
        "ttft": ttft,
        // llama.cpp has no request counter (n_decode_total counts tokens).
        "requests": if llama { Value::Null } else { json!(prom_sum(&text, "vllm:request_success_total").unwrap_or(0.0) as i64) },
        "tokens_generated": generated,
        "tokens_generated_total": cumulative,
        "tps_avg": tps_avg,
        "tokens_prompt": entry,
        "tps_prefill": tps_prefill,
        "slots": slots,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prom_sum_adds_labelled_samples_and_ignores_lookalikes() {
        let text = "# HELP x\nvllm:gen_total{a=\"1\"} 2\nvllm:gen_total{a=\"2\"} 3.5\nvllm:gen_total_other 9\n";
        assert_eq!(prom_sum(text, "vllm:gen_total"), Some(5.5));
        assert_eq!(prom_sum(text, "vllm:absent"), None);
    }

    #[test]
    fn llamacpp_defaults_to_four_slots_and_ds4_to_one() {
        assert_eq!(max_seqs_of("--ctx-size 8192", "llamacpp"), Some(4));
        assert_eq!(max_seqs_of("", "ds4"), Some(1));
        assert_eq!(max_seqs_of("--max-num-seqs 8", "vllm"), Some(8));
        assert_eq!(max_seqs_of("", "vllm"), None);
    }

    #[test]
    fn effective_ctx_rules() {
        // Declared per-slot window wins.
        assert_eq!(effective_ctx("--ctx-size 1048576 --parallel 4 --kv-unified --kv-unified-per-slot 262144", "llamacpp"), Some(262144));
        // Unified pool: every slot addresses the whole context.
        assert_eq!(effective_ctx("--ctx-size 524288 --parallel 6 --kv-unified", "llamacpp"), Some(524288));
        // Partitioned: split among the slots.
        assert_eq!(effective_ctx("--ctx-size 1048576 --parallel 4", "llamacpp"), Some(262144));
        assert_eq!(effective_ctx("--max-model-len 32768", "vllm"), Some(32768));
        assert_eq!(effective_ctx("", "vllm"), None);
    }

    #[test]
    fn ctx_split_matches_the_documented_contract() {
        // CLAUDE.md: 4 private 256k slots with --n-predict 65536 → 196608 / 65536.
        assert_eq!(ctx_split("--ctx-size 1048576 --parallel 4 --n-predict 65536", "llamacpp"), (196608, 65536));
        // vLLM separates input/output itself.
        assert_eq!(ctx_split("--max-model-len 262144", "vllm"), (262144, 131072));
        // Nothing declared: cautious 32k.
        assert_eq!(ctx_split("", "vllm"), (32768, 16384));
    }
}
