//! Admin reads (admin_routes.py, admin_users.py, admin_config.py,
//! admin_storage.py): the administration console's data. Verified on the
//! isolated parity bench (synthetic database, fictitious admin), since no real
//! admin session may be used for tests.
#![allow(unused_imports)]
use super::*;
use axum::http::Uri;
use crate::rowjson;

/// `admin_required`: signed out → 401 on /api/, else the login page; not an
/// admin → 403 on /api/, else the home page.
pub(crate) async fn admin_guard(state: &AppState, headers: &HeaderMap, path: &str) -> Result<auth::SessionData, Response> {
    let api = path.starts_with("/api/");
    match auth::require_session(headers, &state.db).await {
        Err(auth::AuthError::Unauthorized) => Err(if api {
            auth::html_status(StatusCode::UNAUTHORIZED)
        } else {
            (StatusCode::FOUND, [(axum::http::header::LOCATION, "/login"), (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], auth::werkzeug_redirect_body("/login")).into_response()
        }),
        Err(e) => Err(e.into_response()),
        Ok(s) if !s.is_admin => Err(if api {
            auth::html_status(StatusCode::FORBIDDEN)
        } else {
            (StatusCode::FOUND, [(axum::http::header::LOCATION, "/"), (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], auth::werkzeug_redirect_body("/")).into_response()
        }),
        Ok(s) => Ok(s),
    }
}

fn utc_iso_now() -> String {
    chrono::Utc::now().naive_utc().format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
}

/// `get_setting(key, default)`: the stored string, else the default value as Python has it.
pub(crate) async fn setting_or(state: &AppState, key: &str, default: Value) -> Value {
    match state.db.get_setting(key).await.ok().flatten() {
        Some(v) => json!(v),
        None => default,
    }
}

pub(crate) fn key_budget_default() -> Value {
    let b: f64 = std::env::var("KEY_MAX_BUDGET").ok().and_then(|v| v.parse().ok()).unwrap_or(200_000_000.0);
    json!(b)
}

pub(crate) fn key_duration_default() -> Value {
    json!(std::env::var("KEY_BUDGET_DURATION").unwrap_or_else(|_| "7d".to_string()))
}

/// `runner_logs(n)`: the runner's buffer without the probe noise, last `n` lines.
pub(crate) async fn runner_logs(n: usize) -> Vec<Value> {
    static NOISE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let noise = NOISE.get_or_init(|| regex::Regex::new(r#""GET /(?:v1/models|metrics|health\S*|version|ping)\b"#).expect("static regex"));
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let fetched = async {
        let c = reqwest::Client::builder().timeout(std::time::Duration::from_secs(5)).build().ok()?;
        let r = c.get(format!("{}/logs", crate::probes::runner_url())).bearer_auth(token).query(&[("n", "2000")]).send().await.ok()?;
        if !r.status().is_success() {
            return None;
        }
        r.json::<Value>().await.ok()
    };
    let lines: Vec<Value> = fetched.await.and_then(|v| v.get("logs").and_then(Value::as_array).cloned()).unwrap_or_default()
        .into_iter().filter(|l| !l.as_str().is_some_and(|s| noise.is_match(s))).collect();
    lines[lines.len().saturating_sub(n)..].to_vec()
}

/// `admin_get_user_consumption`: accounts with keys, their LiteLLM envelope, today's tokens.
pub(crate) async fn user_consumption(state: &AppState) -> Vec<Value> {
    let counts: Vec<(String, i64)> = sqlx::query_as("SELECT username, COUNT(*) c FROM api_keys GROUP BY username").fetch_all(state.db.pool()).await.unwrap_or_default();
    let counts: std::collections::HashMap<String, i64> = counts.into_iter().collect();
    let mut users: Vec<(String, Value)> = Vec::new();
    let base = std::env::var("LITELLM_URL").unwrap_or_else(|_| "http://litellm:4000".to_string());
    let listed = async {
        let c = reqwest::Client::builder().timeout(std::time::Duration::from_secs(6)).build().ok()?;
        let r = c.get(format!("{}/user/list", base.trim_end_matches('/'))).bearer_auth(std::env::var("LITELLM_MASTER_KEY").unwrap_or_default()).query(&[("page_size", "100")]).send().await.ok()?;
        if !r.status().is_success() {
            return None;
        }
        r.json::<Value>().await.ok()
    };
    for u in listed.await.and_then(|v| v.get("users").and_then(Value::as_array).cloned()).unwrap_or_default() {
        let Some(uid) = u.get("user_id").and_then(Value::as_str) else { continue };
        let Some(n) = counts.get(uid) else { continue };
        let mb = u.get("max_budget").cloned().unwrap_or(Value::Null);
        let spend = u.get("spend").filter(|v| crate::keys::truthy(v)).cloned().unwrap_or(json!(0));
        let entry = json!({"username": uid, "spend": spend, "max_budget": if mb.is_null() { json!(0) } else { mb.clone() }, "unlimited": mb.is_null(), "key_count": n});
        match users.iter_mut().find(|(k, _)| k == uid) {
            Some(e) => e.1 = entry,
            None => users.push((uid.to_string(), entry)),
        }
    }
    let mut names: Vec<&String> = counts.keys().collect();
    names.sort();
    for name in names {
        if !users.iter().any(|(k, _)| k == name) {
            users.push((name.clone(), json!({"username": name, "spend": 0, "max_budget": 0, "unlimited": false, "key_count": counts[name]})));
        }
    }
    let day_start = chrono::Utc::now().date_naive().and_hms_opt(0, 0, 0).unwrap_or_default();
    let toks = match state.spend.as_ref() {
        Some(pool) => tokens_by_user(pool, day_start).await,
        None => Default::default(),
    };
    let mut out: Vec<Value> = users.into_iter().map(|(k, mut v)| {
        v["tokens"] = json!(toks.get(&k).copied().unwrap_or(0));
        v
    }).collect();
    out.sort_by(|a, b| b["tokens"].as_i64().cmp(&a["tokens"].as_i64()));
    out
}

/// `_real_tokens_by_user(since)`: prompt + generated tokens per account.
pub(crate) async fn tokens_by_user(pool: &sqlx::PgPool, since: chrono::NaiveDateTime) -> std::collections::HashMap<String, i64> {
    let map = crate::spend::key_user_map(pool).await;
    let rows: Vec<(Option<String>, Option<i64>)> = sqlx::query_as(
        "SELECT api_key, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0))::bigint FROM \"LiteLLM_SpendLogs\" WHERE \"startTime\" >= $1 GROUP BY api_key",
    ).bind(since).fetch_all(pool).await.unwrap_or_default();
    let mut out = std::collections::HashMap::new();
    for (k, n) in rows {
        let k = k.unwrap_or_default();
        if crate::spend::NON_USER_KEYS.contains(&k.as_str()) {
            continue;
        }
        if let Some(u) = map.get(&k) {
            *out.entry(u.clone()).or_insert(0) += n.unwrap_or(0);
        }
    }
    out
}

/// GET /api/admin - the console's dashboard data
pub async fn api_admin(State(state): State<AppState>, uri: Uri, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, uri.path()).await {
        return r;
    }
    let pool = state.db.pool();
    let read = async {
        let requests = rowjson::rows(pool, "SELECT * FROM model_requests ORDER BY created_at DESC LIMIT 200").await?;
        let model_cfgs = rowjson::rows(pool, "SELECT * FROM model_configs ORDER BY name").await?;
        let ocr_cfgs = rowjson::rows(pool, "SELECT * FROM ocr_configs ORDER BY name").await?;
        let voice_cfgs = rowjson::rows(pool, "SELECT * FROM voice_configs ORDER BY name").await?;
        let budget_reqs = rowjson::rows(pool, "SELECT * FROM budget_requests ORDER BY created_at DESC LIMIT 200").await?;
        let (p, d, r): (Option<i64>, Option<i64>, Option<i64>) = sqlx::query_as("SELECT SUM(status='pending'), SUM(status='done'), SUM(status='rejected') FROM model_requests").fetch_one(pool).await?;
        let bp: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM budget_requests WHERE status='pending'").fetch_one(pool).await?;
        let grants: Vec<Value> = sqlx::query("SELECT * FROM budget_grants WHERE expires_at > ? ORDER BY expires_at").bind(utc_iso_now()).fetch_all(pool).await?.iter().map(rowjson::row_dict).collect();
        Ok::<_, sqlx::Error>((requests, model_cfgs, ocr_cfgs, voice_cfgs, budget_reqs, (p, d, r, bp), grants))
    };
    let Ok((requests, model_cfgs, ocr_cfgs, voice_cfgs, budget_reqs, (p, d, r, bp), grants)) = read.await else {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    };
    let shown = requests.len();
    let st = crate::chat::support_tools::sidecar_status;
    let asr = media::asr_info();
    let (running, spend, ocr_s, ocr_m, video_s, voice_s, voice_m, asr_s, asr_i, image_s, image_m, v_status, logs, decisions) = tokio::join!(
        models::running_models(), user_consumption(&state), st("ocr"), sidecars::ocr_model(), st("video"), st("voice"), sidecars::voice_model(),
        st("asr"), asr, st("image"), sidecars::image_model(), crate::system::runner_status(), runner_logs(120),
        // Its own engine, its own port: the admin sees it next to the others.
        crate::decisions::status()
    );
    let loaded = asr_i.get("loaded").is_some_and(keys::truthy);
    let mut out = json!({
        "requests": requests,
        "stats": {"pending": p.unwrap_or(0), "done": d.unwrap_or(0), "rejected": r.unwrap_or(0), "budget_pending": bp, "requests_shown": shown},
        "maintenance_mode": state.db.maintenance_active().await,
        "model_cfgs": model_cfgs,
        "ocr_cfgs": ocr_cfgs,
        "voice_cfgs": voice_cfgs,
        "image_model_ids": ["black-forest-labs/FLUX.2-klein-4B"],
        "budget_reqs": budget_reqs,
        "budget_grants": grants,
        "default_key_budget": setting_or(&state, "default_key_budget", key_budget_default()).await,
        "default_key_duration": setting_or(&state, "default_key_duration", key_duration_default()).await,
    });
    let extra = json!({
        "running_models": running, "spend_data": spend, "ocr_status": ocr_s, "ocr_model_name": ocr_m, "video_status": video_s,
        "voice_status": voice_s, "voice_model_name": voice_m, "asr_status": asr_s,
        "asr_model_name": if loaded { asr_i.get("type").cloned().unwrap_or(Value::Null) } else { Value::Null },
        "asr_load_error": if loaded { Value::Null } else { asr_i.get("error").filter(|v| keys::truthy(v)).cloned().unwrap_or(Value::Null) },
        "image_status": image_s, "image_model_name": image_m, "v_status": v_status, "init_logs": logs,
        "decisions": decisions,
    });
    if let (Some(o), Some(e)) = (out.as_object_mut(), extra.as_object()) {
        o.extend(e.clone());
    }
    Json(out).into_response()
}

/// GET /admin/audit - the latest sensitive events (?username= filters by actor)
pub async fn admin_audit(State(state): State<AppState>, uri: Uri, headers: HeaderMap, Query(q): Query<HashMap<String, String>>) -> Response {
    if let Err(r) = admin_guard(&state, &headers, uri.path()).await {
        return r;
    }
    let user = q.get("username").map(|u| crate::memory::py_strip(u).to_string()).unwrap_or_default();
    let rows = if user.is_empty() {
        sqlx::query("SELECT id, username, action, detail, created_at FROM audit_log ORDER BY id DESC LIMIT 100").fetch_all(state.db.pool()).await
    } else {
        sqlx::query("SELECT id, username, action, detail, created_at FROM audit_log WHERE username=? ORDER BY id DESC LIMIT 100").bind(&user).fetch_all(state.db.pool()).await
    };
    match rows {
        Ok(r) => Json(Value::Array(r.iter().map(rowjson::row_dict).collect())).into_response(),
        Err(_) => auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
}

/// GET /admin/support/feedback - the Support's votes: totals, per account, latest
pub async fn admin_support_feedback(State(state): State<AppState>, uri: Uri, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, uri.path()).await {
        return r;
    }
    let pool = state.db.pool();
    let read = async {
        let (c, p): (i64, Option<i64>) = sqlx::query_as("SELECT COUNT(*) c, SUM(CASE WHEN vote>0 THEN 1 ELSE 0 END) p FROM support_feedback").fetch_one(pool).await?;
        let by_user = rowjson::rows(pool, "SELECT username, COUNT(*) c, SUM(CASE WHEN vote>0 THEN 1 ELSE 0 END) p FROM support_feedback GROUP BY username ORDER BY c DESC LIMIT 20").await?;
        let recent = rowjson::rows(pool, "SELECT username, vote, comment, question, answer, model, created_at FROM support_feedback ORDER BY id DESC LIMIT 50").await?;
        Ok::<_, sqlx::Error>((c, p, by_user, recent))
    };
    match read.await {
        Ok((c, p, by_user, recent)) => Json(json!({"total": c, "positive": p.unwrap_or(0), "par_utilisateur": by_user, "recent": recent})).into_response(),
        Err(_) => auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
}

/// `setting(name)`: the stored `cfg_<name>`, else the code default.
async fn instance_setting(state: &AppState, name: &str, default: &str) -> String {
    state.db.get_config(name).await.filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

/// GET /admin/email/config - SMTP state, never the password
pub async fn admin_email_config(State(state): State<AppState>, uri: Uri, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, uri.path()).await {
        return r;
    }
    let host = instance_setting(&state, "smtp_host", "").await;
    let user = instance_setting(&state, "smtp_user", "").await;
    let pass = instance_setting(&state, "smtp_pass", "").await;
    let from = instance_setting(&state, "smtp_from", "").await;
    let admin_email = instance_setting(&state, "admin_email", "").await;
    let configured = !host.is_empty() && !user.is_empty() && !pass.is_empty() && !admin_email.is_empty();
    static ADDR: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let sender = ADDR.get_or_init(|| regex::Regex::new(r"<([^>]+)>").expect("static regex")).captures(&from).map(|c| c[1].to_string()).unwrap_or(user);
    Json(json!({"configured": configured, "admin_email": admin_email, "sender": sender, "smtp_host": host})).into_response()
}

/// GET /admin/consumption - per-account consumption
pub async fn admin_consumption(State(state): State<AppState>, uri: Uri, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, uri.path()).await {
        return r;
    }
    Json(json!({"users": user_consumption(&state).await})).into_response()
}

/// GET /admin/runner/logs - the runner's log, probe noise out
pub async fn admin_runner_logs(State(state): State<AppState>, uri: Uri, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, uri.path()).await {
        return r;
    }
    Json(json!({"logs": runner_logs(200).await})).into_response()
}

/// GET /admin/sidecar-logs/:kind - a sidecar's log tail, read by the runner
pub async fn admin_sidecar_logs(State(state): State<AppState>, uri: Uri, headers: HeaderMap, Path(kind): Path<String>) -> Response {
    if let Err(r) = admin_guard(&state, &headers, uri.path()).await {
        return r;
    }
    if !["ocr", "voice", "image", "video", "asr"].contains(&kind.as_str()) {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "unknown kind", "logs": []}))).into_response();
    }
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let fetched = async {
        let c = reqwest::Client::builder().timeout(std::time::Duration::from_secs(10)).build().map_err(|_| ())?;
        c.get(format!("{}/{kind}/logs", crate::probes::runner_url())).bearer_auth(token).send().await.map_err(|_| ())
    };
    match fetched.await {
        Ok(r) if r.status().is_success() => match r.json::<Value>().await {
            Ok(v) => Json(json!({"logs": v.get("logs").cloned().unwrap_or(json!([]))})).into_response(),
            Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"logs": [], "error": "runner unreachable"}))).into_response(),
        },
        Ok(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"logs": [], "error": "runner error"}))).into_response(),
        Err(()) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"logs": [], "error": "runner unreachable"}))).into_response(),
    }
}
