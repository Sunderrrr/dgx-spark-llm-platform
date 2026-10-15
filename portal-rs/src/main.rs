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
        spend: portal_rs::spend::pool_from_env(),
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
        .route("/api/image/history", get(handlers::api_image_history))
        .route("/api/image/status/:prompt_id", get(handlers::api_image_status))
        .route("/api/ocr/history", get(handlers::api_ocr_history))
        .route("/api/video/history", get(handlers::api_video_history))
        .route("/api/voice/history", get(handlers::api_voice_history))
        .route("/api/voice/info", get(handlers::api_voice_info))
        .route("/api/transcribe/available", get(handlers::api_transcribe_available))
        .route("/api/pending-count", get(handlers::api_pending_count))
        .route("/api/conversations", get(handlers::api_conversations))
        .route("/api/discord/status", get(handlers::api_discord_status))
        .route("/api/keys", get(handlers::api_keys))
        .route("/api/settings", get(handlers::api_settings))
        .route("/api/account/sessions", get(handlers::api_account_sessions))
        .route("/api/account/sessions/revoke", post(handlers::api_account_sessions_revoke))
        .route("/c/:token", get(handlers::share_view))
        .route("/settings/appearance", post(handlers::settings_appearance))
        .route("/settings/avatar", post(handlers::settings_avatar))
        .route("/conversations", post(handlers::conversations_post))
        .route("/conversations/share", post(handlers::conversations_share))
        .route("/api/memory", get(handlers::api_memory))
        .route("/api/memory/settings", post(handlers::api_memory_settings))
        .route("/api/memory/enabled", post(handlers::api_memory_enabled))
        .route("/api/memory/facts", post(handlers::api_memory_add))
        .route("/api/memory/facts/:edge_id", axum::routing::patch(handlers::api_memory_update).delete(handlers::api_memory_forget))
        .route("/api/memory/purge", post(handlers::api_memory_purge))
        .route("/api/memory/export", get(handlers::api_memory_export))
        .route("/api/memory/export.md", get(handlers::api_memory_export_md))
        .route("/api/memory/import", post(handlers::api_memory_import))
        .route("/api/memory/import.md", post(handlers::api_memory_import_md))
        .route("/api/skills/:id", get(handlers::api_skill_detail))
        .route("/api/skills/:id/download", get(handlers::api_skill_download))
        .route("/api/skills/:id/enabled", post(handlers::api_skill_enabled))
        .route("/api/skills/:id/sync", post(handlers::api_skill_sync))
        .route("/api/skills/import", post(handlers::api_skill_import_zip))
        .route("/api/skills/import-git", post(handlers::api_skill_import_git))
        .route("/api/onboarding/done", post(handlers::api_onboarding_done))
        .route("/discord/unlink", post(handlers::discord_unlink))
        .route("/skills", post(handlers::skills_form))
        .route("/image/file/:prompt_id", get(handlers::image_file))
        .route("/image/file/:prompt_id/:idx", get(handlers::image_file_idx))
        .route("/ocr/image/:job_id", get(handlers::ocr_image))
        .route("/voice/audio/:job_id", get(handlers::voice_audio))
        .route("/api/support/thread", get(handlers::api_support_thread))
        .route("/support/thread/clear", post(handlers::support_thread_clear))
        .route("/support/feedback", post(handlers::support_feedback))
        .route("/api/playground/data", get(handlers::api_playground_data))
        .route("/api/voice/chat/status", get(handlers::api_voice_chat_status))
        .route("/api/image/generate", post(handlers::api_image_generate))
        .route("/api/image/cancel/:prompt_id", post(handlers::api_image_cancel))
        .route("/api/image/delete/:prompt_id/:idx", post(handlers::api_image_delete))
        .route("/api/csrf", get(handlers::api_csrf))
        .route("/logout", post(handlers::logout))
        .route("/api/voice/generate", post(handlers::api_voice_generate))
        .route("/playground/preview", post(handlers::playground_preview_create))
        .route("/playground/preview/:pid", get(handlers::playground_preview_show))
        .route("/api/conversations/:client_id", get(handlers::api_conversation))
        .route("/api/notifications", get(handlers::api_notifications))
        .route("/api/announcements", get(handlers::api_announcements))
        .route("/api/skills", get(handlers::api_skills))
        .route("/api/notifications/seen", post(handlers::api_notifications_seen))
        .route("/api/announcements/seen", post(handlers::api_announcements_seen))
        
        .layer(axum::middleware::from_fn(portal_rs::security::layer))
        .layer(TraceLayer::new_for_http())
        .layer(DefaultBodyLimit::max(16 * 1024 * 1024)) // 16 MB max body
        .with_state(state);

    // Run server
    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{}", port)).await?;
    axum::serve(listener, app.into_make_service_with_connect_info::<std::net::SocketAddr>()).await?;

    Ok(())
}
