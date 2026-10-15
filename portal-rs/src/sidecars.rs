//! Media sidecars (OCR, voice, image, ComfyUI video), parity with
//! dgx-portal/sidecars.py, comfyui_client.py, voice_routes.py and app.py
//! `_running_and_metrics` / `_sidecar_metrics`.
//!
//! The container state comes from the runner (`/<kind>/status`, it holds the
//! scoped docker/systemd rights; the portal has none). The HTTP probes are
//! short (0.3 s): an on-demand sidecar that is stopped must not make the
//! dashboard wait — a false negative is retried at the next call.
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::db::Database;
use crate::{models, probes};

const PROBE: Duration = Duration::from_millis(300);

fn env_url(name: &str, default: &str) -> String {
    std::env::var(name).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string()).trim_end_matches('/').to_string()
}

pub(crate) fn ocr_url() -> String {
    env_url("OCR_URL", "http://ocr:8000/v1")
}
fn voice_url() -> String {
    env_url("VOICE_URL", "http://voice:8004")
}
pub(crate) fn image_url() -> String {
    env_url("IMAGE_URL", "http://image:8007")
}
fn comfyui_url() -> String {
    env_url("COMFYUI_URL", "http://host.docker.internal:8188")
}

async fn get_json(url: &str, timeout: Duration, bearer: Option<&str>) -> Option<Value> {
    let c = reqwest::Client::builder().timeout(timeout).build().ok()?;
    let mut req = c.get(url);
    if let Some(t) = bearer {
        req = req.bearer_auth(t);
    }
    let r = req.send().await.ok()?;
    if !r.status().is_success() {
        return None;
    }
    r.json().await.ok()
}

type Cache = Mutex<Option<HashMap<String, (Instant, Value)>>>;

fn cached(cache: &Cache, key: &str, ttl: Duration) -> Option<Value> {
    let guard = cache.lock().unwrap_or_else(|e| e.into_inner());
    guard.as_ref()?.get(key).filter(|(at, _)| at.elapsed() < ttl).map(|(_, v)| v.clone())
}

fn store(cache: &Cache, key: &str, v: &Value) {
    let mut guard = cache.lock().unwrap_or_else(|e| e.into_inner());
    guard.get_or_insert_with(HashMap::new).insert(key.to_string(), (Instant::now(), v.clone()));
}

static PROC: Cache = Mutex::new(None);
static MODELS: Cache = Mutex::new(None);
static METRICS: Cache = Mutex::new(None);

/// sidecars._sidecar_proc_status: the container/process state through the
/// runner, cached 5 s; « unreachable » when the runner does not answer.
pub async fn proc_status(kind: &str) -> String {
    if let Some(v) = cached(&PROC, kind, Duration::from_secs(5)) {
        return v.as_str().unwrap_or("unreachable").to_string();
    }
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let v = match get_json(&format!("{}/{kind}/status", probes::runner_url()), Duration::from_secs(5), Some(&token)).await {
        Some(body) => body.get("status").and_then(Value::as_str).unwrap_or("unknown").to_string(),
        None => "unreachable".to_string(),
    };
    store(&PROC, kind, &json!(v));
    v
}

/// get_ocr_model: the model the OCR container serves, None when stopped.
/// No HTTP call when the container is not running: the sidecar network drops
/// packets to an absent service, so the call would wait its whole timeout.
pub async fn ocr_model() -> Option<String> {
    if let Some(v) = cached(&MODELS, "ocr", Duration::from_secs(5)) {
        return v.as_str().map(str::to_string);
    }
    let mut v = None;
    if proc_status("ocr").await == "running" {
        if let Some(body) = get_json(&format!("{}/models", ocr_url()), PROBE, None).await {
            v = body["data"].get(0).and_then(|m| m["id"].as_str()).map(str::to_string);
        }
    }
    store(&MODELS, "ocr", &json!(v));
    v
}

/// get_voice_model: the loaded voice variant ('original' | 'turbo' | 'multilingual'),
/// only once the model is LOADED, not just the process up.
pub async fn voice_model() -> Option<String> {
    if let Some(v) = cached(&MODELS, "voice", Duration::from_secs(5)) {
        return v.as_str().map(str::to_string);
    }
    let mut v = None;
    if proc_status("voice").await == "running" {
        if let Some(body) = get_json(&format!("{}/api/model-info", voice_url()), PROBE, None).await {
            if body["loaded"].as_bool().unwrap_or(false) {
                v = body["type"].as_str().map(str::to_string);
            }
        }
    }
    store(&MODELS, "voice", &json!(v));
    v
}

