//! Read-only media routes, parity with dgx-portal: the job histories and
//! statuses of image, OCR, video and voice, the voice capabilities, and whether
//! dictation can serve. The routes that create jobs, call a sidecar to act, or
//! return a stored file are NOT here: they write, or they read media that may
//! live on S3 (media_store), and stay on Flask until they are ported whole.
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};
use sqlx::sqlite::SqliteRow;
use sqlx::{Column, Row, SqlitePool};

use crate::sidecars;

fn sqlite_value(row: &SqliteRow, idx: usize) -> Value {
    // Every column of these tables is text, an integer, a real or null.
    if let Ok(v) = row.try_get::<Option<i64>, _>(idx) {
        return v.map_or(Value::Null, |n| json!(n));
    }
    if let Ok(v) = row.try_get::<Option<f64>, _>(idx) {
        return v.map_or(Value::Null, |n| json!(n));
    }
    row.try_get::<Option<String>, _>(idx).ok().flatten().map_or(Value::Null, Value::String)
}

/// `[dict(r) for r in rows]`: every row as an object keyed by column name.
fn rows_to_json(rows: &[SqliteRow]) -> Value {
    Value::Array(
        rows.iter()
            .map(|row| {
                let mut obj = Map::new();
                for (i, col) in row.columns().iter().enumerate() {
                    obj.insert(col.name().to_string(), sqlite_value(row, i));
                }
                Value::Object(obj)
            })
            .collect(),
    )
}

/// `/api/image/history`: the viewer's image jobs, newest first.
pub async fn image_history(pool: &SqlitePool, username: &str) -> Result<Value, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT prompt_id, prompt, status, count, done_count, format, created_at FROM image_jobs \
         WHERE username = ? ORDER BY id DESC",
    )
    .bind(username)
    .fetch_all(pool)
    .await?;
    Ok(rows_to_json(&rows))
}

/// `/api/image/status/<prompt_id>`: one of the viewer's jobs, None when not theirs.
pub async fn image_status(pool: &SqlitePool, username: &str, prompt_id: &str) -> Result<Option<Value>, sqlx::Error> {
    let row = sqlx::query("SELECT status, count, done_count FROM image_jobs WHERE prompt_id = ? AND username = ?")
        .bind(prompt_id)
        .bind(username)
        .fetch_optional(pool)
        .await?;
    Ok(row.map(|r| {
        json!({
            "status": sqlite_value(&r, 0),
            "count": sqlite_value(&r, 1),
            "done_count": sqlite_value(&r, 2),
        })
    }))
}

/// `/api/ocr/history`: the viewer's OCR jobs (`has_image` says whether the scan was kept).
pub async fn ocr_history(pool: &SqlitePool, username: &str) -> Result<Value, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT id, text, created_at, image_path IS NOT NULL AS has_image FROM ocr_jobs \
         WHERE username = ? ORDER BY id DESC",
    )
    .bind(username)
    .fetch_all(pool)
    .await?;
    Ok(rows_to_json(&rows))
}

/// `/api/video/history`: the viewer's video jobs, newest first.
pub async fn video_history(pool: &SqlitePool, username: &str) -> Result<Value, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT prompt_id, prompt, status, created_at FROM video_jobs WHERE username = ? ORDER BY id DESC",
    )
    .bind(username)
    .fetch_all(pool)
    .await?;
    Ok(rows_to_json(&rows))
}

/// `/api/voice/history`: the viewer's voice jobs, newest first.
pub async fn voice_history(pool: &SqlitePool, username: &str) -> Result<Value, sqlx::Error> {
    let rows = sqlx::query("SELECT id, text, created_at FROM voice_jobs WHERE username = ? ORDER BY id DESC")
        .bind(username)
        .fetch_all(pool)
        .await?;
    Ok(rows_to_json(&rows))
}

type Cache = Mutex<Option<(Instant, Value)>>;
static VOICE_INFO: Cache = Mutex::new(None);
static ASR_INFO: Cache = Mutex::new(None);

fn cached(cache: &Cache, ttl: Duration) -> Option<Value> {
    let guard = cache.lock().unwrap_or_else(|e| e.into_inner());
    guard.as_ref().filter(|(at, _)| at.elapsed() < ttl).map(|(_, v)| v.clone())
}

fn store(cache: &Cache, v: &Value) {
    *cache.lock().unwrap_or_else(|e| e.into_inner()) = Some((Instant::now(), v.clone()));
}

fn sidecar_url(name: &str, default: &str) -> String {
    std::env::var(name).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string()).trim_end_matches('/').to_string()
}

async fn sidecar_model_info(url: &str, timeout: Duration) -> Value {
    let Ok(c) = reqwest::Client::builder().timeout(timeout).build() else {
        return Value::Null;
    };
    match c.get(format!("{url}/api/model-info")).send().await {
        Ok(r) if r.status().is_success() => r.json::<Value>().await.unwrap_or(Value::Null),
        _ => Value::Null,
    }
}

/// voice_routes._voice_model_info: the voice sidecar's model-info, cached 30 s.
async fn voice_model_info() -> Value {
    if let Some(v) = cached(&VOICE_INFO, Duration::from_secs(30)) {
        return v;
    }
    let v = sidecar_model_info(&sidecar_url("VOICE_URL", "http://voice:8004"), Duration::from_secs(3)).await;
    store(&VOICE_INFO, &v);
    v
}

/// `/api/voice/info`: what the loaded voice backend accepts. The page adapts to it.
pub async fn voice_info() -> Value {
    let info = voice_model_info().await;
    let engine = sidecars::voice_engine().await;
    json!({
        "engine": engine,
        "languages": info.get("supported_languages").filter(|v| v.is_object()).cloned().unwrap_or_else(|| json!({})),
        "supports_ref_text": engine == "qwen3-tts",
    })
}

/// asr_routes / sidecars._asr_info: the ASR sidecar's model-info (0.3 s, cached 10 s).
async fn asr_info() -> Value {
    if let Some(v) = cached(&ASR_INFO, Duration::from_secs(10)) {
        return v;
    }
    let v = sidecar_model_info(&sidecar_url("ASR_URL", "http://asr:8006"), Duration::from_millis(300)).await;
    store(&ASR_INFO, &v);
    v
}

/// `/api/transcribe/available`: dictation can serve NOW, or ON DEMAND (a stopped
/// container is available: the first transcription starts it).
pub async fn transcribe_available() -> bool {
    if asr_info().await.get("loaded").and_then(Value::as_bool).unwrap_or(false) {
        return true;
    }
    matches!(sidecars::proc_status("asr").await.as_str(), "running" | "stopped")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_expires_after_its_ttl() {
        let c: Cache = Mutex::new(None);
        store(&c, &json!(1));
        assert_eq!(cached(&c, Duration::from_secs(60)), Some(json!(1)));
        assert_eq!(cached(&c, Duration::from_nanos(1)).is_none(), true);
    }
}
