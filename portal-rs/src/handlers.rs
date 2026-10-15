use std::collections::HashMap;

use sqlx::Row;
use axum::{
    extract::{Path, Query, State},
    http::{StatusCode, HeaderMap},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};
use crate::{AppState, auth, conversations, docs, health, hf, home, keys, media, memory, models, probes, ranking, sessions, settings, sharing, sidecars, system, userdata};

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
            let public_api = std::env::var("PUBLIC_API_URL").unwrap_or_default();
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

/// Answer of a user-data read: the value, or the unreadable-database refusal.
fn data_or_503(r: Result<Value, sqlx::Error>) -> Response {
    match r {
        Ok(v) => Json(v).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// GET /api/pending-count - the sidebar's badges
pub async fn api_pending_count(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(userdata::pending_count(state.db.pool(), &s.username, s.is_admin).await),
    }
}

/// GET /api/notifications - the bell
pub async fn api_notifications(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(userdata::notifications(state.db.pool(), &s.username).await),
    }
}

/// GET /api/announcements - announcements the viewer has not seen
pub async fn api_announcements(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(userdata::announcements(state.db.pool(), &s.username).await),
    }
}

/// GET /api/skills - the viewer's skills
pub async fn api_skills(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(userdata::skills(state.db.pool(), &s.username, 50).await),
    }
}

/// GET /api/conversations - the list, newest first, with its size budget
pub async fn api_conversations(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(conversations::list(state.db.pool(), &s.username).await),
    }
}

/// GET /api/conversations/:client_id - one whole conversation (Flask's JSON errors)
pub async fn api_conversation(State(state): State<AppState>, headers: HeaderMap, Path(client_id): Path<String>) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match conversations::one(state.db.pool(), &session.username, &client_id).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err("introuvable")) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "conversation introuvable"}))).into_response(),
        Ok(Err(_)) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"ok": false, "error": "conversation illisible"}))).into_response(),
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
    }
}

/// GET /api/discord/status - account linking and DMs, and this account's link
pub async fn api_discord_status(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let (client_id, client_secret, bot_token) = tokio::join!(
        state.db.get_config("discord_client_id"),
        state.db.get_config("discord_client_secret"),
        state.db.get_config("discord_bot_token"),
    );
    let link_enabled = client_id.is_some() && client_secret.is_some();
    Json(userdata::discord_status(state.db.pool(), &session.username, link_enabled, bot_token.is_some()).await).into_response()
}

