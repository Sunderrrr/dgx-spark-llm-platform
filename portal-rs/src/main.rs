use axum::{
    extract::DefaultBodyLimit,
    routing::{get, post},
    Router,
};
use std::sync::Arc;
use tower_http::trace::TraceLayer;
use tracing::info;

use portal_rs::{AppState, Database, handlers};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Setup logging
    tracing_subscriber::fmt::init();
    
    dotenv::dotenv().ok();
    
    // Get config from env
    let db_url = std::env::var("DATABASE_URL").unwrap_or_else(|_| "sqlite:/app/data/portal.db".to_string());
    let port = std::env::var("PORTAL_RUST_PORT").unwrap_or_else(|_| "5100".to_string());
    let port: u16 = port.parse()?;

    // Initialize database
    let db = Database::new(&db_url).await?;
    let state = AppState {
        db: Arc::new(db),
    };

    info!("Portal Rust starting on 0.0.0.0:{}", port);

    // Build router
    let app = Router::new()
        // Health & status (no auth required)
        .route("/healthz", get(handlers::healthz))
        .route("/metrics", get(handlers::metrics))
        
        // API routes (no auth required)
        .route("/api/config", get(handlers::api_config))
        .route("/.well-known/security.txt", get(handlers::security_txt))
        .route("/api/branding/logo", get(handlers::branding_logo))
        
        // Auth-required routes (placeholder for now)
        .route("/api/session", get(handlers::api_session))
        .route("/api/whoami", get(handlers::api_whoami))
        .route("/api/health", get(handlers::api_health))
        .route("/api/home", get(handlers::api_home))
        .route("/api/modelhealth", get(handlers::api_modelhealth))
        .route("/api/running-models", get(handlers::api_running_models))
        .route("/api/ranking", get(handlers::api_ranking))
        .route("/api/ranking/activity", get(handlers::api_ranking_activity))
        .route("/api/ranking/records", get(handlers::api_ranking_records))
        .route("/api/ranking/user/:username", get(handlers::api_ranking_user))
        .route("/api/ranking/export.csv", get(handlers::api_ranking_export))
        .route("/api/ranking/privacy", post(handlers::api_ranking_privacy))
        .route("/api/search", get(handlers::api_search))
        .route("/docs", get(handlers::docs))
        .route("/system/stats", get(handlers::system_stats))
        .route("/usage/hourly", get(handlers::usage_hourly))
        .route("/api/latency", get(handlers::api_latency))
        .route("/", get(handlers::index))
        
        .layer(TraceLayer::new_for_http())
        .layer(DefaultBodyLimit::max(16 * 1024 * 1024)) // 16 MB max body
        .with_state(state);

    // Run server
    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{}", port)).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
