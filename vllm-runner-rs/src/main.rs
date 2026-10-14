use axum::{
    extract::{DefaultBodyLimit, State},
    http::{header, StatusCode},
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tower_http::cors::CorsLayer;
use tracing::info;

mod process;

#[derive(Clone)]
pub struct AppState {
    runner_token: String,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct StatusResponse {
    ok: bool,
    model: Option<String>,
    pid: Option<u32>,
    uptime_seconds: u64,
    time: i64,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct LaunchRequest {
    model: String,
    #[serde(default)]
    engine: String,
    #[serde(default)]
    extra_tokens: String,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct ApiResponse<T> {
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<T>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    ok: Option<bool>,
}

// GET /status - Public health probe
async fn status(State(_state): State<AppState>) -> impl IntoResponse {
    // TODO: Read from process state file
    (
        StatusCode::OK,
        Json(StatusResponse {
            ok: true,
            model: None,
            pid: None,
            uptime_seconds: 0,
            time: chrono::Utc::now().timestamp(),
        }),
    )
}

// POST /launch - Start a model
async fn launch(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    Json(req): Json<LaunchRequest>,
) -> impl IntoResponse {
    // Check Bearer token
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|h| h.to_str().ok())
        .and_then(|h| h.strip_prefix("Bearer "));

    if auth != Some(&state.runner_token) {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({
                "ok": false,
                "error": "Invalid or missing Bearer token"
            })),
        )
            .into_response();
    }

    // TODO: Spawn model process, validate args, check memory
    info!("Launch request: model={}, engine={}", req.model, req.engine);

    (
        StatusCode::ACCEPTED,
        Json(ApiResponse::<()> {
            data: None,
            error: None,
            ok: Some(true),
        }),
    )
        .into_response()
}

// POST /stop - Stop the model
async fn stop(State(state): State<AppState>, headers: axum::http::HeaderMap) -> impl IntoResponse {
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|h| h.to_str().ok())
        .and_then(|h| h.strip_prefix("Bearer "));

    if auth != Some(&state.runner_token) {
        return (StatusCode::UNAUTHORIZED, "Invalid token").into_response();
    }

    // TODO: Kill process, persist state
    (StatusCode::OK, Json(json!({"ok": true}))).into_response()
}

// POST /resume - Auto-resume last model
async fn resume(State(_state): State<AppState>) -> impl IntoResponse {
    // TODO: Read last_model.json, relaunch
    (StatusCode::OK, Json(json!({"ok": true}))).into_response()
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    dotenv::dotenv().ok();

    let runner_token = std::env::var("RUNNER_TOKEN")
        .expect("RUNNER_TOKEN env var required")
        .trim()
        .to_string();

    if runner_token.len() < 16 {
        anyhow::bail!("RUNNER_TOKEN must be at least 16 characters");
    }

    let state = AppState {
        runner_token,
    };

    let app = Router::new()
        .route("/status", get(status))
        .route("/launch", post(launch))
        .route("/stop", post(stop))
        .route("/resume", post(resume))
        .layer(CorsLayer::permissive())
        .layer(DefaultBodyLimit::max(16 * 1024 * 1024))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind("0.0.0.0:8001").await?;
    info!("vLLM runner starting on 0.0.0.0:8001");
    axum::serve(listener, app).await?;

    Ok(())
}
