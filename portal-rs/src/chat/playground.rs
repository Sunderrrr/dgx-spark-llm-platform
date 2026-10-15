//! POST /playground/chat (chat_routes.py `playground_chat`): the playground's
//! turn, streamed. The request is prepared (messages bounded, images kept for
//! a model that sees, window fitted, memory and clock in the system prompt),
//! the tool phase runs when a tool is explicitly asked for, then the model's
//! stream is relayed as is while the relay feeds the live gauges. An SSE
//! comment goes out BEFORE any work: the proxy cuts a silent connection.
use std::time::{Duration, Instant};

use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::{json, Map, Value};

use crate::chat::history::{self, Msg};
use crate::chat::{data_frame, gauges, memory_turn, phase, reasoning, tools, triggers, Out};
use crate::AppState;

const DEFAULT_REASONING_EFFORT: &str = "medium";
const SYSTEM_MAX: usize = 16000;
const READ_TIMEOUT: Duration = Duration::from_secs(300);

/// `_sse_notice`: a structured notice the frontend translates; `done` ends the stream.
pub fn notice(id: &str, args: Value, done: bool) -> String {
    let mut inner = json!({"id": id});
    if let (Some(o), Some(a)) = (inner.as_object_mut(), args.as_object()) {
        o.extend(a.clone());
    }
    let payload = crate::handlers::py_dumps_ascii(&json!({"dgx_notice": inner}));
    if done { format!("data: {payload}\n\ndata: [DONE]\n\n") } else { format!("data: {payload}\n\n") }
}

fn sse(body: String) -> Response {
    (StatusCode::OK, [(axum::http::header::CONTENT_TYPE, "text/event-stream; charset=utf-8")], body).into_response()
}

/// Python's `min(max(cast(v), lo), hi)` with the default on a bad value.
fn num_f(v: Option<&Value>, lo: f64, hi: f64, default: f64) -> f64 {
    let f = match v {
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::Bool(b)) => Some(f64::from(u8::from(*b))),
        Some(Value::String(s)) => crate::memory::py_strip(s).parse::<f64>().ok(),
        _ => None,
    };
    match f.filter(|x| x.is_finite()) {
        Some(x) => x.max(lo).min(hi),
        None => default,
    }
}

fn num_i(v: Option<&Value>, lo: i64, hi: i64, default: i64) -> i64 {
    match v.and_then(crate::handlers::py_int) {
        Some(x) => x.max(lo).min(hi),
        None => default,
    }
}

/// `_playground_input_limit`: what a PROMPT can weigh for this model (None = unknown).
async fn input_limit(state: &AppState, model: &str, running: &[String]) -> Option<i64> {
    let auto = std::env::var("AUTO_MODEL_NAME").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "auto-model".to_string());
    let q = "SELECT vllm_args, engine FROM model_configs WHERE name=?";
    let mut row: Option<(Option<String>, Option<String>)> = sqlx::query_as(q).bind(model).fetch_optional(state.db.pool()).await.ok().flatten();
    if row.is_none() {
        if model != auto || running.is_empty() {
            return None;
        }
        row = sqlx::query_as(q).bind(&running[0]).fetch_optional(state.db.pool()).await.ok().flatten();
    }
    let (args, engine) = row?;
    let entry = crate::health::ctx_split(args.as_deref().unwrap_or(""), engine.as_deref().filter(|e| !e.is_empty()).unwrap_or("vllm")).0;
    (entry != 0).then_some(entry)
}

/// `websearch_active`: not switched off by the user, and both services answer.
async fn websearch_active(state: &AppState, username: &str) -> bool {
    let off: Option<Option<i64>> = sqlx::query_scalar("SELECT websearch_enabled FROM user_prefs WHERE username=?").bind(username).fetch_optional(state.db.pool()).await.ok().flatten();
    if matches!(off, Some(Some(0))) {
        return false;
    }
    tools::websearch::available().await
}

fn free_gib() -> Value {
    match crate::asr_ondemand::mem_available_gb() {
        Some(g) => json!(format!("{g:.1}").parse::<f64>().unwrap_or(g)),
        None => Value::Null,
    }
}