/// GET /api/keys - the viewer's keys, the account envelope and the model limits
/// (app.api_keys). The keys are returned in clear, as Flask does.
pub async fn api_keys(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let db = &state.db;
    let key_budget: f64 = std::env::var("KEY_MAX_BUDGET").ok().and_then(|v| v.parse().ok()).unwrap_or(200_000_000.0);
    let default_budget: f64 = db
        .get_setting("default_key_budget")
        .await
        .ok()
        .flatten()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(key_budget);
    let duration = db
        .get_setting("default_key_duration")
        .await
        .ok()
        .flatten()
        .unwrap_or_else(|| std::env::var("KEY_BUDGET_DURATION").unwrap_or_else(|_| "7d".to_string()));

    let acct = keys::account_info(&session.username).await;
    let has_pending = sqlx::query_as::<_, (i64,)>("SELECT 1 FROM budget_requests WHERE username = ? AND status = 'pending'")
        .bind(&session.username)
        .fetch_optional(db.pool())
        .await
        .ok()
        .flatten()
        .is_some();
    let account = json!({
        "spend": acct.spend,
        "max_budget": if acct.exists { json!(acct.max_budget) } else { json!(default_budget) },
        "budget_reset_at": acct.budget_reset_at,
        "unlimited": session.is_admin,
        "has_pending": has_pending,
    });

    // Limits per catalog model, then `auto-model` inheriting the running one's.
    let mut model_limits = serde_json::Map::new();
    let catalog: Vec<(String, Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT name, vllm_args, engine FROM model_configs",
    )
    .fetch_all(db.pool())
    .await
    .unwrap_or_default();
    for (name, args, engine) in catalog {
        let engine = engine.filter(|e| !e.is_empty()).unwrap_or_else(|| "vllm".to_string());
        let (max_in, max_out) = health::ctx_split(args.as_deref().unwrap_or_default(), &engine);
        model_limits.insert(name, json!({"context": max_in, "output": max_out}));
    }
    let running = models::running_models().await;
    let auto_name = std::env::var("AUTO_MODEL_NAME").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "auto-model".to_string());
    let auto = running
        .first()
        .and_then(|m| model_limits.get(m).cloned())
        .unwrap_or_else(|| json!({"context": 262144, "output": 131072}));
    model_limits.insert(auto_name.clone(), auto);

    let (used, remaining) = home::budget_remaining(
        state.spend.as_ref(),
        &session.username,
        default_budget,
        home::budget_period_days(&duration),
    )
    .await;
    let user_keys = keys::user_keys(db.pool(), state.spend.as_ref(), &session.username).await;

    Json(json!({
        "user_keys": user_keys,
        "budget_tokens": home::thousands(default_budget),
        "budget_duration": duration,
        "budget_used": used,
        "budget_remaining": remaining,
        "account": account,
        "model_limits": model_limits,
        "running_models": running,
        "auto_model": auto_name,
        "public_api_url": std::env::var("PUBLIC_API_URL").unwrap_or_default(),
    }))
    .into_response()
}

/// Shared shape of the viewer's POST writes that take no body: CSRF first (as
/// Flask's before_request does, 400 even without a session), then the session.
async fn csrf_then_viewer(state: &AppState, headers: &HeaderMap) -> Result<auth::SessionData, Response> {
    match auth::csrf_ok(headers, None, &state.db).await {
        Err(e) => return Err(e.into_response()),
        Ok(false) => return Err(auth::html_status(StatusCode::BAD_REQUEST)),
        Ok(true) => {}
    }
    viewer(state, headers).await
}

