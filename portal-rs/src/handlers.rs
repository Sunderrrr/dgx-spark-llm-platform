use axum::{
    extract::State,
    http::{StatusCode, HeaderMap},
    response::IntoResponse,
    Json,
};
use serde_json::json;
use crate::{AppState, auth, probes};

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
    // Fetch all settings needed for config (parallel)
    let (oidc_metadata_url, oidc_client_id, oidc_client_secret, platform_name_setting, platform_logo, has_local_admin, has_dir_admin) = tokio::join!(
        async { state.db.get_setting("oidc_metadata_url").await.ok().flatten() },
        async { state.db.get_setting("oidc_client_id").await.ok().flatten() },
        async { state.db.get_setting("oidc_client_secret").await.ok().flatten() },
        async { state.db.get_setting("platform_name").await.ok().flatten() },
        async { state.db.get_setting("platform_logo").await.ok().flatten() },
        async {
            // Check for enabled local admin: local_users with enabled=1 and (is_admin=1 or group with is_admin=1)
            sqlx::query_scalar::<_, i32>(
                "SELECT 1 FROM local_users u LEFT JOIN user_groups g ON g.name = u.group_name
                 WHERE u.enabled = 1 AND (u.is_admin = 1 OR g.is_admin = 1) LIMIT 1"
            )
            .fetch_optional(state.db.pool())
            .await
            .ok()
            .flatten()
            .is_some()
        },
        async {
            // Check for directory admin
            sqlx::query_scalar::<_, i32>("SELECT 1 FROM user_sources WHERE last_is_admin = 1 LIMIT 1")
            .fetch_optional(state.db.pool())
            .await
            .ok()
            .flatten()
            .is_some()
        },
    );

    // oidc_enabled = all three components present
    let oidc_enabled = oidc_metadata_url.is_some() && oidc_client_id.is_some() && oidc_client_secret.is_some();

    // setup_required = no admins exist (neither local nor directory)
    let setup_required = !has_local_admin && !has_dir_admin;

    // platform_name defaults to "DGX" (not "DGX Portal")
    let platform_name = platform_name_setting.unwrap_or_else(|| "DGX".to_string());

    (
        StatusCode::OK,
        Json(json!({
            "oidc_enabled": oidc_enabled,
            "branding": {
                "name": platform_name,
                "logo": if platform_logo.is_some() { Some("/api/branding/logo") } else { None }
            },
            "setup_required": setup_required,
        })),
    )
}

/// GET /.well-known/security.txt
pub async fn security_txt(State(state): State<AppState>) -> impl IntoResponse {
    let (security_contact, admin_email, platform_name_setting) = tokio::join!(
        async { state.db.get_setting("security_contact").await.ok().flatten() },
        async { state.db.get_setting("admin_email").await.ok().flatten() },
        async { state.db.get_setting("platform_name").await.ok().flatten() },
    );

    let contact = security_contact.or(admin_email);
    let platform_name = platform_name_setting.unwrap_or_else(|| "DGX".to_string());

    // PUBLIC_WEB_URL from env, default to https://example.org
    let public_web_url = std::env::var("DGX_WEB")
        .unwrap_or_else(|_| "https://example.org".to_string())
        .trim_end_matches('/')
        .to_string();

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
                    "# Report a security issue on {}.\nContact: {}\nExpires: {}\nCanonical: {}/.well-known/security.txt\nPreferred-Languages: en, fr\n",
                    platform_name,
                    contact_line,
                    expires.format("%Y-%m-%dT00:00:00Z"),
                    public_web_url,
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

    // Probe services in parallel
    let (runner_ok, litellm_ok) = tokio::join!(
        probes::probe_runner(),
        probes::probe_litellm(),
    );

    (
        StatusCode::OK,
        Json(json!({
            "ok": true,
            "services": {
                "runner": { "reachable": runner_ok },
                "litellm": { "reachable": litellm_ok },
                "chat": { "running": null, "ready": runner_ok && litellm_ok },
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
    let _session = match auth::require_session(&headers, &state.db).await {
        Err(_) => return (StatusCode::UNAUTHORIZED, Json(json!({"error": "Unauthorized"}))).into_response(),
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
            // Fetch user preferences and password management
            let (prefs, pwd_mgmt) = match (
                state.db.get_user_prefs(&session.username).await,
                state.db.get_password_management(&session.username).await,
            ) {
                (Ok(prefs), Ok(pwd_mgmt)) => (prefs, pwd_mgmt),
                _ => {
                    return (
                        StatusCode::OK,
                        Json(json!({
                            "username": session.username,
                            "fullname": session.fullname.unwrap_or_else(|| session.username.clone()),
                            "is_admin": session.is_admin,
                            "avatar_id": null,
                            "theme_id": "neutral",
                            "lang": "fr",
                            "password_managed_by": "inconnu",
                        })),
                    )
                    .into_response();
                }
            };

            (
                StatusCode::OK,
                Json(json!({
                    "username": session.username,
                    "fullname": session.fullname.unwrap_or_else(|| session.username.clone()),
                    "is_admin": session.is_admin,
                    "avatar_id": prefs.avatar_id,
                    "theme_id": prefs.theme_id.unwrap_or_else(|| "neutral".to_string()),
                    "lang": prefs.lang.unwrap_or_else(|| "fr".to_string()),
                    "password_managed_by": pwd_mgmt,
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