/// `invented_images`: the generated-image addresses an answer cites that serve nothing.
async fn invented_images(state: &AppState, text: &str, username: &str) -> usize {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"/image/file/([0-9a-f]+)(?:/(\d+))?").expect("static regex"));
    let mut seen = std::collections::HashSet::new();
    let mut ghosts = 0;
    for c in re.captures_iter(text) {
        let pid = c[1].to_string();
        let idx: i64 = c.get(2).and_then(|m| m.as_str().parse().ok()).unwrap_or(0);
        if !seen.insert((pid.clone(), idx)) {
            continue;
        }
        let owned = sqlx::query_scalar::<_, i64>("SELECT 1 FROM image_jobs WHERE prompt_id=? AND username=?").bind(&pid).bind(username).fetch_optional(state.db.pool()).await.ok().flatten().is_some();
        if !owned || crate::handlers::find_image(state, &pid, idx).await.is_none() {
            ghosts += 1;
        }
    }
    ghosts
}

/// Everything decided before the stream starts.
struct Turn {
    username: String,
    model: String,
    user_key: String,
    history: Vec<Msg>,
    msgs: Vec<Msg>,
    attachments: Vec<String>,
    mem_on: bool,
    reasoning: bool,
    effort: Option<String>,
    temperature: f64,
    max_tokens: i64,
    top_p: f64,
    web_ok: bool,
    img: (bool, bool, bool),
    vid: (bool, bool, bool),
    doc: (bool, bool, bool),
    files: tools::files::Files,
    edit_ok: bool,
    skill_rows: Vec<tools::library::SkillRow>,
    skill_ok: bool,
    chats_ok: bool,
}