/// POST /api/notifications/seen - mark the bell's notifications as read
pub async fn api_notifications_seen(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match csrf_then_viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match userdata::notifications_seen(state.db.pool(), &session.username).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// POST /api/announcements/seen - mark the announcements as read up to the newest
pub async fn api_announcements_seen(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match csrf_then_viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match userdata::announcements_seen(state.db.pool(), &session.username).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// GET /api/settings - the settings page's data (settings_routes.api_settings)
pub async fn api_settings(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let db = state.db.pool();
    let username = session.username.as_str();
    let servers: Vec<Value> = match sqlx::query(
        "SELECT id, name, url, description, allowed_tools, enabled, (auth_header IS NOT NULL) AS has_auth, created_at \
         FROM mcp_servers WHERE username = ? ORDER BY created_at DESC",
    )
    .bind(username)
    .fetch_all(db)
    .await
    {
        Ok(rows) => rows.iter().map(|r| json!({
            "id": r.get::<i64, _>("id"),
            "name": r.get::<Option<String>, _>("name"),
            "url": r.get::<Option<String>, _>("url"),
            "description": r.get::<Option<String>, _>("description"),
            "allowed_tools": r.get::<Option<String>, _>("allowed_tools"),
            "enabled": r.get::<Option<i64>, _>("enabled"),
            "has_auth": r.get::<Option<i64>, _>("has_auth"),
            "created_at": r.get::<Option<String>, _>("created_at"),
        })).collect(),
        Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
    };
    let skills: Vec<Value> = match sqlx::query(
        "SELECT id, name, description, instructions, created_at, source, version, enabled \
         FROM skills WHERE username = ? ORDER BY created_at DESC",
    )
    .bind(username)
    .fetch_all(db)
    .await
    {
        Ok(rows) => rows.iter().map(|r| json!({
            "id": r.get::<i64, _>("id"),
            "name": r.get::<Option<String>, _>("name"),
            "description": r.get::<Option<String>, _>("description"),
            "instructions": r.get::<Option<String>, _>("instructions"),
            "created_at": r.get::<Option<String>, _>("created_at"),
            "source": r.get::<Option<String>, _>("source"),
            "version": r.get::<Option<String>, _>("version"),
            "enabled": r.get::<Option<i64>, _>("enabled"),
        })).collect(),
        Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
    };
    let pref: Option<(Option<String>, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT avatar_id, theme_id, lang FROM user_prefs WHERE username = ?")
            .bind(username)
            .fetch_optional(db)
            .await
            .ok()
            .flatten();
    let (avatar_id, theme_id, lang) = match pref {
        Some((a, t, l)) => (a, t, l),
        None => (None, None, None),
    };

    let key_budget: f64 = std::env::var("KEY_MAX_BUDGET").ok().and_then(|v| v.parse().ok()).unwrap_or(200_000_000.0);
    let default_budget: f64 = state
        .db
        .get_setting("default_key_budget")
        .await
        .ok()
        .flatten()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(key_budget);
    let duration = state
        .db
        .get_setting("default_key_duration")
        .await
        .ok()
        .flatten()
        .unwrap_or_else(|| std::env::var("KEY_BUDGET_DURATION").unwrap_or_else(|_| "7d".to_string()));
    let acct = keys::account_info(username).await;
    let key_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM api_keys WHERE username = ?")
        .bind(username)
        .fetch_one(db)
        .await
        .unwrap_or(0);

    let running = models::running_models().await;
    let ctx = match running.first() {
        Some(model) => match state.db.model_config(model).await {
            Ok(Some((engine, args))) => {
                let engine = engine.filter(|e| !e.is_empty()).unwrap_or_else(|| "vllm".to_string());
                health::effective_ctx(args.as_deref().unwrap_or_default(), &engine)
            }
            _ => None,
        },
        None => None,
    };
    let activity = settings::activity(state.spend.as_ref(), username, 182).await;
    let limits = settings::limits(
        db,
        username,
        session.is_admin,
        default_budget,
        &acct,
        servers.len() as i64,
        skills.len() as i64,
        &running,
        ctx,
    )
    .await;

    Json(json!({
        "mcp_servers": servers,
        "skills": skills,
        "avatar_id": avatar_id,
        "theme_id": theme_id.unwrap_or_else(|| "neutral".to_string()),
        "lang": lang.unwrap_or_else(|| "en".to_string()),
        "theme_ids": settings::THEME_IDS,
        "langs": settings::LANGS,
        "avatars": settings::AVATAR_IDS.iter().map(|a| json!({"id": a, "label": settings::avatar_label(a)})).collect::<Vec<_>>(),
        "account": {
            "username": username,
            "fullname": session.fullname.clone().unwrap_or_else(|| username.to_string()),
            "is_admin": session.is_admin,
            "spend": acct.spend,
            "max_budget": acct.max_budget,
            "unlimited": session.is_admin,
            "budget_reset_at": Some(acct.budget_reset_at.clone()).filter(|s| !s.is_empty()),
            "budget_duration": duration,
            "key_count": key_count,
            "mcp_count": servers.len(),
            "skill_count": skills.len(),
        },
        "activity": activity,
        "limits": limits,
    }))
    .into_response()
}

/// GET /api/account/sessions - the viewer's active sessions and how the password is managed
pub async fn api_account_sessions(State(state): State<AppState>, headers: HeaderMap, connect: Option<axum::extract::ConnectInfo<std::net::SocketAddr>>) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    // complete_session_origin: fill in this browser's origin where missing.
    if let Some(sid) = session.sid.as_deref() {
        let ip = auth::client_ip(&headers, connect.map(|c| c.0.ip()));
        let ua = headers.get(axum::http::header::USER_AGENT).and_then(|v| v.to_str().ok()).unwrap_or_default();
        if let Err(e) = sessions::complete_origin(state.db.pool(), sid, &session.username, &ip, ua).await {
            let _ = e;
        }
    }
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    let listed = match sessions::list(state.db.pool(), &session.username, session.sid.as_deref(), now).await {
        Ok(v) => v,
        Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
    };
    let management = match state.db.get_password_management(&session.username).await {
        Ok(m) => m,
        Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
    };
    Json(json!({
        "sessions": listed,
        "local_account": management.local,
        "password_managed_by": management.management,
        "passkey_possible": management.passkey,
    }))
    .into_response()
}

/// POST /api/account/sessions/revoke - close one of the viewer's sessions (`id`), or all the others (`all`)
pub async fn api_account_sessions_revoke(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Response {
    // CSRF first, then the session; the body is JSON, or a form as the fallback.
    let form: std::collections::HashMap<String, String> = if headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/x-www-form-urlencoded"))
    {
        serde_urlencoded::from_bytes(&body).unwrap_or_default()
    } else {
        Default::default()
    };
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::html_status(StatusCode::BAD_REQUEST),
        Ok(true) => {}
    }
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    // Flask: `request.get_json(silent=True) or request.form`.
    let json_body: Option<Value> = serde_json::from_slice::<Value>(&body).ok().filter(|v| v.is_object());
    let field = |name: &str| -> Option<Value> {
        match &json_body {
            Some(v) if v.get(name).is_some() => v.get(name).cloned(),
            _ => form.get(name).map(|s| Value::String(s.clone())),
        }
    };
    let all = match field("all") {
        Some(Value::Bool(true)) => true,
        Some(Value::String(s)) => matches!(s.as_str(), "1" | "true" | "on"),
        _ => false,
    };
    let db = state.db.pool();
    if all {
        let n = match sessions::revoke_others(db, &session.username, session.sid.as_deref()).await {
            Ok(n) => n,
            Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
        };
        sessions::log_audit(db, &session.username, "account.sessions.revoke",
            &format!("{n} autre(s) session(s) fermée(s) par l'utilisateur")).await;
        return Json(json!({"ok": true, "revoked": n})).into_response();
    }
    let ident = match field("id") {
        Some(Value::String(s)) => s.trim().to_string(),
        _ => String::new(),
    };
    match sessions::revoke_one(db, &session.username, &ident).await {
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
        Ok(sessions::RevokeOne::TooShort) => (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Identifiant de session invalide."}))).into_response(),
        Ok(sessions::RevokeOne::NotFound) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "Session introuvable (déjà fermée ?)."}))).into_response(),
        Ok(sessions::RevokeOne::Ambiguous) => (StatusCode::CONFLICT, Json(json!({"ok": false, "error": "Identifiant de session ambigu, réessaie."}))).into_response(),
        Ok(sessions::RevokeOne::Revoked(target)) => {
            let current = session.sid.as_deref() == Some(target.as_str());
            let detail = if current { "fermeture de la session courante".to_string() } else { format!("fermeture de la session {ident}") };
            sessions::log_audit(db, &session.username, "account.sessions.revoke", &detail).await;
            Json(json!({"ok": true, "revoked": 1, "current": current})).into_response()
        }
    }
}

