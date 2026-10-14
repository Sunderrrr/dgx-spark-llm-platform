use std::collections::HashMap;

use axum::{
    extract::{Path, Query, State},
    http::{StatusCode, HeaderMap},
    response::IntoResponse,
    Json,
};
use serde_json::json;
use crate::{AppState, auth, models, probes, ranking};

/// GET /healthz - Minimal public liveness probe
pub async fn healthz() -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "ok": true,
            "time": chrono::Utc::now().timestamp()
        })),
    )
}

/// GET /metrics - Prometheus metrics placeholder
pub async fn metrics() -> impl IntoResponse {
    (
        StatusCode::OK,
        "# HELP portal_requests_total Total HTTP requests\n# TYPE portal_requests_total counter\nportal_requests_total 0\n",
    )
}

/// GET /api/config - Public platform config (branding + SSO + setup status)
/// Parity: dgx-portal/app.py api_config (instance settings are `cfg_<name>`).
pub async fn api_config(State(state): State<AppState>) -> impl IntoResponse {
    let db = &state.db;
    let (metadata_url, client_id, client_secret, name, logo, setup_required) = tokio::join!(
        db.get_config("oidc_metadata_url"),
        db.get_config("oidc_client_id"),
        db.get_config("oidc_client_secret"),
        db.get_setting_set("platform_name"),
        db.get_setting_set("platform_logo"),
        setup_required(db),
    );
    let oidc_enabled = metadata_url.is_some() && client_id.is_some() && client_secret.is_some();
    (
        StatusCode::OK,
        Json(json!({
            "oidc_enabled": oidc_enabled,
            "branding": {
                "name": name.unwrap_or_else(|| "DGX".to_string()),
                "logo": logo.map(|_| "/api/branding/logo"),
            },
            "setup_required": setup_required,
        })),
    )
}

/// owner.setup_required: no owner, no enabled local admin (own flag or group),
/// no directory admin. A database error answers false: never offer the setup
/// form on an instance we could not read.
async fn setup_required(db: &crate::Database) -> bool {
    if db.get_setting_set("owner_username").await.is_some() {
        return false;
    }
    let local_admin = sqlx::query_scalar::<_, i32>(
        "SELECT 1 FROM local_users u LEFT JOIN user_groups g ON g.name = u.group_name \
         WHERE u.enabled = 1 AND (u.is_admin = 1 OR g.is_admin = 1) LIMIT 1",
    )
    .fetch_optional(db.pool())
    .await;
    let directory_admin =
        sqlx::query_scalar::<_, i32>("SELECT 1 FROM user_sources WHERE last_is_admin = 1 LIMIT 1")
            .fetch_optional(db.pool())
            .await;
    matches!((local_admin, directory_admin), (Ok(None), Ok(None)))
}

/// GET /.well-known/security.txt - RFC 9116, parity with app.py security_txt.
pub async fn security_txt(State(state): State<AppState>) -> impl IntoResponse {
    let db = &state.db;
    let (security_contact, admin_email, name) = tokio::join!(
        db.get_config("security_contact"),
        db.get_config("admin_email"),
        db.get_setting_set("platform_name"),
    );
    let Some(contact) = security_contact.or(admin_email) else {
        return (StatusCode::NOT_FOUND, "no security contact configured\n".to_string());
    };
    let contact = if contact.starts_with("https://") || contact.starts_with("mailto:") {
        contact
    } else {
        format!("mailto:{contact}")
    };
    let public_web_url = std::env::var("DGX_WEB")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "https://example.org".to_string());
    let expires = chrono::Utc::now() + chrono::Duration::days(180);
    (
        StatusCode::OK,
        format!(
            "# Report a security issue on {}.\nContact: {}\nExpires: {}\nPreferred-Languages: en, fr\nCanonical: {}/.well-known/security.txt\n",
            name.unwrap_or_else(|| "DGX".to_string()),
            contact,
            expires.format("%Y-%m-%dT00:00:00Z"),
            public_web_url.trim_end_matches('/'),
        ),
    )
}

/// GET /api/branding/logo
pub async fn branding_logo() -> impl IntoResponse {
    (StatusCode::NOT_FOUND, "logo not found")
}

/// GET /api/health - Aggregated service health (auth required)
pub async fn api_health(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(_session) => {}
    }

    // Probe services in parallel
    let (runner_ok, litellm_ok, chat) = tokio::join!(
        probes::probe_runner(),
        probes::probe_litellm(),
        models::running_models(),
    );

    // The media sidecars live on dedicated networks portal-rs does not join
    // yet: their state is UNKNOWN here (null), never reported as stopped.
    (
        StatusCode::OK,
        Json(json!({
            "ok": runner_ok && litellm_ok,
            "services": {
                "runner": { "reachable": runner_ok },
                "litellm": { "reachable": litellm_ok },
                "chat": { "running": chat, "ready": !chat.is_empty() },
                "video": { "ready": null, "on_demand": true },
                "ocr": { "ready": null, "on_demand": true },
                "voice": { "ready": null, "on_demand": true },
                "image": { "ready": null, "on_demand": true },
            },
            "time": chrono::Utc::now().timestamp(),
        })),
    )
    .into_response()
}

