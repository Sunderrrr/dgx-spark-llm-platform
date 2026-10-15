//! The home dashboard's data, parity with dgx-portal/app.py `_index_data`
//! (route /api/home) and the stats.py helpers it calls.
//!
//! Everything here READS. Two Flask helpers also write as a side effect and
//! stay Flask's job while it serves the dashboard: `_inflight_snapshot`
//! sweeps in-flight rows older than 15 min (here they are skipped, not
//! deleted), and the token total (see health.rs).
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use sqlx::PgPool;

use crate::db::Database;
use crate::{health, probes, spend};

type Cache = Mutex<Option<HashMap<String, (Instant, Value)>>>;

fn cached(cache: &Cache, key: &str, ttl: Duration) -> Option<Value> {
    let guard = cache.lock().unwrap_or_else(|e| e.into_inner());
    guard.as_ref()?.get(key).filter(|(at, _)| at.elapsed() < ttl).map(|(_, v)| v.clone())
}

fn store(cache: &Cache, key: &str, v: &Value) {
    let mut guard = cache.lock().unwrap_or_else(|e| e.into_inner());
    guard.get_or_insert_with(HashMap::new).insert(key.to_string(), (Instant::now(), v.clone()));
}

static HOURLY: Cache = Mutex::new(None);
static BY_MODEL: Cache = Mutex::new(None);
static RUNNER: Cache = Mutex::new(None);
static BUDGET: Cache = Mutex::new(None);

/// stats.user_hourly: today's 24 hourly points (prompt + generated tokens) of
/// one account, cached 30 s. None without the LiteLLM database.
pub async fn user_hourly(pool: Option<&PgPool>, username: &str) -> Value {
    if let Some(v) = cached(&HOURLY, username, Duration::from_secs(30)) {
        return v;
    }
    let Some(pool) = pool else { return Value::Null };
    let empty = json!({
        "has_data": false, "points": (0..24).map(|h| json!({"hour": h, "tokens": 0})).collect::<Vec<_>>(),
        "total": 0, "peak_hour": 0, "peak_val": 0, "active_keys": 0,
    });
    let umap = spend::key_user_map(pool).await;
    let my_keys: Vec<String> = umap.iter().filter(|(_, u)| u.as_str() == username).map(|(k, _)| k.clone()).collect();
    if my_keys.is_empty() {
        store(&HOURLY, username, &empty);
        return empty;
    }
    let tz = spend::local_tz();
    let rows: Result<Vec<(i32, Option<String>, Option<i64>)>, _> = sqlx::query_as(
        "SELECT EXTRACT(HOUR FROM ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE $1))::int AS h, \
         api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0))::bigint \
         FROM \"LiteLLM_SpendLogs\" WHERE api_key = ANY($2) \
         AND ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date \
         GROUP BY h, api_key",
    )
    .bind(&tz)
    .bind(&my_keys)
    .fetch_all(pool)
    .await;
    let Ok(rows) = rows else { return empty };
    let mut by_hour = [0i64; 24];
    let mut active = std::collections::HashSet::new();
    for (h, api_key, toks) in rows {
        let toks = toks.unwrap_or(0);
        by_hour[h.clamp(0, 23) as usize] += toks;
        if toks != 0 {
            active.insert(api_key);
        }
    }
    // max() keeps the first hour reaching the peak.
    let peak_hour = (0..24).fold(0usize, |best, h| if by_hour[h] > by_hour[best] { h } else { best });
    let total: i64 = by_hour.iter().sum();
    let result = json!({
        "has_data": total > 0,
        "points": (0..24).map(|h| json!({"hour": h, "tokens": by_hour[h]})).collect::<Vec<_>>(),
        "total": total, "peak_hour": peak_hour, "peak_val": by_hour[peak_hour], "active_keys": active.len(),
    });
    store(&HOURLY, username, &result);
    result
}

