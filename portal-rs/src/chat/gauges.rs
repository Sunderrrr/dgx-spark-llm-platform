//! What the chat relay records as it streams (stats.py, guards.py): the
//! in-flight row behind the « who uses the model » panel and the live decode
//! gauge, the TTFT average, the chars/token calibration, the last prefill
//! rate, and the quota guard read before a request leaves.
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use sqlx::SqlitePool;

use crate::db::Database;

const TTFT_ALPHA: f64 = 0.2;
const QUOTA_CACHE_TTL: Duration = Duration::from_secs(30);

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// Python's `str(round(x, n))` for the settings these gauges write.
pub fn py_round_str(x: f64, n: i32) -> String {
    // `{:.n}` rounds the exact binary value half-to-even, as Python's round() does.
    let r: f64 = format!("{x:.prec$}", prec = n.max(0) as usize).parse().unwrap_or(x);
    if r.fract() == 0.0 && r.abs() < 1e16 { format!("{r:.1}") } else { format!("{r}") }
}

async fn set_setting(pool: &SqlitePool, key: &str, value: &str) {
    let _ = sqlx::query("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        .bind(key)
        .bind(value)
        .execute(pool)
        .await;
}

/// `_inflight_start`: the request's row, read by the admin panel and the decode gauge.
pub async fn inflight_start(pool: &SqlitePool, username: &str) -> String {
    let mut b = [0u8; 8];
    let _ = getrandom::getrandom(&mut b);
    let rid = hex::encode(b);
    let _ = sqlx::query("INSERT INTO inflight_requests (id, username, started_at) VALUES (?,?,?)").bind(&rid).bind(username).bind(now()).execute(pool).await;
    rid
}

pub async fn inflight_end(pool: &SqlitePool, rid: &str) {
    let _ = sqlx::query("DELETE FROM inflight_requests WHERE id=?").bind(rid).execute(pool).await;
}

/// `ratio_chars_per_token`: the calibrated ratio (4.0 until calibrated).
pub async fn ratio_chars_per_token(db: &Database) -> f64 {
    let r = db.get_setting("chars_per_token").await.ok().flatten().and_then(|v| v.trim().parse::<f64>().ok()).unwrap_or(0.0);
    if (0.5..=10.0).contains(&r) { r } else { 4.0 }
}

/// `_inflight_tokens`: live progress (tokens estimated from the characters seen);
/// the first token's wall time kept once.
pub async fn inflight_tokens(db: &Database, rid: &str, chars: usize, since: Option<f64>) {
    let estimated = (chars as f64 / ratio_chars_per_token(db).await) as i64;
    let q = match since {
        Some(s) => sqlx::query("UPDATE inflight_requests SET tokens=?, decode_since=COALESCE(decode_since, ?) WHERE id=?").bind(estimated).bind(s).bind(rid),
        None => sqlx::query("UPDATE inflight_requests SET tokens=? WHERE id=?").bind(estimated).bind(rid),
    };
    let _ = q.execute(db.pool()).await;
}

/// `record_ttft`: the TTFT moving average (milliseconds).
pub async fn record_ttft(db: &Database, ms: f64) {
    if ms <= 0.0 || ms > 600_000.0 {
        return;
    }
    let old = db.get_setting("ttft_ms_ewma").await.ok().flatten().and_then(|v| v.trim().parse::<f64>().ok());
    let new = match old {
        None => ms,
        Some(o) => (1.0 - TTFT_ALPHA) * o + TTFT_ALPHA * ms,
    };
    set_setting(db.pool(), "ttft_ms_ewma", &py_round_str(new, 1)).await;
}

/// `record_ratio`: calibrate chars/token on the exact count at the end.
pub async fn record_ratio(db: &Database, chars: usize, tokens: i64) {
    if tokens <= 8 || chars == 0 {
        return;
    }
    let r = chars as f64 / tokens as f64;
    if !(0.5..=10.0).contains(&r) {
        return;
    }
    let old = db.get_setting("chars_per_token").await.ok().flatten().filter(|v| !v.is_empty()).and_then(|v| v.trim().parse::<f64>().ok());
    let v = match old {
        Some(o) if o != 0.0 => 0.7 * o + 0.3 * r,
        _ => r,
    };
    set_setting(db.pool(), "chars_per_token", &py_round_str(v, 3)).await;
}

/// `record_prefill`: the last observed prefill rate (prompt tokens / TTFT).
pub async fn record_prefill(db: &Database, prompt_tokens: i64, ttft_s: f64) {
    if prompt_tokens <= 0 || ttft_s <= 0.0 {
        return;
    }
    set_setting(db.pool(), "prefill_rate_last", &py_round_str(prompt_tokens as f64 / ttft_s, 1)).await;
    set_setting(db.pool(), "prefill_rate_at", &format!("{}", now())).await;
}

static QUOTA: Mutex<Option<HashMap<String, (Instant, Option<String>)>>> = Mutex::new(None);

/// `quota_exceeded_reset`: the next reset date when the account has used its
/// envelope (SpendLogs, the reliable accounting), else None. Never blocks on a failure.
pub async fn quota_exceeded_reset(db: &Database, spend: Option<&sqlx::PgPool>, username: &str) -> Option<String> {
    if let Some((at, v)) = QUOTA.lock().unwrap_or_else(|e| e.into_inner()).as_ref().and_then(|m| m.get(username)) {
        if at.elapsed() < QUOTA_CACHE_TTL {
            return v.clone();
        }
    }
    let mut reset = None;
    let info = crate::keys::account_info(username).await;
    if let (Some(effective), Some(pool)) = (info.max_budget.filter(|b| *b != 0.0), spend) {
        let duration = db.get_setting("default_key_duration").await.ok().flatten()
            .unwrap_or_else(|| std::env::var("KEY_BUDGET_DURATION").unwrap_or_else(|_| "7d".to_string()));
        let digits: String = duration.chars().filter(|c| c.is_ascii_digit()).collect();
        let days: i64 = digits.parse().unwrap_or(7);
        let since = chrono::Utc::now().naive_utc() - chrono::Duration::days(days);
        if let Ok(used) = used_tokens(pool, username, since).await {
            if used >= effective as i64 {
                reset = Some(info.budget_reset_at.chars().take(16).collect::<String>().replace('T', " "));
            }
        }
    }
    QUOTA.lock().unwrap_or_else(|e| e.into_inner()).get_or_insert_with(HashMap::new).insert(username.to_string(), (Instant::now(), reset.clone()));
    reset
}

/// `_real_tokens_by_user(since)[username]`: prompt + generated tokens since a date.
async fn used_tokens(pool: &sqlx::PgPool, username: &str, since: chrono::NaiveDateTime) -> Result<i64, sqlx::Error> {
    let map = crate::spend::key_user_map(pool).await;
    let rows: Vec<(Option<String>, Option<i64>)> = sqlx::query_as(
        "SELECT api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0))::bigint FROM \"LiteLLM_SpendLogs\" WHERE \"startTime\" >= $1 GROUP BY api_key",
    )
    .bind(since)
    .fetch_all(pool)
    .await?;
    Ok(rows
        .into_iter()
        .filter(|(k, _)| !crate::spend::NON_USER_KEYS.contains(&k.as_deref().unwrap_or("")))
        .filter(|(k, _)| k.as_ref().and_then(|k| map.get(k)).is_some_and(|u| u == username))
        .map(|(_, n)| n.unwrap_or(0))
        .sum())
}

/// `slots_busy`: (busy, total) sessions of the engine, None without /slots.
pub async fn slots_busy() -> Option<(usize, usize)> {
    let api = crate::models::engine_api();
    let base = match api.rfind("/v1") {
        Some(i) => api[..i].to_string(),
        None => api,
    };
    let c = reqwest::Client::builder().timeout(Duration::from_secs(2)).build().ok()?;
    let slots: serde_json::Value = c.get(format!("{base}/slots")).send().await.ok()?.json().await.ok()?;
    let list = slots.as_array().filter(|a| !a.is_empty())?;
    Some((list.iter().filter(|s| s.get("is_processing").is_some_and(crate::keys::truthy)).count(), list.len()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_are_written_as_python_prints_them() {
        assert_eq!(py_round_str(1234.0, 1), "1234.0");
        assert_eq!(py_round_str(3.44049, 3), "3.44");
        assert_eq!(py_round_str(697.25, 1), "697.2");
        assert_eq!(py_round_str(2.675, 2), "2.67");
        assert_eq!(py_round_str(0.125, 2), "0.12");
    }
}