/// GET /c/:token - a shared conversation, read-only, for signed-in viewers only
pub async fn share_view(State(state): State<AppState>, headers: HeaderMap, Path(token): Path<String>) -> Response {
    if let Err(r) = viewer(&state, &headers).await {
        return r;
    }
    match sharing::load_and_count(state.db.pool(), &token).await {
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
        Ok(None) => auth::html_status(StatusCode::NOT_FOUND),
        Ok(Some(share)) => {
            let platform = state.db.get_setting_set("platform_name").await.unwrap_or_else(|| "DGX".to_string());
            ([(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], sharing::page(&share, &platform)).into_response()
        }
    }
}

/// Form fields of a POST (request.form): an urlencoded body, nothing else.
fn form_of(headers: &HeaderMap, body: &axum::body::Bytes) -> std::collections::HashMap<String, String> {
    let is_form = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/x-www-form-urlencoded"));
    if is_form { serde_urlencoded::from_bytes(body).unwrap_or_default() } else { Default::default() }
}

/// POST /settings/appearance - theme and language (allow-listed)
pub async fn settings_appearance(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let form = form_of(&headers, &body);
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::html_status(StatusCode::BAD_REQUEST),
        Ok(true) => {}
    }
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let theme = form.get("theme_id").map(String::as_str);
    let lang = form.get("lang").map(String::as_str);
    if let Some(t) = theme {
        if !settings::THEME_IDS.contains(&t) {
            return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Thème inconnu."}))).into_response();
        }
    }
    if let Some(l) = lang {
        if !settings::LANGS.contains(&l) {
            return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Langue inconnue."}))).into_response();
        }
    }
    match userdata::set_appearance(state.db.pool(), &session.username, theme, lang).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
    }
}