/// stats._tokens_by_model: real tokens per model over all time, cached 30 s.
pub async fn tokens_by_model(pool: Option<&PgPool>) -> Value {
    if let Some(v) = cached(&BY_MODEL, "", Duration::from_secs(30)) {
        return v;
    }
    let Some(pool) = pool else { return json!([]) };
    let rows: Result<Vec<(Option<String>, Option<i64>)>, _> = sqlx::query_as(
        "SELECT model, t::bigint FROM (SELECT model, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) AS t \
         FROM \"LiteLLM_SpendLogs\" GROUP BY model ORDER BY 2 DESC) s",
    )
    .fetch_all(pool)
    .await;
    let Ok(rows) = rows else { return json!([]) };
    let result: Value = rows
        .into_iter()
        .map(|(m, t)| {
            let m = m.filter(|m| !m.is_empty()).unwrap_or_else(|| "unknown".to_string());
            json!({"model": m, "tokens": t.unwrap_or(0)})
        })
        .collect();
    store(&BY_MODEL, "", &result);
    result
}

/// sidecars.runner_metrics: host CPU/RAM/GPU from the runner, cached 3 s.
pub async fn runner_metrics() -> Value {
    if let Some(v) = cached(&RUNNER, "", Duration::from_secs(3)) {
        return v;
    }
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let out = async {
        let c = reqwest::Client::builder().timeout(Duration::from_secs(5)).build().ok()?;
        let r = c.get(format!("{}/metrics", probes::runner_url())).bearer_auth(token).send().await.ok()?;
        if !r.status().is_success() {
            return None;
        }
        r.json::<Value>().await.ok()
    }
    .await
    .unwrap_or(Value::Null);
    store(&RUNNER, "", &out);
    out
}

/// litellm_client._litellm_user_info: the account's LiteLLM envelope.
async fn litellm_user_info(username: &str) -> (Option<Value>, String) {
    let key = std::env::var("LITELLM_MASTER_KEY").unwrap_or_default();
    let info = async {
        let c = reqwest::Client::builder().timeout(Duration::from_secs(5)).build().ok()?;
        let r = c
            .get(format!("{}/user/info", probes::litellm_url()))
            .bearer_auth(key)
            .query(&[("user_id", username)])
            .send()
            .await
            .ok()?;
        if !r.status().is_success() {
            return None;
        }
        let d: Value = r.json().await.ok()?;
        let ui = d.get("user_info").filter(|v| !v.is_null() && *v != &json!({})).cloned().unwrap_or(d);
        (!ui.is_null() && ui != json!({})).then_some(ui)
    }
    .await;
    let reset = info
        .as_ref()
        .and_then(|ui| ui.get("budget_reset_at"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let max_budget = info.as_ref().and_then(|ui| ui.get("max_budget")).filter(|v| !v.is_null()).cloned();
    (max_budget, reset)
}

/// app._budget_period_days: « 1d », « 7d », « 30d », « 3 mois »… Pure.
pub fn budget_period_days(duration: &str) -> i64 {
    let s = duration.to_lowercase();
    if s.contains("mois") || s.contains("month") {
        return 30;
    }
    let digits: String = s.chars().filter(char::is_ascii_digit).collect();
    if digits.is_empty() {
        return 1;
    }
    digits.parse::<i64>().map(|n| n.max(1)).unwrap_or(1)
}

/// f"{x:,.0f}".replace(',', ' '): ties-to-even like Python, spaces between thousands. Pure.
pub fn thousands(x: f64) -> String {
    let s = format!("{x:.0}");
    let (sign, digits) = s.strip_prefix('-').map_or(("", s.as_str()), |d| ("-", d));
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(' ');
        }
        out.push(ch);
    }
    format!("{sign}{out}")
}

/// app._budget_remaining: (used, remaining) real tokens over the budget window, cached 60 s.
pub(crate) async fn budget_remaining(pool: Option<&PgPool>, username: &str, budget: f64, days: i64) -> (i64, i64) {
    if let Some(v) = cached(&BUDGET, username, Duration::from_secs(60)) {
        return (v[0].as_i64().unwrap_or(0), v[1].as_i64().unwrap_or(0));
    }
    let mut used = 0i64;
    if let Some(pool) = pool {
        let umap = spend::key_user_map(pool).await;
        let rows: Vec<(Option<String>, Option<i64>)> = sqlx::query_as(
            "SELECT api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0))::bigint \
             FROM \"LiteLLM_SpendLogs\" WHERE \"startTime\" >= (now() AT TIME ZONE 'UTC') - make_interval(days => $1) \
             GROUP BY api_key",
        )
        .bind(days as i32)
        .fetch_all(pool)
        .await
        .unwrap_or_default();
        for (api_key, toks) in rows {
            let Some(k) = api_key else { continue };
            if spend::NON_USER_KEYS.contains(&k.as_str()) {
                continue;
            }
            if umap.get(&k).map(String::as_str) == Some(username) {
                used += toks.unwrap_or(0);
            }
        }
    }
    let remaining = ((budget - used as f64) as i64).max(0);
    store(&BUDGET, username, &json!([used, remaining]));
    (used, remaining)
}

