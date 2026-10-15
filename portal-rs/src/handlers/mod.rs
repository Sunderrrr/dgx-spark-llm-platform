use std::collections::HashMap;

use sqlx::Row;
use axum::{
    extract::{Path, Query, State},
    http::{StatusCode, HeaderMap},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};
mod memory_routes;
mod skill_routes;
mod user_routes;
mod media_routes;
mod support_routes;
mod preview_routes;
mod image_routes;
mod auth_routes;
mod voice_routes;
mod chat_routes;
mod video_routes;
mod dictation_routes;
mod ocr_routes;
mod mcp_routes;
mod key_routes;
mod admin_routes;
mod admin_writes;
mod admin_budget;
mod admin_groups;
mod admin_accounts;
mod user_requests;
mod admin_catalog;
mod admin_ops;
mod discord_routes;
pub use memory_routes::*;
pub use skill_routes::*;
pub use user_routes::*;
pub use media_routes::*;
pub use support_routes::*;
pub use preview_routes::*;
pub use image_routes::*;
pub use auth_routes::*;
pub use voice_routes::*;
pub use chat_routes::*;
pub use video_routes::*;
pub use dictation_routes::*;
pub use ocr_routes::*;
pub use mcp_routes::*;
pub use key_routes::*;
pub use admin_routes::*;
pub use admin_writes::*;
pub use admin_budget::*;
pub use admin_groups::*;
pub use admin_accounts::*;
pub use admin_catalog::*;
pub use admin_ops::*;
pub use discord_routes::*;
pub use user_requests::{api_model_request, request_model};

use crate::{AppState, auth, guards, s3, conversations, docs, health, hf, home, keys, media, memory, models, probes, ranking, sessions, settings, sharing, sidecars, skillstore, system, userdata};

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

/// GET /metrics - Prometheus exposition, refused to anything that crossed Cloudflare
/// (its `Cf-Connecting-Ip` header), as app.prom_metrics.
pub async fn metrics(State(state): State<AppState>, headers: HeaderMap) -> impl IntoResponse {
    let _ = state;
    if headers.contains_key("cf-connecting-ip") {
        return (StatusCode::FORBIDDEN, [(axum::http::header::CONTENT_TYPE, "text/plain; charset=utf-8")],
                "metrics: accès réservé au réseau local\n").into_response();
    }
    let m = system::runner_metrics().await.unwrap_or_else(|| json!({}));
    (StatusCode::OK, [(axum::http::header::CONTENT_TYPE, "text/plain; charset=utf-8")],
     system::prometheus_text(&m)).into_response()
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

/// GET /api/branding/logo - the deployment's logo, public (the login page shows it)
pub async fn branding_logo(State(state): State<AppState>) -> impl IntoResponse {
    let Some(value) = state.db.get_setting_set("platform_logo").await else {
        return auth::html_status(StatusCode::NOT_FOUND);
    };
    match system::logo_bytes(&value) {
        Some((mime, bytes)) => (
            StatusCode::OK,
            [
                (axum::http::header::CONTENT_TYPE, mime),
                (axum::http::header::CACHE_CONTROL, "public, max-age=3600".to_string()),
            ],
            bytes,
        )
            .into_response(),
        None => auth::html_status(StatusCode::NOT_FOUND),
    }
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

    // Probe services in parallel (app.api_health). Media services are
    // on-demand: their absence does not invalidate the overall health.
    let (runner_ok, litellm_ok, chat, video, ocr, voice, image) = tokio::join!(
        probes::probe_runner(),
        probes::probe_litellm(),
        models::running_models(),
        sidecars::comfyui_up(),
        sidecars::ocr_model(),
        sidecars::voice_model(),
        sidecars::image_ready(),
    );
    (
        StatusCode::OK,
        Json(json!({
            "ok": runner_ok && litellm_ok,
            "services": {
                "runner": { "reachable": runner_ok },
                "litellm": { "reachable": litellm_ok },
                "chat": { "running": chat, "ready": !chat.is_empty() },
                "video": { "ready": video, "on_demand": true },
                "ocr": { "ready": ocr.is_some(), "on_demand": true },
                "voice": { "ready": voice.is_some(), "on_demand": true },
                "image": { "ready": image, "on_demand": true },
            },
            "time": chrono::Utc::now().timestamp(),
        })),
    )
    .into_response()
}

/// GET /api/home - the home dashboard (app._index_data)
pub async fn api_home(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    let session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => session,
    };
    Json(home::index_data(&state.db, state.spend.as_ref(), &session.username, session.is_admin).await).into_response()
}