/// GET /api/home - Dashboard aggregated state (auth required)
pub async fn api_home(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    let _session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(s) => s,
    };

    // TODO: Fetch real data:
    // - model status from runner :8001/status
    // - throughput from runner or litellm metrics
    // - user's budget from litellm
    // - sidecars status from health checks
    // - user's recent requests from audit/spend logs

    (
        StatusCode::OK,
        Json(json!({
            "model": null,
            "uptime_seconds": 0,
            "tps": 0.0,
            "tps_prefill": null,
            "tps_moyen": null,
            "ttft_ms": 0,
            "requests_in_flight": 0,
            "tokens_generated": 0,
            "tokens_prompt": 0,
            "spend": {
                "budget": 0.0,
                "spent": 0.0,
                "limit": 0.0,
                "period": "weekly"
            },
            "sidecars": {
                "video": { "ready": false, "on_demand": true },
                "ocr": { "ready": false, "on_demand": true },
                "voice": { "ready": false, "on_demand": true },
                "image": { "ready": false, "on_demand": true },
            },
            "my_requests": [],
            "active_users": [],
            "time": chrono::Utc::now().timestamp(),
        })),
    )
    .into_response()
}

/// GET /api/modelhealth - Model health (auth required)
pub async fn api_modelhealth(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(_session) => {}
    }

    (
        StatusCode::OK,
        Json(json!({
            "model": null,
            "requests_processing": 0,
            "tokens_generated": 0,
            "tps": 0.0,
            "ttft_ms": 0,
            "time": chrono::Utc::now().timestamp(),
        })),
    )
    .into_response()
}

/// GET /api/running-models - List running models (auth required)
pub async fn api_running_models(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(_session) => {}
    }

    (
        StatusCode::OK,
        Json(json!({
            // Chat model only: the sidecars are not probed by portal-rs yet.
            "running_models": models::running_models()
                .await
                .into_iter()
                .map(|m| json!({"name": m, "kind": "chat", "exposed": true}))
                .collect::<Vec<_>>(),
        })),
    )
    .into_response()
}

/// GET /api/session - Check if logged in (auth required)
pub async fn api_session(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Ok(_session) => (
            StatusCode::NO_CONTENT,
            [(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")],
            "",
        )
            .into_response(),
        Err(e) => return e.into_response(),
    }
}

/// GET /api/whoami - Current user info (auth required)
pub async fn api_whoami(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => {
            // Parity: app.py api_whoami.
            let (prefs, management, maintenance) = tokio::join!(
                state.db.get_user_prefs(&session.username),
                state.db.get_password_management(&session.username),
                state.db.maintenance_active(),
            );
            let (Ok(prefs), Ok(management)) = (prefs, management) else {
                return (
                    StatusCode::SERVICE_UNAVAILABLE,
                    Json(json!({"ok": false, "error": "Base du portail illisible."})),
                )
                    .into_response();
            };
            (
                StatusCode::OK,
                Json(json!({
                    "username": session.username,
                    "fullname": session.fullname,
                    "is_admin": session.is_admin,
                    "avatar_id": prefs.avatar_id,
                    "theme_id": prefs.theme_id.unwrap_or_else(|| "neutral".to_string()),
                    "lang": prefs.lang.unwrap_or_else(|| "fr".to_string()),
                    "onboarded": prefs.onboarded,
                    "local_account": management.local,
                    "password_managed_by": management.management,
                    "auth_sources": management.sources,
                    "passkey_possible": management.passkey,
                    "maintenance_mode": maintenance,
                })),
            )
            .into_response()
        }
    }
}

/// GET /api/ranking - Leaderboard (auth required)
pub async fn api_ranking(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> impl IntoResponse {
    let session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => session,
    };
    // Validated like Flask: an unknown period or metric falls back rather than
    // answering an error to a page that opens.
    let period = q.get("period").map(String::as_str).filter(|p| ranking::PERIODS.contains(p)).unwrap_or("day");
    let metric = q
        .get("metric")
        .map(String::as_str)
        .filter(|m| ranking::RANKING_METRICS.contains(m))
        .unwrap_or("total");
    let by = q.get("by").map(String::as_str).unwrap_or("account");
    let model: Option<String> = q
        .get("model")
        .map(|m| m.trim().chars().take(120).collect::<String>())
        .filter(|m| !m.is_empty());
    let data = ranking::ranking_for_viewer(
        &state.db,
        state.spend.as_ref(),
        period,
        metric,
        by,
        model.as_deref(),
        &session.username,
        session.is_admin,
    )
    .await;
    (StatusCode::OK, Json(data)).into_response()
}