/// POST /settings/avatar - a listed avatar, or the generated one ('' or 'genere')
pub async fn settings_avatar(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let form = form_of(&headers, &body);
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::html_status(StatusCode::BAD_REQUEST),
        Ok(true) => {}
    }
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let requested = form.get("avatar_id").map(String::as_str).unwrap_or_default();
    let avatar: Option<&str> = if requested.is_empty() || requested == "genere" {
        None
    } else if settings::AVATAR_IDS.contains(&requested) {
        Some(requested)
    } else {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Avatar inconnu."}))).into_response();
    };
    match userdata::set_avatar(state.db.pool(), &session.username, avatar).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
    }
}

/// POST /conversations - `action=save` (upsert, bounded, the 30 newest kept) or
/// `action=delete` (one of the viewer's; `deleted` counts the rows).
pub async fn conversations_post(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let form = form_of(&headers, &body);
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::html_status(StatusCode::BAD_REQUEST),
        Ok(true) => {}
    }
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let bad = |msg: String| (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": msg}))).into_response();
    let action = form.get("action").map(String::as_str);
    match action {
        Some("save") => {
            let field = |k: &str| form.get(k).map(String::as_str).unwrap_or("").trim().to_string();
            let client_id: String = field("id").chars().take(64).collect();
            let title: String = field("title").chars().take(120).collect();
            let title = if title.is_empty() { "Conversation".to_string() } else { title };
            let model: String = field("model").chars().take(80).collect();
            if client_id.is_empty() {
                return bad("id manquant".to_string());
            }
            let raw = form.get("messages").map(String::as_str).unwrap_or("[]");
            let Ok(Value::Array(messages)) = serde_json::from_str::<Value>(raw) else {
                return bad("messages invalides".to_string());
            };
            match conversations::save(state.db.pool(), &session.username, &client_id, &title, &model, &messages).await {
                Ok(()) => Json(json!({"ok": true})).into_response(),
                Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
            }
        }
        Some("delete") => {
            let id = form.get("id").map(String::as_str).unwrap_or("");
            match conversations::delete(state.db.pool(), &session.username, id).await {
                Ok(n) => Json(json!({"ok": true, "deleted": n})).into_response(),
                Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
            }
        }
        other => bad(format!("Action inconnue : {}", conversations::py_repr(other))),
    }
}