/// GET /api/modelhealth - health of the served model alone (1 s dashboard poll)
pub async fn api_modelhealth(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = auth::require_session(&headers, &state.db).await {
        return e.into_response();
    }
    Json(health::vllm_health(&state.db, state.spend.as_ref()).await).into_response()
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
        Json(json!({"running_models": sidecars::running_and_metrics(&state.db).await.0})),
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

/// GET /api/search - model search on Hugging Face (app.api_search). An outage
/// answers 503 with `ok: false`, never an empty list.
pub async fn api_search(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> impl IntoResponse {
    if let Err(e) = auth::require_session(&headers, &state.db).await {
        return e.into_response();
    }
    let query = q.get("q").map(|v| v.trim().to_string()).unwrap_or_default();
    let task = q.get("task").map(|v| v.trim().to_string()).unwrap_or_default();
    // The GB10 filter is opt-in; `all=1` is accepted and does nothing.
    let gb10 = q.get("gb10").map(String::as_str) == Some("1");
    let skip: u32 = q.get("skip").and_then(|v| v.parse::<i64>().ok()).map(|v| v.max(0) as u32).unwrap_or(0);
    if !task.is_empty() && !hf::TASKS.contains(&task.as_str()) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"ok": false, "results": [], "error": format!("Tâche inconnue : {task}.")})),
        )
            .into_response();
    }
    let task_opt = (!task.is_empty()).then_some(task.as_str());
    match hf::search_page(&query, task_opt, gb10, skip).await {
        Err(hf::HfError::Unavailable(msg)) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "ok": false, "results": [], "code": "hf_unavailable",
                "error": format!("Hugging Face n'a pas répondu ({msg}). Réessaie dans un instant."),
            })),
        )
            .into_response(),
        Ok((results, has_more)) => {
            // Only when the GB10 filter found nothing: does the model exist elsewhere?
            let outside = if gb10 && !query.is_empty() && results.is_empty() {
                hf::outside_gb10(&query, task_opt).await
            } else {
                None
            };
            Json(json!({
                "ok": true, "results": results, "query": query, "task": task,
                "gb10_only": gb10, "skip": skip, "page_size": hf::PAGE_SIZE,
                "has_more": has_more, "outside_gb10": outside,
                "hf_token": hf::token_present(),
            }))
            .into_response()
        }
    }
}

/// GET /system/stats - the runner's metrics, the served model, and for an admin
/// the runner's state and the active users (admin_routes.system_stats).
pub async fn system_stats(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    let session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => session,
    };
    let mut data = system::runner_metrics().await.unwrap_or_else(|| json!({}));
    let model_health = health::vllm_health(&state.db, state.spend.as_ref()).await;
    data["model"] = model_health.clone();
    // Flask: `get_running_models()`, the bare list of model names.
    data["running"] = json!(models::running_models().await);
    if session.is_admin {
        data["runner"] = system::runner_status().await;
        data["active_users"] = home::active_users(&state.db, state.spend.as_ref(), &model_health).await;
    }
    Json(data).into_response()
}

/// GET /usage/hourly - today's hourly token usage of the viewer (`{has_data: false}` when unknown)
pub async fn usage_hourly(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    let session = match auth::require_session(&headers, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(session) => session,
    };
    match home::user_hourly(state.spend.as_ref(), &session.username).await {
        Value::Null => Json(json!({"has_data": false})).into_response(),
        v => Json(v).into_response(),
    }
}