/// The account's quota when LiteLLM has no envelope: user override → group → default.
async fn local_effective_budget(db: &Database, username: &str, default: f64) -> f64 {
    let row: Option<(Option<f64>, Option<f64>)> = sqlx::query_as(
        "SELECT CAST(u.max_budget AS REAL), CAST(g.max_budget AS REAL) FROM local_users u \
         LEFT JOIN user_groups g ON g.name = u.group_name WHERE u.username = ?",
    )
    .bind(username)
    .fetch_optional(db.pool())
    .await
    .ok()
    .flatten();
    match row {
        Some((Some(own), _)) => own,
        Some((None, Some(group))) => group,
        _ => default,
    }
}

/// stats._account_exists: known to user_prefs (directory accounts included).
async fn account_exists(db: &Database, name: &str) -> bool {
    !name.is_empty()
        && sqlx::query_as::<_, (i64,)>("SELECT 1 FROM user_prefs WHERE username = ? LIMIT 1")
            .bind(name)
            .fetch_optional(db.pool())
            .await
            .ok()
            .flatten()
            .is_some()
}

/// stats._account_from_alias: a fragment of the key alias that IS an account, else ''.
async fn account_from_alias(db: &Database, alias: &str) -> String {
    for chunk in alias.split(['-', '_']) {
        if !chunk.is_empty() && account_exists(db, chunk).await {
            return chunk.to_string();
        }
    }
    String::new()
}

/// stats._inflight: {account: age} from the LiteLLM callback's database
/// (read-only), the OLDEST request of an account winning.
async fn inflight_by_callback(db: &Database) -> Vec<(String, f64)> {
    use sqlx::sqlite::SqliteConnectOptions;
    use sqlx::{ConnectOptions, Connection};
    let path = std::env::var("DGX_INFLIGHT_DB").unwrap_or_else(|_| "/run/dgx/inflight.db".to_string());
    let Ok(mut conn) = SqliteConnectOptions::new().filename(&path).read_only(true).connect().await else {
        return Vec::new();
    };
    let rows: Vec<(Option<String>, Option<String>, Option<f64>)> =
        sqlx::query_as("SELECT alias, CAST(user_id AS TEXT), CAST(started_at AS REAL) FROM inflight")
            .fetch_all(&mut conn)
            .await
            .unwrap_or_default();
    let _ = conn.close().await;
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    let mut out: Vec<(String, f64)> = Vec::new();
    for (alias, user_id, start) in rows {
        let mut u = account_from_alias(db, alias.as_deref().unwrap_or_default()).await;
        if u.is_empty() {
            if let Some(id) = user_id.filter(|id| !id.is_empty()) {
                if account_exists(db, &id).await {
                    u = id;
                }
            }
        }
        if u.is_empty() {
            continue;
        }
        let age = start.map(|s| (now - s).max(0.0)).unwrap_or(0.0);
        match out.iter_mut().find(|(name, _)| *name == u) {
            Some((_, a)) if age > *a => *a = age,
            Some(_) => {}
            None => out.push((u, age)),
        }
    }
    out
}