/// POST /conversations/share - a read-only link to one of the viewer's conversations
pub async fn conversations_share(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let form = form_of(&headers, &body);
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::html_status(StatusCode::BAD_REQUEST),
        Ok(true) => {}
    }
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    // Flask: `request.get_json(silent=True) or request.form` (JSON only with its type).
    let is_json = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/json"));
    let json_body = if is_json { serde_json::from_slice::<Value>(&body).ok().filter(keys::truthy) } else { None };
    let raw_id: String = match &json_body {
        Some(v) => match v.get("client_id") {
            None => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(Value::Null) => "None".to_string(),
            Some(other) => other.to_string(),
        },
        None => form.get("client_id").cloned().unwrap_or_default(),
    };
    let client_id: String = raw_id.trim().chars().take(64).collect();
    if client_id.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "id manquant"}))).into_response();
    }
    match conversations::share(state.db.pool(), &session.username, &client_id).await {
        Ok(Some(token)) => Json(json!({"ok": true, "token": token})).into_response(),
        Ok(None) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "conversation introuvable"}))).into_response(),
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
    }
}

// ── Memory (memory_routes.py) ───────────────────────────────────────────────

/// `request.get_json(silent=True) or {}`: a JSON object body, else an empty map.
fn json_object(headers: &HeaderMap, body: &axum::body::Bytes) -> serde_json::Map<String, Value> {
    let is_json = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/json") || v.contains("+json"));
    if !is_json {
        return Default::default();
    }
    match serde_json::from_slice::<Value>(body) {
        Ok(Value::Object(m)) => m,
        _ => Default::default(),
    }
}

/// CSRF then the session, for a JSON or raw-body write.
async fn write_guard(state: &AppState, headers: &HeaderMap) -> Result<auth::SessionData, Response> {
    csrf_then_viewer(state, headers).await
}

/// Flask's `<int:edge_id>`: digits only, else the route does not match (404).
fn int_param(raw: &str) -> Option<i64> {
    (!raw.is_empty() && raw.chars().all(|c| c.is_ascii_digit())).then(|| raw.parse().ok()).flatten()
}

fn db_down() -> Response {
    (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response()
}

/// GET /api/memory - the viewer's whole graph (`?expired=1` includes expired facts)
pub async fn api_memory(State(state): State<AppState>, headers: HeaderMap, Query(q): Query<HashMap<String, String>>) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let db = state.db.pool();
    let include_expired = q.get("expired").map(String::as_str) == Some("1");
    let Ok((nodes, edges)) = memory::graph(db, &session.username, include_expired).await else { return db_down() };
    let (search_chats, sensitive) = memory::settings(db, &session.username).await;
    Json(json!({
        "enabled": memory::enabled(db, &session.username).await,
        "max_facts": memory::MEM_MAX_FACTS,
        "search_chats": search_chats,
        "sensitive": sensitive,
        "nodes": nodes,
        "edges": edges,
    }))
    .into_response()
}

/// POST /api/memory/settings - the two secondary switches
pub async fn api_memory_settings(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    let db = state.db.pool();
    for (key, column) in [("search_chats", "memory_search_chats"), ("sensitive", "memory_sensitive")] {
        if let Some(v) = data.get(key) {
            if memory::set_pref(db, &session.username, column, keys::truthy(v)).await.is_err() {
                return db_down();
            }
        }
    }
    let (search_chats, sensitive) = memory::settings(db, &session.username).await;
    Json(json!({"ok": true, "search_chats": search_chats, "sensitive": sensitive})).into_response()
}

/// POST /api/memory/enabled - turn the memory on or off (erases nothing)
pub async fn api_memory_enabled(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    let active = data.get("enabled").is_some_and(keys::truthy);
    if memory::set_pref(state.db.pool(), &session.username, "memory_enabled", active).await.is_err() {
        return db_down();
    }
    Json(json!({"ok": true, "enabled": active})).into_response()
}

