//! LiteLLM's database (Postgres), read-only: consumption per request in
//! `LiteLLM_SpendLogs`, and the key → account mapping. Parity with
//! dgx-portal/db.py `_spend_conn` and stats.py `_key_user_map`.
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use sqlx::postgres::{PgPool, PgPoolOptions};

/// Keys that are not a person's (the proxy's own, empty ones).
pub const NON_USER_KEYS: [&str; 3] = ["litellm_proxy_master_key", "None", ""];

/// A lazy pool on `LITELLM_DATABASE_URL`; None when unset (no statistics).
/// Nothing connects until the first query; a connection waits 4 s at most,
/// like Flask's `connect_timeout=4`.
pub fn pool_from_env() -> Option<PgPool> {
    let url = std::env::var("LITELLM_DATABASE_URL").ok().filter(|v| !v.is_empty())?;
    PgPoolOptions::new()
        .max_connections(4)
        .acquire_timeout(Duration::from_secs(4))
        .connect_lazy(&url)
        .ok()
}

/// The display time zone (TZ_DISPLAY, as config.LOCAL_TZ).
pub fn local_tz() -> String {
    std::env::var("TZ_DISPLAY").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "Europe/Paris".into())
}

static KEY_MAP: Mutex<Option<(Instant, HashMap<String, String>)>> = Mutex::new(None);

/// token (hash) → username, from the keys' metadata, active and deleted.
/// Cached 30 s: it only changes when a key is created or revoked.
pub async fn key_user_map(pool: &PgPool) -> HashMap<String, String> {
    if let Some((at, map)) = KEY_MAP.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
        if at.elapsed() < Duration::from_secs(30) {
            return map.clone();
        }
    }
    let mut mapping = HashMap::new();
    for table in [
        "LiteLLM_VerificationToken",
        "LiteLLM_DeletedVerificationToken",
        "LiteLLM_DeprecatedVerificationToken",
    ] {
        // A table without `metadata` (the deprecated one) fails: skipped, as in Flask.
        let sql = format!("SELECT token, metadata->>'user' FROM \"{table}\"");
        if let Ok(rows) = sqlx::query_as::<_, (Option<String>, Option<String>)>(&sql).fetch_all(pool).await {
            for (token, user) in rows {
                if let (Some(t), Some(u)) = (token, user) {
                    if !t.is_empty() && !u.is_empty() {
                        mapping.entry(t).or_insert(u);
                    }
                }
            }
        }
    }
    *KEY_MAP.lock().unwrap_or_else(|e| e.into_inner()) = Some((Instant::now(), mapping.clone()));
    mapping
}