/// voice_routes.get_voice_engine: 'chatterbox' or 'qwen3-tts' (model-info cached 30 s).
pub async fn voice_engine() -> String {
    let info = match cached(&MODELS, "voice_info", Duration::from_secs(30)) {
        Some(v) => v,
        None => {
            let v = get_json(&format!("{}/api/model-info", voice_url()), Duration::from_secs(3), None)
                .await
                .unwrap_or(Value::Null);
            store(&MODELS, "voice_info", &v);
            v
        }
    };
    info["engine"].as_str().filter(|e| !e.is_empty()).unwrap_or("chatterbox").to_string()
}

/// sidecars.image_ready: the image sidecar answers AND its model is ready.
pub async fn image_ready() -> bool {
    get_json(&format!("{}/health", image_url()), Duration::from_secs(3), None)
        .await
        .is_some_and(|b| b["ready"].as_bool().unwrap_or(false))
}

/// sidecars.get_image_model: the image model's name, or None.
pub async fn image_model() -> Option<String> {
    get_json(&format!("{}/model-info", image_url()), PROBE, None)
        .await
        .and_then(|b| b["model"].as_str().map(str::to_string))
}

/// comfyui_client.comfyui_is_up: ComfyUI (video) answers, cached 5 s.
pub async fn comfyui_up() -> bool {
    if let Some(v) = cached(&MODELS, "comfyui", Duration::from_secs(5)) {
        return v.as_bool().unwrap_or(false);
    }
    let up = async {
        let c = reqwest::Client::builder().timeout(PROBE).build().ok()?;
        Some(c.get(format!("{}/system_stats", comfyui_url())).send().await.ok()?.status().is_success())
    }
    .await
    .unwrap_or(false);
    store(&MODELS, "comfyui", &json!(up));
    up
}

/// Python's round(x) to an int: ties to even on the exact binary value.
fn py_round0(x: f64) -> i64 {
    format!("{x:.0}").parse().unwrap_or(x as i64)
}

fn py_round1(x: f64) -> f64 {
    format!("{x:.1}").parse().unwrap_or(x)
}