/// POST /api/memory/facts - add a fact from the Memory page
pub async fn api_memory_add(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    let generic = json!(memory::MEM_GENERIC_RELATION);
    let subject_kind = json!("subject");
    // `data.get('relation') or 'à propos de'`, same for kind.
    let relation = data.get("relation").filter(|v| keys::truthy(v)).unwrap_or(&generic);
    let kind = data.get("kind").filter(|v| keys::truthy(v)).unwrap_or(&subject_kind);
    match memory::add_fact(state.db.pool(), &session.username, data.get("subject"), Some(relation), data.get("fact"), data.get("object"), "user", Some(kind)).await {
        Ok((msg, true)) => Json(json!({"ok": true, "message": msg})).into_response(),
        Ok((msg, false)) => (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": msg}))).into_response(),
        Err(_) => db_down(),
    }
}

/// PATCH /api/memory/facts/:edge_id - correct a fact in place
pub async fn api_memory_update(State(state): State<AppState>, headers: HeaderMap, Path(raw): Path<String>, body: axum::body::Bytes) -> Response {
    let Some(edge_id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    match memory::update_fact(state.db.pool(), &session.username, edge_id, data.get("fact"), data.get("relation")).await {
        Ok(memory::Update::Done(msg)) => Json(json!({"ok": true, "message": msg})).into_response(),
        Ok(memory::Update::Refused(msg)) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": msg}))).into_response(),
        Ok(memory::Update::Malformed) => auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
        Err(_) => db_down(),
    }
}

/// DELETE /api/memory/facts/:edge_id - forget a fact for good
pub async fn api_memory_forget(State(state): State<AppState>, headers: HeaderMap, Path(raw): Path<String>) -> Response {
    let Some(edge_id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match memory::forget(state.db.pool(), &session.username, edge_id).await {
        Ok(true) => Json(json!({"ok": true})).into_response(),
        Ok(false) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "Fait introuvable."}))).into_response(),
        Err(_) => db_down(),
    }
}

/// POST /api/memory/purge - erase the viewer's whole memory
pub async fn api_memory_purge(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match memory::purge(state.db.pool(), &session.username).await {
        Ok(n) => Json(json!({"ok": true, "deleted": n})).into_response(),
        Err(_) => db_down(),
    }
}

fn attachment(content_type: &str, filename: String, body: String) -> Response {
    (
        [
            (axum::http::header::CONTENT_TYPE, content_type.to_string()),
            (axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename={filename}")),
        ],
        body,
    )
        .into_response()
}

/// GET /api/memory/export - the viewer's memory as structured JSON (download)
pub async fn api_memory_export(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match memory::export_doc(state.db.pool(), &session.username).await {
        Ok(doc) => attachment("application/json", format!("dgx-memory-{}.json", session.username), memory::py_dumps_indent(&doc)),
        Err(_) => db_down(),
    }
}

/// GET /api/memory/export.md - the viewer's memory as readable Markdown (download)
pub async fn api_memory_export_md(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let platform = state.db.get_setting_set("platform_name").await.unwrap_or_else(|| "DGX".to_string());
    match memory::export_doc(state.db.pool(), &session.username).await {
        Ok(doc) => attachment(
            "text/markdown; charset=utf-8",
            format!("dgx-memory-{}.md", session.username),
            memory::export_markdown(&doc, &platform),
        ),
        Err(_) => db_down(),
    }
}

/// POST /api/memory/import - merge an exported JSON memory (the session is authoritative)
pub async fn api_memory_import(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    if body.len() > memory::IMPORT_MAX_BYTES {
        return (StatusCode::PAYLOAD_TOO_LARGE, Json(json!({"ok": false, "error": "Fichier trop volumineux."}))).into_response();
    }
    let Ok(mut doc) = serde_json::from_slice::<Value>(&body) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "JSON invalide."}))).into_response();
    };
    if let Some(err) = memory::validate_import(&mut doc) {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": err}))).into_response();
    }
    let db = state.db.pool();
    let user = session.username.as_str();
    let (mut imported_nodes, mut imported_facts) = (0i64, 0i64);
    for n in doc["nodes"].as_array().into_iter().flatten() {
        let name = n["name"].as_str().unwrap_or_default();
        let kind = n["kind"].as_str().unwrap_or("subject");
        let Ok(found) = memory::node(db, user, name, kind, true).await else { return db_down() };
        let Some(node_id) = found else { continue };
        imported_nodes += 1;
        for alias in n["aliases"].as_array().into_iter().flatten().filter_map(Value::as_str) {
            if memory::add_alias(db, user, node_id, alias).await.is_err() {
                return db_down();
            }
        }
    }
    let generic = json!(memory::MEM_GENERIC_RELATION);
    let subject_kind = json!("subject");
    for e in doc["edges"].as_array().into_iter().flatten() {
        let subject = memory::py_strip(e["subject"].as_str().unwrap_or_default()).to_string();
        let fact: String = memory::py_strip(e["fact"].as_str().unwrap_or_default()).chars().take(memory::MEM_MAX_FACT_LEN).collect();
        if subject.is_empty() || fact.is_empty() {
            continue;
        }
        match memory::fact_exists(db, user, &subject, &fact).await {
            Err(_) => return db_down(),
            Ok(true) => continue,
            Ok(false) => {}
        }
        let relation = e.get("relation").filter(|v| keys::truthy(v)).unwrap_or(&generic);
        let source = if e["source"] == "user" { "user" } else { "model" };
        match memory::add_fact(db, user, Some(&json!(subject)), Some(relation), Some(&json!(fact)), e.get("object"), source, Some(&subject_kind)).await {
            Ok((_, true)) => imported_facts += 1,
            Ok((_, false)) => {}
            Err(_) => return db_down(),
        }
    }
    Json(json!({"ok": true, "imported_nodes": imported_nodes, "imported_facts": imported_facts})).into_response()
}