/// GET /api/ranking/activity - day × hour heatmap, the viewer's (`scope=me`) or the platform's
pub async fn api_ranking_activity(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> impl IntoResponse {
    let session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => session,
    };
    let period = q.get("period").map(String::as_str).unwrap_or("month");
    let me = q.get("scope").map(String::as_str) == Some("me");
    let user = me.then_some(session.username.as_str());
    Json(ranking::activity(state.spend.as_ref(), period, user).await).into_response()
}

/// GET /api/ranking/records - records and streaks, anonymity applied
pub async fn api_ranking_records(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    let session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => session,
    };
    let me = session.username.as_str();
    let mut data = ranking::records(&state.db, state.spend.as_ref(), Some(me)).await;
    let anonymous = state.db.anonymous_accounts().await;
    for key in ["best_days", "longest_streaks", "current_streaks", "firsts"] {
        if let Some(rows) = data[key].as_array_mut() {
            for r in rows {
                let name = r["username"].as_str().unwrap_or_default().to_string();
                r["is_me"] = json!(name == me);
                if anonymous.contains(&name) && name != me && !session.is_admin {
                    r["username"] = serde_json::Value::Null;
                }
            }
        }
    }
    Json(data).into_response()
}

/// GET /api/search - HF model search (auth required)
pub async fn api_search(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"models": []})))
        .into_response()
}

/// GET /system/stats - System statistics (auth required)
pub async fn system_stats(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"memory": {}})))
        .into_response()
}

/// GET /usage/hourly - Hourly usage stats (auth required)
pub async fn usage_hourly(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"usage": []})))
        .into_response()
}

/// GET /api/latency - API latency measurements (auth required)
pub async fn api_latency(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"routes": []})))
        .into_response()
}

/// GET /api/ranking/user/:username - one account's detail: itself, or an admin
pub async fn api_ranking_user(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(username): Path<String>,
    Query(q): Query<HashMap<String, String>>,
) -> impl IntoResponse {
    let session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => session,
    };
    if username != session.username && !session.is_admin {
        return auth::html_status(StatusCode::NOT_FOUND);
    }
    let period = q.get("period").map(String::as_str).unwrap_or("month");
    Json(ranking::user_detail(&state.db, state.spend.as_ref(), &username, period).await).into_response()
}

/// GET /api/ranking/export.csv - the ranking as CSV, admin only (it carries every name)
pub async fn api_ranking_export(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> impl IntoResponse {
    let session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => session,
    };
    if !session.is_admin {
        return auth::html_status(StatusCode::NOT_FOUND);
    }
    let period = q.get("period").map(String::as_str).filter(|p| ranking::PERIODS.contains(p)).unwrap_or("month");
    let metric = q.get("metric").map(String::as_str).unwrap_or("total");
    let by = q.get("by").map(String::as_str).unwrap_or("account");
    let model: Option<String> = q
        .get("model")
        .map(|m| m.trim().chars().take(120).collect::<String>())
        .filter(|m| !m.is_empty());
    let data = ranking::ranking_full(&state.db, state.spend.as_ref(), period, None, metric, model.as_deref(), by).await;
    let (body, name) = ranking::export_csv(&data, period);
    (
        StatusCode::OK,
        [
            (axum::http::header::CONTENT_TYPE, "text/csv; charset=utf-8".to_string()),
            (axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename=\"{name}\"")),
        ],
        body,
    )
        .into_response()
}

/// POST /api/ranking/privacy - appear anonymously (anonymous=1|true|on) or not
pub async fn api_ranking_privacy(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> impl IntoResponse {
    // request.form: a body that is not a form simply has no fields.
    let is_form = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/x-www-form-urlencoded"));
    let form: HashMap<String, String> =
        if is_form { serde_urlencoded::from_bytes(&body).unwrap_or_default() } else { HashMap::new() };
    // CSRF first, like Flask's before_request: 400 even without a session.
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return (StatusCode::BAD_REQUEST, "").into_response(),
        Ok(true) => {}
    }
    let session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => session,
    };
    let anonymous = matches!(form.get("anonymous").map(String::as_str), Some("1" | "true" | "on"));
    if state.db.set_ranking_anonymous(&session.username, anonymous).await.is_err() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"ok": false, "error": "Base du portail illisible."})),
        )
            .into_response();
    }
    Json(json!({"ok": true, "anonymous": anonymous})).into_response()
}

/// GET /docs - OpenAPI docs (auth required)
pub async fn docs(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, "OpenAPI docs")
        .into_response()
}

/// GET / - Index (auth required)
pub async fn index(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, "Portal Rust - Phase 2 (routes structure)")
        .into_response()
}