/// stats._active_users: who used the model in the last 30 min (admin panel).
pub async fn active_users(db: &Database, pool: Option<&PgPool>, model_health: &Value) -> Value {
    let window_s = 1800.0;
    // Portal in-flight requests: rows older than 15 min are stale (Flask sweeps them).
    let snapshot: Vec<(String, i64)> = sqlx::query_as(
        "SELECT username, COUNT(*) FROM inflight_requests WHERE started_at >= ? GROUP BY username",
    )
    .bind(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0) - 900.0)
    .fetch_all(db.pool())
    .await
    .unwrap_or_default();
    let busy = model_health["running"].as_i64().unwrap_or(0) + model_health["waiting"].as_i64().unwrap_or(0);
    if snapshot.is_empty() && busy == 0 {
        return json!([]);
    }
    // Entries in first-seen order: (username, requests, tokens, live, last_s, inflight, since_s)
    let mut agg: Vec<(String, i64, i64, bool, Option<f64>, bool, Option<f64>)> = Vec::new();
    fn entry<'a>(
        agg: &'a mut Vec<(String, i64, i64, bool, Option<f64>, bool, Option<f64>)>,
        u: &str,
    ) -> &'a mut (String, i64, i64, bool, Option<f64>, bool, Option<f64>) {
        let i = match agg.iter().position(|a| a.0 == u) {
            Some(i) => i,
            None => {
                agg.push((u.to_string(), 0, 0, false, None, false, None));
                agg.len() - 1
            }
        };
        &mut agg[i]
    }
    if let Some(pool) = pool {
        let umap = spend::key_user_map(pool).await;
        // Bounded on startTime (indexed) one hour wider, then on endTime: a row
        // only lands at the END of a request. Flask's SQL, verbatim inside.
        let rows: Vec<(Option<String>, i64, Option<i64>, Option<String>, Option<String>, Option<f64>)> = sqlx::query_as(
            "SELECT api_key, n, t::bigint, u, a, EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'UTC') - l))::float8 FROM (\
             SELECT api_key, COUNT(*) AS n, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) AS t, \
             MAX(COALESCE(\"user\", '')) AS u, MAX(COALESCE(metadata->>'user_api_key_alias', '')) AS a, \
             MAX(COALESCE(\"endTime\", \"startTime\")) AS l FROM \"LiteLLM_SpendLogs\" \
             WHERE \"startTime\" >= (now() AT TIME ZONE 'UTC') - make_interval(secs => $1 + 3600) \
             AND COALESCE(\"endTime\", \"startTime\") >= (now() AT TIME ZONE 'UTC') - make_interval(secs => $1) \
             GROUP BY api_key) s",
        )
        .bind(window_s)
        .fetch_all(pool)
        .await
        .unwrap_or_default();
        for (api_key, cnt, toks, col_user, alias, age) in rows {
            if api_key.as_deref().is_some_and(|k| spend::NON_USER_KEYS.contains(&k)) {
                continue;
            }
            let key = api_key.clone().unwrap_or_else(|| "None".to_string());
            let mut u = api_key.as_ref().and_then(|k| umap.get(k)).cloned().unwrap_or_default();
            if u.is_empty() {
                u = col_user.unwrap_or_default().trim().to_string();
            }
            if u.is_empty() {
                if let Some(a) = alias.filter(|a| !a.is_empty()) {
                    u = account_from_alias(db, &a).await;
                }
            }
            if u.is_empty() {
                u = format!("key {}…", key.chars().take(8).collect::<String>());
            }
            let a = entry(&mut agg, &u);
            a.1 += cnt;
            a.2 += toks.unwrap_or(0);
            if let Some(age) = age {
                if a.4.is_none_or(|l| age < l) {
                    a.4 = Some(age.max(0.0));
                }
            }
            // The engine works AND this account just emitted (< 15 s).
            if busy > 0 && a.4.is_some_and(|l| l < 15.0) {
                a.3 = true;
            }
        }
    }
    for (u, n) in snapshot {
        let a = entry(&mut agg, &u);
        a.3 = true;
        a.4 = Some(0.0);
        if a.1 == 0 {
            a.1 = n;
        }
    }
    for (u, age) in inflight_by_callback(db).await {
        let a = entry(&mut agg, &u);
        a.3 = true;
        a.5 = true;
        a.6 = Some(age);
        a.4 = Some(0.0);
    }
    // sorted(key=(live, requests), reverse=True): stable, ties keep their order.
    agg.sort_by(|x, y| (y.3, y.1).cmp(&(x.3, x.1)));
    agg.into_iter()
        .map(|(u, requests, tokens, live, last_s, inflight, since_s)| {
            json!({"username": u, "requests": requests, "tokens": tokens, "live": live,
                   "last_s": last_s, "inflight": inflight, "since_s": since_s})
        })
        .collect()
}

