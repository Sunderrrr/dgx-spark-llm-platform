use axum::{
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use serde_json::json;
use crate::AppState;

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
    // Placeholder: return empty Prometheus format
    (
        StatusCode::OK,
        "# HELP portal_requests_total Total HTTP requests\n# TYPE portal_requests_total counter\nportal_requests_total 0\n",
    )
}

/// GET /api/config - Public platform config (branding + SSO + setup status)
pub async fn api_config() -> impl IntoResponse {
    // TODO: Read from DB: oidc_enabled, branding.name, branding.logo, setup_required
    // For now: defaults matching Flask fallback
    (
        StatusCode::OK,
        Json(json!({
            "oidc_enabled": false,
            "branding": {
                "name": "DGX Portal",
                "logo": null
            },
            "setup_required": false,
        })),
    )
}

/// GET /.well-known/security.txt
pub async fn security_txt() -> impl IntoResponse {
    (
        StatusCode::OK,
        "# Security contact\n# See deployment documentation\n",
    )
}

/// GET /api/branding/logo
pub async fn branding_logo() -> impl IntoResponse {
    // TODO: return actual logo or redirect
    (StatusCode::NOT_FOUND, "logo not found")
}

/// GET /api/health - Aggregated service health (auth required)
pub async fn api_health(State(_state): State<AppState>) -> impl IntoResponse {
    // TODO: Probe runner (:8001), litellm (:4001), sidecars
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
}

/// GET /api/home - Dashboard aggregated state (auth required)
pub async fn api_home(State(_state): State<AppState>) -> impl IntoResponse {
    // TODO: Read from vllm_health(), LiteLLM spend, user requests, sidecars
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
}

/// GET /api/modelhealth - Model health (auth required)
pub async fn api_modelhealth(State(_state): State<AppState>) -> impl IntoResponse {
    // TODO: Read from vllm_health() endpoint
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
}

/// GET /api/running-models - List running models (auth required)
pub async fn api_running_models(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "models": [],
        })),
    )
}

/// GET /api/ranking - Leaderboard (auth required)
pub async fn api_ranking(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "users": [],
        })),
    )
}

/// GET /api/ranking/activity - Ranking activity (auth required)
pub async fn api_ranking_activity(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "activity": [],
        })),
    )
}

/// GET /api/ranking/records - Ranking records (auth required)
pub async fn api_ranking_records(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "records": [],
        })),
    )
}

/// GET /api/ranking/user/:username - User ranking (auth required)
pub async fn api_ranking_user(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "user": null,
        })),
    )
}

/// GET /api/ranking/export.csv - Export ranking as CSV (auth required)
pub async fn api_ranking_export(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        "username,tokens\n",
    )
}

/// POST /api/ranking/privacy - Update ranking privacy (auth required)
pub async fn api_ranking_privacy(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "ok": true,
        })),
    )
}

/// GET /api/search - HF model search (auth required)
pub async fn api_search(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "models": [],
        })),
    )
}

/// GET /docs - OpenAPI docs (auth required)
pub async fn docs(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        "OpenAPI docs",
    )
}

/// GET /system/stats - System statistics (auth required)
pub async fn system_stats(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "memory": {},
        })),
    )
}

/// GET /usage/hourly - Hourly usage stats (auth required)
pub async fn usage_hourly(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "usage": [],
        })),
    )
}

/// GET /api/latency - API latency measurements (auth required)
pub async fn api_latency(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "routes": [],
        })),
    )
}

/// GET / - Index/landing page (auth required)
pub async fn index(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        "Portal Rust - Phase 2 (stateless reads)",
    )
}