/// GET /api/latency - NOT PORTED. This is an in-memory ring of this process's
/// own request durations (latency.py); answering from the Rust process would
/// report a different measurement under the same name. Answered 501 rather
/// than an empty list that reads as « no requests ».
pub async fn api_latency(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if let Err(e) = auth::require_session(&headers, &state.db).await {
        return e.into_response();
    }
    (
        StatusCode::NOT_IMPLEMENTED,
        Json(json!({"ok": false, "error": "Non porté sur le portail Rust : mesure du processus Flask."})),
    )
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

/// GET /docs - the API page (app.docs). Not an API: signed out → redirect to login.
pub async fn docs(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Ok(_session) => {
            let platform = state.db.get_setting_set("platform_name").await.unwrap_or_else(|| "DGX".to_string());
            let public_api = crate::probes::public_api_url();
            let auto = std::env::var("AUTO_MODEL_NAME")
                .ok()
                .filter(|v| !v.is_empty())
                .unwrap_or_else(|| "auto-model".to_string());
            let served = models::running_models().await;
            let page = docs::html(&platform, &public_api, &auto, &served);
            ([(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], page).into_response()
        }
        Err(auth::AuthError::Unauthorized) => {
            (StatusCode::FOUND, [(axum::http::header::LOCATION, "/login?next=/docs"), (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")]).into_response()
        }
        Err(e) => e.into_response(),
    }
}

/// GET / - the page itself is the Next.js frontend's. Flask keeps the route for
/// its redirect targets: signed out → 302 to the login page (it is not an API,
/// so no 401), signed in → 204.
pub async fn index(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Ok(_session) => StatusCode::NO_CONTENT.into_response(),
        Err(auth::AuthError::Unauthorized) => {
            (StatusCode::FOUND, [(axum::http::header::LOCATION, "/login?next=/"), (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")]).into_response()
        }
        Err(e) => e.into_response(),
    }
}

/// Signed-in viewer, or the refusal to answer with (401 JSON-less, like Flask's API routes).
async fn viewer(state: &AppState, headers: &HeaderMap) -> Result<auth::SessionData, Response> {
    auth::require_session(headers, &state.db).await.map_err(|e| e.into_response())
}

/// GET /api/image/history - the viewer's image jobs
pub async fn api_image_history(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match media::image_history(state.db.pool(), &session.username).await {
        Ok(v) => Json(v).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// GET /api/image/status/:prompt_id - one of the viewer's image jobs, 404 otherwise
pub async fn api_image_status(State(state): State<AppState>, headers: HeaderMap, Path(prompt_id): Path<String>) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match media::image_status(state.db.pool(), &session.username, &prompt_id).await {
        Ok(Some(v)) => Json(v).into_response(),
        Ok(None) => auth::html_status(StatusCode::NOT_FOUND),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// GET /api/ocr/history - the viewer's OCR jobs
pub async fn api_ocr_history(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match media::ocr_history(state.db.pool(), &session.username).await {
        Ok(v) => Json(v).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// GET /api/video/history - the viewer's video jobs
pub async fn api_video_history(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match media::video_history(state.db.pool(), &session.username).await {
        Ok(v) => Json(v).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// GET /api/voice/history - the viewer's voice jobs
pub async fn api_voice_history(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match media::voice_history(state.db.pool(), &session.username).await {
        Ok(v) => Json(v).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// GET /api/voice/info - capabilities of the loaded voice backend
pub async fn api_voice_info(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(r) = viewer(&state, &headers).await {
        return r;
    }
    Json(media::voice_info().await).into_response()
}

/// GET /api/transcribe/available - dictation can serve now or on demand
pub async fn api_transcribe_available(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(r) = viewer(&state, &headers).await {
        return r;
    }
    Json(json!({"available": media::transcribe_available().await})).into_response()
}
