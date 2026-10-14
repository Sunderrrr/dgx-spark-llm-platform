use axum::{
    extract::State,
    http::{StatusCode, HeaderMap},
    response::IntoResponse,
    Json,
};
use serde_json::json;
use crate::{AppState, auth};

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
pub async fn api_config(State(state): State<AppState>) -> impl IntoResponse {
    let oidc_enabled = state.db.get_setting("oidc_enabled").await.ok().flatten()
        .map(|v| v == "1")
        .unwrap_or(false);
    let platform_name = state.db.get_setting("platform_name").await.ok().flatten()
        .unwrap_or_else(|| "DGX Portal".to_string());
    let platform_logo = state.db.get_setting("platform_logo").await.ok().flatten();
    let setup_required = state.db.get_setting("setup_required").await.ok().flatten()
        .map(|v| v == "1")
        .unwrap_or(false);

    (
        StatusCode::OK,
        Json(json!({
            "oidc_enabled": oidc_enabled,
            "branding": {
                "name": platform_name,
                "logo": platform_logo.map(|_| "/api/branding/logo")
            },
            "setup_required": setup_required,
        })),
    )
}

/// GET /.well-known/security.txt
pub async fn security_txt(State(state): State<AppState>) -> impl IntoResponse {
    let contact = state.db.get_setting("security_contact").await.ok().flatten();
    let contact = if contact.is_none() {
        state.db.get_setting("admin_email").await.ok().flatten()
    } else {
        contact
    };

    match contact {
        Some(c) => {
            let contact_line = if c.starts_with("https://") || c.starts_with("mailto:") {
                c
            } else {
                format!("mailto:{}", c)
            };
            let expires = chrono::Utc::now() + chrono::Duration::days(180);
            (
                StatusCode::OK,
                format!(
                    "# Report a security issue\nContact: {}\nExpires: {}\nPreferred-Languages: en, fr\n",
                    contact_line,
                    expires.format("%Y-%m-%dT00:00:00Z")
                ),
            )
        }
        None => (StatusCode::NOT_FOUND, "No security contact configured\n".to_string()),
    }
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
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(_session) => {}
    }

    // TODO: Probe runner (:8001), litellm (:4001)
    (
        StatusCode::OK,
        Json(json!({
            "ok": true,
            "services": {
                "runner": { "reachable": false },
                "litellm": { "reachable": false },
                "chat": { "running": null, "ready": false },
                "video": { "ready": false, "on_demand": true },
                "ocr": { "ready": false, "on_demand": true },
                "voice": { "ready": false, "on_demand": true },
                "image": { "ready": false, "on_demand": true },
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
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(_session) => {}
    }

    (
        StatusCode::OK,
        Json(json!({
            "model": null,
            "uptime_seconds": 0,
            "tps": 0.0,
            "ttft_ms": 0,
            "requests_in_flight": 0,
            "tokens_generated": 0,
            "spend": { "budget": 0, "spend": 0 },
            "sidecars": {},
            "my_requests": [],
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
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
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
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(_session) => {}
    }

    (
        StatusCode::OK,
        Json(json!({"models": []})),
    )
    .into_response()
}

/// GET /api/session - Check if logged in (auth required)
pub async fn api_session(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Ok(_session) => (StatusCode::NO_CONTENT, "").into_response(),
        Err(_) => (StatusCode::UNAUTHORIZED, "").into_response(),
    }
}

/// GET /api/whoami - Current user info (auth required)
pub async fn api_whoami(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(session) => {
            (
                StatusCode::OK,
                Json(json!({
                    "username": session.username,
                    "fullname": session.fullname.unwrap_or_else(|| session.username.clone()),
                    "is_admin": session.is_admin,
                    "avatar_id": null,
                    "theme_id": "neutral",
                    "lang": "fr",
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
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"users": []})))
        .into_response()
}

/// GET /api/ranking/activity - Ranking activity (auth required)
pub async fn api_ranking_activity(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"activity": []})))
        .into_response()
}

/// GET /api/ranking/records - Ranking records (auth required)
pub async fn api_ranking_records(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"records": []})))
        .into_response()
}

/// GET /api/search - HF model search (auth required)
pub async fn api_search(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
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
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
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
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
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
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"routes": []})))
        .into_response()
}

/// GET /api/ranking/user/:username - User ranking (auth required)
pub async fn api_ranking_user(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"user": null})))
        .into_response()
}

/// GET /api/ranking/export.csv - Export ranking as CSV (auth required)
pub async fn api_ranking_export(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, "Unauthorized").into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, "username,tokens\n")
        .into_response()
}

/// POST /api/ranking/privacy - Update ranking privacy (auth required)
pub async fn api_ranking_privacy(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, Json(json!({"ok": true})))
        .into_response()
}

/// GET /docs - OpenAPI docs (auth required)
pub async fn docs(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, "Unauthorized").into_response(),
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
        Err(_) => return (StatusCode::UNAUTHORIZED, "Unauthorized").into_response(),
        Ok(_session) => {}
    }

    (StatusCode::OK, "Portal Rust - Phase 2 (routes structure)")
        .into_response()
}