/// POST /playground/chat
pub async fn playground_chat(state: AppState, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match crate::handlers::csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data: Map<String, Value> = crate::handlers::json_object(&headers, &body);
    let mut hist = history::parse_messages(&data);
    if hist.is_empty() {
        return sse(notice("empty_message", json!({}), true));
    }
    let attachments = history::attachments(&hist);
    let maintenance = !session.is_admin && state.db.maintenance_active().await;
    if maintenance {
        return sse(crate::handlers::sse_msg("Maintenance in progress — model access is temporarily suspended, please try again later."));
    }
    let user = session.username.clone();
    match crate::guards::rate_limited(state.db.pool(), &user, "rl-playground", 20, 60.0).await {
        Ok(0) => {}
        Ok(wait) => return sse(notice("chat_rate_limited", json!({"wait": wait}), true)),
        Err(_) => return crate::auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
    let running = crate::models::running_models().await;
    let Some(first) = running.first().cloned() else { return sse(notice("no_model_running", json!({}), true)) };
    let model = data.get("model").and_then(Value::as_str).filter(|m| running.iter().any(|r| r == m)).map(str::to_string).unwrap_or(first);
    let (limits, vision) = match crate::handlers::model_windows(&state, &running).await {
        Ok(x) => x,
        Err(_) => return crate::auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    history::bound_images(&mut hist, vision.get(&model).and_then(Value::as_bool).unwrap_or(false));
    let mut system: String = crate::memory::py_strip(&data.get("system").map(history::py_str).unwrap_or_default()).chars().take(SYSTEM_MAX).collect();
    let temperature = num_f(data.get("temperature"), 0.0, 2.0, 0.7);
    let mut max_tokens = num_i(data.get("max_tokens"), 1, 131072, 4096);
    let top_p = num_f(data.get("top_p"), 0.0, 1.0, 1.0);
    let mode = match data.get("reasoning") {
        None => "auto".to_string(),
        Some(Value::Bool(b)) => if *b { "on".into() } else { "off".into() },
        Some(v) => {
            let m = crate::memory::py_strip(&history::py_str(v)).to_lowercase();
            if m.is_empty() { "auto".into() } else { m }
        }
    };
    let think = if mode == "auto" { reasoning::reasonably_complex(&history::last_user_message(&hist)) } else { !matches!(mode.as_str(), "off" | "false" | "0" | "non") };
    let raw_effort = data.get("reasoning_effort").filter(|v| crate::keys::truthy(v));
    let mut effort = raw_effort.map(|v| crate::memory::py_strip(&history::py_str(v)).to_lowercase()).filter(|e| matches!(e.as_str(), "low" | "medium" | "xhigh"));
    if effort.is_none() && raw_effort.is_none() && think {
        effort = Some(DEFAULT_REASONING_EFFORT.into());
    }
    let key: Option<String> = sqlx::query_scalar("SELECT key_value FROM api_keys WHERE username = ? ORDER BY created_at DESC LIMIT 1").bind(&user).fetch_optional(state.db.pool()).await.ok().flatten();
    let Some(user_key) = key else { return sse(notice("no_api_key", json!({}), true)) };
    if let Some(reset) = gauges::quota_exceeded_reset(&state.db, state.spend.as_ref(), &user).await {
        return sse(notice("quota_exceeded", json!({"reset": reset}), true));
    }
    let ctx = match input_limit(&state, &model, &running).await {
        Some(c) => Some(c),
        None => limits.get(&model).and_then(Value::as_i64),
    };
    let hist = history::history_for_model(&hist, &system, ctx);
    let mem_on = crate::memory::enabled(state.db.pool(), &user).await;
    if mem_on {
        let memctx = memory_turn::inject_context(state.db.pool(), &user).await;
        if !memctx.is_empty() {
            system = if system.is_empty() { memctx } else { format!("{system}\n\n{memctx}") };
        }
    }
    system = format!("{}{}", if system.is_empty() { String::new() } else { format!("{system}\n\n") }, history::timestamp());
    let mut msgs = vec![Msg::text("system", system)];
    msgs.extend(hist.iter().cloned());

    let web_ok = data.get("web") != Some(&Value::Bool(false)) && triggers::search_relevant(&hist) && websearch_active(&state, &user).await;
    let img_req = triggers::image_requested(&hist);
    let img_service = crate::sidecars::image_ready().await;
    let vid_req = triggers::video_requested(&hist);
    let vid_service = crate::sidecars::comfyui_up().await || tools::media::startable("video");
    let doc_req = triggers::document_requested(&hist);
    let doc_service = crate::sidecars::ocr_model().await.is_some() || tools::media::startable("ocr");
    let files = tools::files::parse_files(data.get("files"));
    let edit_ok = !files.is_empty() && triggers::edit_requested(&hist);
    let skill_rows = tools::library::enabled_skills(state.db.pool(), &user).await;
    let invoked: String = crate::memory::py_strip(&data.get("skill").filter(|v| crate::keys::truthy(v)).map(history::py_str).unwrap_or_default()).chars().take(64).collect();
    if !invoked.is_empty() {
        let (text, ok) = tools::library::use_skill(state.db.pool(), &user, &invoked).await;
        if ok {
            msgs[0].content.push_str(&format!("\n\nThe user invoked the following skill for this request: follow its instructions.\n\n{text}"));
        }
    }
    let last_user = hist.iter().rev().find(|h| h.role == "user").map(|h| h.content.clone()).unwrap_or_default();
    let skill_ok = !skill_rows.is_empty() && (!invoked.is_empty() || !tools::library::matching_skills(&skill_rows, &last_user).is_empty());
    let chats_ok = triggers::refers_to_past(&hist) && tools::library::search_enabled(state.db.pool(), &user).await;
    if let Some(window) = limits.get(&model).and_then(Value::as_i64).filter(|c| *c != 0) {
        // ~3 characters per token (pessimistic), 512 for the chat template.
        let approx_prompt = (msgs.iter().map(history::weight).sum::<usize>() / 3) as i64;
        max_tokens = 256i64.max(max_tokens.min(window - approx_prompt - 512));
    }
    let turn = Turn {
        username: user, model, user_key, history: hist, msgs, attachments, mem_on, reasoning: think, effort, temperature, max_tokens, top_p,
        web_ok, img: (img_req, img_service, img_req && img_service), vid: (vid_req, vid_service, vid_req && vid_service), doc: (doc_req, doc_service, doc_req && doc_service),
        files, edit_ok, skill_rows, skill_ok, chats_ok,
    };
    let (tx, rx) = tokio::sync::mpsc::channel(256);
    let out = Out(tx);
    tokio::spawn(async move {
        let rid = gauges::inflight_start(state.db.pool(), &turn.username).await;
        run(&state, turn, &out, &rid).await;
        gauges::inflight_end(state.db.pool(), &rid).await;
    });
    (
        [
            (axum::http::header::CONTENT_TYPE, "text/event-stream; charset=utf-8"),
            (axum::http::header::CACHE_CONTROL, "no-cache"),
            (axum::http::HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        axum::body::Body::from_stream(tokio_stream::wrappers::ReceiverStream::new(rx)),
    )
        .into_response()
}

/// What the reader hands the relay.
enum Upstream {
    Line(String),
    Status(u16),
    Failed(bool),
}

/// The stream of a turn: tools first if any, then the model's answer relayed.
async fn run(state: &AppState, mut t: Turn, out: &Out, rid: &str) {
    if !out.send(": ouverture\n\n").await {
        return;
    }
    for (req, service, id) in [(t.img.0, t.img.1, "image_service_off"), (t.vid.0, t.vid.1, "video_service_off"), (t.doc.0, t.doc.1, "document_service_off")] {
        if req && !service {
            out.send(data_frame(&json!({"dgx_notice": {"id": id, "free_gib": free_gib()}}))).await;
        }
    }
    let t0 = Instant::now();
    let slots = std::sync::Arc::new(std::sync::Mutex::new((None::<Instant>, None::<(usize, usize)>, None::<(usize, usize)>)));
    let queue_event = {
        let slots = slots.clone();
        move || -> Option<String> {
            let mut s = slots.lock().unwrap_or_else(|e| e.into_inner());
            if s.0.is_none_or(|at| at.elapsed() >= Duration::from_secs(10)) {
                s.0 = Some(Instant::now());
                let slots = slots.clone();
                tokio::spawn(async move {
                    let v = gauges::slots_busy().await;
                    slots.lock().unwrap_or_else(|e| e.into_inner()).1 = v;
                });
            }
            let v = s.1?;
            if Some(v) == s.2 {
                return None;
            }
            s.2 = Some(v);
            Some(data_frame(&json!({"dgx_queue": {"busy": v.0, "total": v.1}})))
        }
    };
    if let Some(q) = queue_event() {
        out.send(q).await;
    }
    let mut edit_summary = None;
    if t.web_ok || t.img.2 || t.vid.2 || t.doc.2 || t.edit_ok || t.skill_ok || t.chats_ok {
        out.send(": recherche\n\n").await;
        let originals = t.files.clone();
        let armed = phase::Armed {
            web: t.web_ok, image: t.img.2, video: t.vid.2, document: t.doc.2,
            files: if t.edit_ok { Some(&mut t.files) } else { None },
            skills: if t.skill_ok { Some(&t.skill_rows) } else { None },
            history: t.chats_ok,
        };
        let o = phase::run(state, &t.model, &t.msgs, &t.user_key, &t.username, &t.attachments, armed, out).await;
        for (path, before) in &o.changes {
            let now = t.files.get(path);
            if now != before.as_ref() {
                out.send(data_frame(&json!({"dgx_file": {"name": path, "content": now, "created": before.is_none()}}))).await;
            }
        }
        let _ = originals;
        let txt = phase::reinjection(&o, Some(&t.files));
        if !txt.is_empty() {
            if let Some(m) = t.msgs.iter_mut().rev().find(|m| m.role == "user") {
                m.content.push_str(&txt);
            }
        }
        edit_summary = o.edit_summary;
    }
    // The reader: the POST blocks until the end of the prefill, so it runs apart
    // and the relay keeps beating time meanwhile.
    let (ltx, mut lrx) = tokio::sync::mpsc::channel::<Upstream>(1000);
    let reader = {
        let (model, key, msgs) = (t.model.clone(), t.user_key.clone(), history::msgs_api(&t.msgs));
        let (temperature, max_tokens, top_p, think, effort) = (t.temperature, t.max_tokens, t.top_p, t.reasoning, t.effort.clone());
        let user = t.username.clone();
        tokio::spawn(async move {
            if let Some(summary) = edit_summary {
                // The edit phase already wrote the answer: replayed as a one-chunk stream.
                for l in [
                    format!("data: {}", crate::handlers::py_dumps_ascii(&json!({"choices": [{"index": 0, "delta": {"role": "assistant", "content": summary}}]}))),
                    format!("data: {}", crate::handlers::py_dumps_ascii(&json!({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))),
                    "data: [DONE]".to_string(),
                ] {
                    let _ = ltx.send(Upstream::Line(l)).await;
                }
                return;
            }
            let base = std::env::var("LITELLM_URL").unwrap_or_else(|_| "http://litellm:4000".to_string());
            let Ok(client) = reqwest::Client::builder().connect_timeout(Duration::from_secs(10)).read_timeout(READ_TIMEOUT).build() else {
                let _ = ltx.send(Upstream::Failed(false)).await;
                return;
            };
            let open = |ctk: Value| {
                client.post(format!("{}/v1/chat/completions", base.trim_end_matches('/'))).bearer_auth(&key)
                    .json(&json!({"model": model, "messages": msgs, "stream": true, "temperature": temperature, "max_tokens": max_tokens,
                                  "top_p": top_p, "stream_options": {"include_usage": true}, "chat_template_kwargs": ctk}))
                    .send()
            };
            let mut ctk = json!({"enable_thinking": think});
            if let Some(e) = &effort {
                ctk["reasoning_effort"] = json!(e);
            }
            let mut r = match open(ctk).await {
                Ok(r) => r,
                Err(e) => {
                    let _ = ltx.send(Upstream::Failed(e.is_timeout() && !e.is_connect())).await;
                    return;
                }
            };
            if !r.status().is_success() && effort.is_some() && r.status().as_u16() == 500 {
                // This model's template refuses the effort: once more without it.
                eprintln!("[chat] playground {user}: reasoning_effort refused by the model — retrying without");
                r = match open(json!({"enable_thinking": think})).await {
                    Ok(r) => r,
                    Err(e) => {
                        let _ = ltx.send(Upstream::Failed(e.is_timeout() && !e.is_connect())).await;
                        return;
                    }
                };
            }
            if !r.status().is_success() {
                let _ = ltx.send(Upstream::Status(r.status().as_u16())).await;
                return;
            }
            let mut pending: Vec<u8> = Vec::new();
            loop {
                match r.chunk().await {
                    Ok(Some(chunk)) => {
                        pending.extend_from_slice(&chunk);
                        while let Some(pos) = pending.iter().position(|b| *b == b'\n') {
                            let raw: Vec<u8> = pending.drain(..=pos).collect();
                            let line = String::from_utf8_lossy(&raw[..raw.len() - 1]).trim_end_matches('\r').to_string();
                            // The relay is gone (client left): dropping `r` frees the engine slot.
                            if ltx.send(Upstream::Line(line)).await.is_err() {
                                return;
                            }
                        }
                    }
                    Ok(None) => {
                        if !pending.is_empty() {
                            let _ = ltx.send(Upstream::Line(String::from_utf8_lossy(&pending).trim_end_matches('\r').to_string())).await;
                        }
                        return;
                    }
                    Err(e) => {
                        let _ = ltx.send(Upstream::Failed(e.is_timeout())).await;
                        return;
                    }
                }
            }
        })
    };
    let (mut finish, mut out_tokens, mut bytes) = (None::<String>, None::<i64>, 0usize);
    let (mut ttft_seen, mut ttft_s, mut chars_seen) = (false, None::<f64>, 0usize);
    let (mut gauge_at, mut first_tok_wall) = (None::<Instant>, None::<f64>);
    let (mut response, mut answer, mut answer_len) = (Vec::<String>::new(), String::new(), 0usize);
    let mut status = None::<u16>;
    let mut failure = None::<bool>;
    loop {
        let item = tokio::select! {
            item = lrx.recv() => item,
            _ = tokio::time::sleep(Duration::from_secs(5)) => {
                if !out.send(": attente\n\n").await {
                    reader.abort();
                    return;
                }
                if !ttft_seen {
                    if let Some(q) = queue_event() {
                        out.send(q).await;
                    }
                }
                continue;
            }
        };
        let Some(item) = item else { break };
        let txt = match item {
            Upstream::Line(l) => l,
            Upstream::Status(s) => {
                status = Some(s);
                break;
            }
            Upstream::Failed(timeout) => {
                failure = Some(timeout);
                break;
            }
        };
        if txt.is_empty() {
            continue;
        }
        if txt.starts_with("data: ") && txt.contains("\"delta\"") {
            if let Ok(v) = serde_json::from_str::<Value>(&txt[6..]) {
                let dl = v.pointer("/choices/0/delta").cloned().unwrap_or(Value::Null);
                let content = dl.get("content").and_then(Value::as_str).unwrap_or("");
                let reasoning_c = dl.get("reasoning_content").and_then(Value::as_str).unwrap_or("");
                let dc = content.chars().count() + reasoning_c.chars().count();
                if dc > 0 {
                    if !ttft_seen {
                        let s = t0.elapsed().as_secs_f64();
                        ttft_s = Some(s);
                        first_tok_wall = Some(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0));
                        gauges::record_ttft(&state.db, s * 1000.0).await;
                        ttft_seen = true;
                    }
                    chars_seen += dc;
                    if !content.is_empty() && answer_len < 200_000 {
                        answer.push_str(content);
                        answer_len += content.chars().count();
                    }
                    if gauge_at.is_none_or(|at| at.elapsed() >= Duration::from_secs(1)) {
                        gauge_at = Some(Instant::now());
                        gauges::inflight_tokens(&state.db, rid, chars_seen, first_tok_wall).await;
                    }
                }
                if t.mem_on && response.len() < 400 && !content.is_empty() {
                    response.push(content.to_string());
                }
            }
        }
        if txt.contains("\"finish_reason\"") || txt.contains("\"completion_tokens\"") {
            let d = txt.strip_prefix("data: ").and_then(|p| serde_json::from_str::<Value>(p).ok()).unwrap_or(Value::Null);
            if let Some(f) = d.pointer("/choices/0/finish_reason").and_then(Value::as_str).filter(|f| !f.is_empty()) {
                finish = Some(f.to_string());
            }
            if let Some(u) = d.get("usage").filter(|u| crate::keys::truthy(u)) {
                if let Some(c) = u.get("completion_tokens").and_then(Value::as_i64).filter(|c| *c != 0) {
                    out_tokens = Some(c);
                }
                if let (Some(pt), Some(s)) = (u.get("prompt_tokens").and_then(Value::as_i64).filter(|p| *p != 0), ttft_s) {
                    gauges::record_prefill(&state.db, pt, s).await;
                }
                if let Some(o) = out_tokens {
                    gauges::record_ratio(&state.db, chars_seen, o).await;
                }
            }
        }
        bytes += txt.len();
        if !out.send(format!("{txt}\n\n")).await {
            eprintln!("[chat] playground {}: client left after {bytes} bytes / {:?} tokens", t.username, out_tokens);
            reader.abort();
            return;
        }
    }
    if let Some(timeout) = failure {
        eprintln!("[chat] playground {}: stream interrupted", t.username);
        out.send(notice(if timeout { "model_timeout" } else { "model_unreachable" }, json!({}), true)).await;
        return;
    }
    if let Some(s) = status {
        if s == 429 {
            let reset = crate::keys::account_info(&t.username).await.budget_reset_at.chars().take(16).collect::<String>().replace('T', " ");
            out.send(notice("quota_exceeded", json!({"reset": reset}), true)).await;
        } else {
            out.send(notice("model_replied_error", json!({"status": s}), true)).await;
        }
        return;
    }
    match finish.as_deref() {
        None => {
            eprintln!("[chat] playground {}: upstream stream closed without finish_reason after {bytes} bytes — reply cut", t.username);
            out.send(data_frame(&json!({"choices": [{"delta": {}, "finish_reason": "length"}]}))).await;
            out.send("data: [DONE]\n\n").await;
        }
        Some("tool_calls") => eprintln!("[chat] playground {}: SPONTANEOUS finish_reason=tool_calls, {} content tokens", t.username, out_tokens.unwrap_or(0)),
        Some("stop") => {
            if out_tokens.is_some_and(|o| o > 4000) {
                eprintln!("[chat] playground {}: normal end (stop) after {:?} tokens", t.username, out_tokens);
            }
        }
        Some(f) => eprintln!("[chat] playground {}: finish_reason={f}, {:?} tokens produced", t.username, out_tokens),
    }
    if answer.contains("/image/file/") {
        let ghosts = invented_images(state, &answer, &t.username).await;
        if ghosts > 0 {
            eprintln!("[chat] playground {}: {ghosts} invented image address(es)", t.username);
            out.send(notice("image_invented", json!({"count": ghosts}), false)).await;
        }
    }
    if t.mem_on && finish.as_deref() == Some("stop") {
        let mut excerpts: Vec<(String, String)> = t.history[t.history.len().saturating_sub(4)..].iter().map(|m| (m.role.clone(), m.content.clone())).collect();
        excerpts.push(("assistant".into(), response.concat().chars().take(4000).collect()));
        tokio::spawn(memory_turn::extract_and_save(state.db.pool().clone(), t.username.clone(), t.model.clone(), t.user_key.clone(), excerpts));
    }
}
