//! The viewer's API keys and their budgets, parity with dgx-portal app.py
//! `api_keys` and litellm_client.py `get_user_keys` / `_key_infos`.
//!
//! The keys are returned IN CLEAR, as Flask does: the viewer copies them from
//! here. LiteLLM's key table is read directly (one query, joined on the SHA-256
//! of each key), never through its HTTP `/key/info`, which would put the key in
//! the proxy's access log.
use std::collections::HashMap;
use std::time::Duration;

use chrono::NaiveDateTime;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row, SqlitePool};

/// What LiteLLM knows of one key (`_key_infos`).
#[derive(Debug, Clone)]
pub struct KeyInfo {
    pub spend: f64,
    pub max_budget: Option<f64>,
    pub budget_reset_at: Option<NaiveDateTime>,
    pub last_active: Option<NaiveDateTime>,
}

/// The account's LiteLLM envelope (`_litellm_user_info`).
#[derive(Debug, Clone, Default)]
pub struct AccountInfo {
    pub exists: bool,
    pub spend: f64,
    pub max_budget: Option<f64>,
    pub budget_reset_at: String,
}

/// Python's `datetime.isoformat()` for a naive datetime: microseconds only when non-zero.
pub fn iso(dt: &NaiveDateTime) -> String {
    let base = dt.format("%Y-%m-%dT%H:%M:%S").to_string();
    let micros = dt.and_utc().timestamp_subsec_micros();
    if micros == 0 { base } else { format!("{base}.{micros:06}") }
}

/// Flask's JSON encoding of a datetime: the HTTP date (RFC 822, GMT).
pub fn http_date(dt: &NaiveDateTime) -> String {
    dt.format("%a, %d %b %Y %H:%M:%S GMT").to_string()
}

/// `_litellm_user_info`: the account envelope, read from LiteLLM's `/user/info`.
pub async fn account_info(username: &str) -> AccountInfo {
    let base = std::env::var("LITELLM_URL").unwrap_or_else(|_| "http://litellm:4001".to_string());
    let key = std::env::var("LITELLM_MASTER_KEY").unwrap_or_default();
    let fetched = async {
        let c = reqwest::Client::builder().timeout(Duration::from_secs(5)).build().ok()?;
        let r = c
            .get(format!("{}/user/info", base.trim_end_matches('/')))
            .bearer_auth(key)
            .query(&[("user_id", username)])
            .send()
            .await
            .ok()?;
        if !r.status().is_success() {
            return None;
        }
        r.json::<Value>().await.ok()
    }
    .await;
    let Some(d) = fetched else { return AccountInfo::default() };
    // Python: `d.get('user_info') or d`
    let ui = d.get("user_info").filter(|v| truthy(v)).cloned().unwrap_or(d);
    if !truthy(&ui) {
        return AccountInfo::default();
    }
    AccountInfo {
        exists: true,
        spend: ui.get("spend").and_then(Value::as_f64).unwrap_or(0.0),
        max_budget: ui.get("max_budget").and_then(Value::as_f64),
        budget_reset_at: ui.get("budget_reset_at").and_then(Value::as_str).unwrap_or_default().to_string(),
    }
}

/// Python truthiness of a JSON value (for `x or y`).
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `_key_infos`: LiteLLM's figures for keys given in clear. A missing key has no
/// entry; any database failure answers an empty map (budgets then read as 0).
pub async fn key_infos(pool: Option<&PgPool>, keys: &[String]) -> HashMap<String, KeyInfo> {
    let keys: Vec<&String> = keys.iter().filter(|k| !k.is_empty()).collect();
    let Some(pool) = pool else { return HashMap::new() };
    if keys.is_empty() {
        return HashMap::new();
    }
    let by_hash: HashMap<String, String> = keys
        .iter()
        .map(|k| (hex::encode(Sha256::digest(k.as_bytes())), (*k).clone()))
        .collect();
    let hashes: Vec<String> = by_hash.keys().cloned().collect();
    let rows: Result<Vec<(String, Option<f64>, Option<f64>, Option<NaiveDateTime>, Option<NaiveDateTime>)>, _> =
        sqlx::query_as(
            "SELECT token, spend, max_budget, budget_reset_at, last_active \
             FROM \"LiteLLM_VerificationToken\" WHERE token = ANY($1)",
        )
        .bind(&hashes)
        .fetch_all(pool)
        .await;
    let Ok(rows) = rows else { return HashMap::new() };
    rows.into_iter()
        .filter_map(|(token, spend, max_budget, reset, last)| {
            by_hash.get(&token).map(|clear| {
                (
                    clear.clone(),
                    KeyInfo {
                        spend: spend.unwrap_or(0.0),
                        max_budget,
                        budget_reset_at: reset,
                        last_active: last,
                    },
                )
            })
        })
        .collect()
}

/// `get_user_keys`: the viewer's keys, newest first, with LiteLLM's figures.
pub async fn user_keys(db: &SqlitePool, pg: Option<&PgPool>, username: &str) -> Value {
    let local = match sqlx::query(
        "SELECT key_alias, key_value, created_at FROM api_keys WHERE username = ? ORDER BY created_at DESC",
    )
    .bind(username)
    .fetch_all(db)
    .await
    {
        Ok(rows) => rows,
        Err(_) => return json!([]),
    };
    let values: Vec<String> = local.iter().map(|r| r.get::<String, _>("key_value")).collect();
    let infos = key_infos(pg, &values).await;
    let out: Vec<Value> = local
        .iter()
        .map(|r| {
            let key: String = r.get("key_value");
            let info = infos.get(&key);
            let mut item = Map::new();
            item.insert("key_alias".into(), json!(r.get::<Option<String>, _>("key_alias")));
            item.insert("key".into(), json!(key));
            item.insert("created_at".into(), json!(r.get::<Option<String>, _>("created_at")));
            item.insert("spend".into(), json!(info.map_or(0.0, |i| i.spend)));
            item.insert("max_budget".into(), json!(info.and_then(|i| i.max_budget)));
            // A datetime goes out as an HTTP date; an empty one as ''; a key
            // LiteLLM does not know as null.
            let reset = match info {
                None => Value::Null,
                Some(i) => match &i.budget_reset_at {
                    Some(dt) => json!(http_date(dt)),
                    None => json!(""),
                },
            };
            item.insert("budget_reset_at".into(), reset);
            item.insert("last_active".into(), json!(info.and_then(|i| i.last_active.as_ref()).map(iso)));
            Value::Object(item)
        })
        .collect();
    Value::Array(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;

    #[test]
    fn iso_drops_zero_microseconds_like_python() {
        let d = NaiveDate::from_ymd_opt(2026, 10, 14).unwrap();
        assert_eq!(iso(&d.and_hms_opt(16, 33, 46).unwrap()), "2026-10-14T16:33:46");
        assert_eq!(iso(&d.and_hms_micro_opt(16, 33, 46, 5).unwrap()), "2026-10-14T16:33:46.000005");
    }

    #[test]
    fn datetimes_go_out_as_http_dates() {
        let d = NaiveDate::from_ymd_opt(2026, 10, 14).unwrap().and_hms_opt(0, 0, 0).unwrap();
        assert_eq!(http_date(&d), "Wed, 14 Oct 2026 00:00:00 GMT");
    }

    #[test]
    fn python_truthiness() {
        assert!(!truthy(&json!({})));
        assert!(!truthy(&json!(0)));
        assert!(truthy(&json!("x")));
    }
}