/// POST /api/memory/import.md - merge facts read from a Markdown file
pub async fn api_memory_import_md(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    if body.len() > memory::IMPORT_MAX_BYTES {
        return (StatusCode::PAYLOAD_TOO_LARGE, Json(json!({"ok": false, "error": "Fichier trop volumineux."}))).into_response();
    }
    let Ok(text) = std::str::from_utf8(&body) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Encodage invalide (UTF-8 attendu)."}))).into_response();
    };
    let (edges, aliases) = memory::md_parse(text);
    if edges.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Aucun fait reconnu dans ce fichier."}))).into_response();
    }
    if edges.len() as i64 > memory::MEM_MAX_FACTS {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"ok": false, "code": "too_many_facts",
                        "error": format!("Trop de faits dans ce fichier ({} ; {} au maximum).", edges.len(), memory::MEM_MAX_FACTS)})),
        )
            .into_response();
    }
    let db = state.db.pool();
    let user = session.username.as_str();
    let subject_kind = json!("subject");
    let mut imported_facts = 0i64;
    for e in &edges {
        let subject: String = memory::py_strip(&e.subject).chars().take(crate::memnorm::MEM_MAX_NAME_LEN).collect();
        let fact: String = memory::py_strip(&e.fact).chars().take(memory::MEM_MAX_FACT_LEN).collect();
        if subject.is_empty() || fact.is_empty() {
            continue;
        }
        match memory::fact_exists(db, user, &subject, &fact).await {
            Err(_) => return db_down(),
            Ok(true) => continue,
            Ok(false) => {}
        }
        let object = e.object.as_ref().map(|o| json!(o));
        match memory::add_fact(db, user, Some(&json!(subject)), Some(&json!(e.relation)), Some(&json!(fact)), object.as_ref(), "user", Some(&subject_kind)).await {
            Ok((_, true)) => imported_facts += 1,
            Ok((_, false)) => {}
            Err(_) => return db_down(),
        }
    }
    let mut imported_aliases = 0u64;
    for (subject, alias) in &aliases {
        let Ok(found) = memory::node(db, user, subject, "subject", false).await else { return db_down() };
        let Some(node_id) = found else { continue };
        match memory::add_alias(db, user, node_id, alias).await {
            Ok(n) => imported_aliases += n,
            Err(_) => return db_down(),
        }
    }
    Json(json!({"ok": true, "imported_facts": imported_facts, "imported_aliases": imported_aliases})).into_response()
}