/// app._sidecar_metrics: today's and total generations, average and last
/// duration over the 20 latest timed jobs, and per-kind throughputs; cached 3 s.
pub async fn metrics(db: &Database, kind: &str) -> Value {
    if let Some(v) = cached(&METRICS, kind, Duration::from_secs(3)) {
        return v;
    }
    let table = match kind {
        "ocr" => "ocr_jobs",
        "video" => "video_jobs",
        "voice" => "voice_jobs",
        _ => return Value::Null,
    };
    // datetime.now() in the portal container, which runs in UTC.
    let today = chrono::Utc::now().format("%Y-%m-%d").to_string();
    let pool = db.pool();
    let count_today: i64 = sqlx::query_as::<_, (i64,)>(&format!("SELECT COUNT(*) FROM {table} WHERE created_at >= ?"))
        .bind(&today)
        .fetch_one(pool)
        .await
        .map(|r| r.0)
        .unwrap_or(0);
    let total: i64 = sqlx::query_as::<_, (i64,)>(&format!("SELECT COUNT(*) FROM {table}"))
        .fetch_one(pool)
        .await
        .map(|r| r.0)
        .unwrap_or(0);
    // (duration_ms, text chars, audio_ms, req_duration_s) of the 20 latest timed jobs.
    let cols = match kind {
        "ocr" => "duration_ms, text, NULL, NULL",
        "voice" => "duration_ms, text, audio_ms, NULL",
        _ => "duration_ms, NULL, NULL, req_duration_s",
    };
    let recent: Vec<(Option<i64>, Option<String>, Option<i64>, Option<i64>)> = sqlx::query_as(&format!(
        "SELECT {cols} FROM {table} WHERE duration_ms IS NOT NULL ORDER BY id DESC LIMIT 20"
    ))
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    let hard: Vec<i64> = recent.iter().filter_map(|r| r.0.filter(|d| *d != 0)).collect();
    let mut m = json!({
        "count_today": count_today, "total": total,
        "avg_ms": if hard.is_empty() { Value::Null } else { json!(py_round0(hard.iter().sum::<i64>() as f64 / hard.len() as f64)) },
        "last_ms": hard.first(),
    });
    let avg = |vals: Vec<f64>| -> Option<f64> { (!vals.is_empty()).then(|| vals.iter().sum::<f64>() / vals.len() as f64) };
    if kind == "ocr" || kind == "voice" {
        let cps = avg(recent
            .iter()
            .filter_map(|r| match (r.0, &r.1) {
                (Some(d), Some(t)) if d != 0 && !t.is_empty() => Some(t.chars().count() as f64 * 1000.0 / d as f64),
                _ => None,
            })
            .collect());
        m["chars_per_s"] = cps.filter(|v| *v != 0.0).map(py_round0).into();
    }
    if kind == "ocr" {
        m["chars_avg"] = if recent.is_empty() {
            Value::Null
        } else {
            let chars: usize = recent.iter().map(|r| r.1.as_deref().unwrap_or_default().chars().count()).sum();
            json!(py_round0(chars as f64 / recent.len() as f64))
        };
    }
    if kind == "voice" {
        let rtf = avg(recent
            .iter()
            .filter_map(|r| match (r.2, r.0) {
                (Some(a), Some(d)) if a != 0 && d != 0 => Some(a as f64 / d as f64),
                _ => None,
            })
            .collect());
        m["rtf"] = rtf.filter(|v| *v != 0.0).map(py_round1).into();
    }
    if kind == "video" {
        let ends: Vec<(String, i64)> = sqlx::query_as(&format!(
            "SELECT status, COUNT(*) FROM {table} WHERE status IN ('done','error') GROUP BY status"
        ))
        .fetch_all(pool)
        .await
        .unwrap_or_default();
        let get = |s: &str| ends.iter().find(|(k, _)| k == s).map_or(0, |(_, n)| *n);
        let finished = get("done") + get("error");
        m["success_rate"] = if finished != 0 { json!(py_round0(100.0 * get("done") as f64 / finished as f64)) } else { Value::Null };
        let secs: Option<i64> = sqlx::query_as::<_, (Option<i64>,)>(&format!(
            "SELECT SUM(req_duration_s) FROM {table} WHERE created_at >= ? AND req_duration_s IS NOT NULL"
        ))
        .bind(&today)
        .fetch_one(pool)
        .await
        .ok()
        .and_then(|r| r.0);
        m["video_secs_today"] = json!(secs);
        let gpv = avg(recent
            .iter()
            .filter_map(|r| match (r.3, r.0) {
                (Some(s), Some(d)) if s != 0 && d != 0 => Some((d as f64 / 1000.0) / s as f64),
                _ => None,
            })
            .collect());
        m["gen_per_vsec"] = gpv.filter(|v| *v != 0.0).map(py_round1).into();
    }
    store(&METRICS, kind, &m);
    m
}

/// app._running_and_metrics: what runs right now (chat model + sidecars)
/// and the sidecars' metrics. The probes run together: the page then costs
/// the slowest probe, not their sum.
pub async fn running_and_metrics(db: &Database) -> (Vec<Value>, Value) {
    let (chat, ocr, comfy, image, voice, image_name, m_ocr, m_video, m_voice) = tokio::join!(
        models::running_models(),
        ocr_model(),
        comfyui_up(),
        image_ready(),
        voice_model(),
        image_model(),
        metrics(db, "ocr"),
        metrics(db, "video"),
        metrics(db, "voice"),
    );
    let mut running: Vec<Value> = chat.into_iter().map(|m| json!({"name": m, "kind": "chat", "exposed": true})).collect();
    let mut out = serde_json::Map::new();
    if let Some(m) = ocr {
        running.push(json!({"name": m, "kind": "ocr", "exposed": false}));
        out.insert("ocr".into(), m_ocr);
    }
    if comfy {
        running.push(json!({"name": "MiniMax-H3", "kind": "video", "exposed": false}));
        out.insert("video".into(), m_video);
    }
    if image {
        running.push(json!({"name": image_name.unwrap_or_else(|| "Image".to_string()), "kind": "image", "exposed": false}));
    }
    if let Some(v) = voice {
        let label = if voice_engine().await == "qwen3-tts" { "Qwen3-TTS" } else { "Chatterbox" };
        running.push(json!({"name": format!("{label} ({v})"), "kind": "voice", "exposed": false}));
        out.insert("voice".into(), m_voice);
    }
    (running, Value::Object(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounding_follows_python() {
        assert_eq!(py_round0(2.5), 2);
        assert_eq!(py_round0(3.5), 4);
        assert_eq!(py_round0(1234.4), 1234);
        assert_eq!(py_round1(0.25), 0.2);
    }
}