/// app._index_data: the whole /api/home payload for one viewer.
pub async fn index_data(db: &Database, pool: Option<&PgPool>, username: &str, is_admin: bool) -> Value {
    let (running, sidecar_metrics) = crate::sidecars::running_and_metrics(db).await;
    let my_requests: Vec<Value> = sqlx::query_as::<_, (i64, String, String, String, Option<String>, Option<String>, String, Option<String>)>(
        "SELECT id, username, fullname, model_id, reason, status, created_at, updated_at FROM model_requests \
         WHERE username = ? ORDER BY created_at DESC LIMIT 5",
    )
    .bind(username)
    .fetch_all(db.pool())
    .await
    .unwrap_or_default()
    .into_iter()
    .map(|(id, u, f, m, r, s, c, up)| {
        json!({"id": id, "username": u, "fullname": f, "model_id": m, "reason": r, "status": s,
               "created_at": c, "updated_at": up})
    })
    .collect();
    let key_budget: f64 = std::env::var("KEY_MAX_BUDGET").ok().and_then(|v| v.parse().ok()).unwrap_or(200_000_000.0);
    let default_budget: f64 = db
        .get_setting("default_key_budget")
        .await
        .ok()
        .flatten()
        .and_then(|v| v.parse().ok())
        .unwrap_or(key_budget);
    let duration = db
        .get_setting("default_key_duration")
        .await
        .ok()
        .flatten()
        .unwrap_or_else(|| std::env::var("KEY_BUDGET_DURATION").unwrap_or_else(|_| "7d".to_string()));
    // The envelope that ACTUALLY blocks is LiteLLM's; the portal's own figure
    // is the fallback for an account LiteLLM does not know yet.
    let (litellm_max, reset_at) = litellm_user_info(username).await;
    let effective = match litellm_max.as_ref().and_then(Value::as_f64) {
        Some(v) => v,
        None => local_effective_budget(db, username, default_budget).await,
    };
    let (used, remaining) = budget_remaining(pool, username, effective, budget_period_days(&duration)).await;
    let reset_at: String = reset_at.chars().take(16).collect::<String>().replace('T', " ");
    let pending = sqlx::query_as::<_, (i64,)>("SELECT 1 FROM budget_requests WHERE username = ? AND status = 'pending'")
        .bind(username)
        .fetch_optional(db.pool())
        .await
        .ok()
        .flatten()
        .is_some();
    let model_health = health::vllm_health(db, pool).await;
    let (usage, by_model, sysmetrics) =
        tokio::join!(user_hourly(pool, username), tokens_by_model(pool), runner_metrics());
    let active = if is_admin { active_users(db, pool, &model_health).await } else { Value::Null };
    json!({
        "running_models": running,
        "my_requests": my_requests,
        "public_api_url": crate::probes::public_api_url(),
        "auto_model": std::env::var("AUTO_MODEL_NAME").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "auto-model".to_string()),
        "usage": usage,
        "usage_by_model": by_model,
        "sysmetrics": sysmetrics,
        "sidecar_metrics": sidecar_metrics,
        "modelhealth": model_health,
        "active_users": active,
        "budget_tokens": thousands(effective),
        "budget_duration": duration,
        "budget_used": used,
        "budget_remaining": remaining,
        "budget_reset_at": reset_at,
        "budget_request_pending": pending,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn budget_periods() {
        assert_eq!(budget_period_days("7d"), 7);
        assert_eq!(budget_period_days("1d"), 1);
        assert_eq!(budget_period_days("3 mois"), 30);
        assert_eq!(budget_period_days("weekly"), 1);
        assert_eq!(budget_period_days("0d"), 1);
    }

    #[test]
    fn thousands_like_python() {
        assert_eq!(thousands(200_000_000.0), "200 000 000");
        assert_eq!(thousands(999.0), "999");
        assert_eq!(thousands(1000.0), "1 000");
        assert_eq!(thousands(2.5), "2");
        assert_eq!(thousands(0.0), "0");
    }
}
