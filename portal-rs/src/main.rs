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

    // The probes' shared HTTP client, built before serving: its first build
    // (TLS roots) would otherwise stall the first requests' 0.3 s probes.
    let _ = portal_rs::sidecars::http();

    // Initialize database
    let db = Database::new(&db_url).await?;
    let state = AppState {
        db: Arc::new(db),
        spend: portal_rs::spend::pool_from_env(),
    };

    // The directory snapshot's first read starts now, so the first visit of
    // the Users page already has the LDAP states (directory_states_for([]) at
    // Flask's start). Same seam as the background jobs.
    if std::env::var("DGX_NO_REAPER").as_deref() != Ok("1") {
        let _ = portal_rs::ldap::states_for(&state, &[]);
    }

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
        .route("/playground/chat", post(handlers::playground_chat))
        .route("/support/chat", post(handlers::support_chat))
        .route("/support/confirm", post(handlers::support_confirm))
        .route("/api/voice/chat/status", get(handlers::api_voice_chat_status))
        .route("/api/image/generate", post(handlers::api_image_generate))
        .route("/api/image/cancel/:prompt_id", post(handlers::api_image_cancel))
        .route("/api/image/delete/:prompt_id/:idx", post(handlers::api_image_delete))
        .route("/api/csrf", get(handlers::api_csrf))
        .route("/logout", post(handlers::logout))
        .route("/login", post(handlers::login))
        .route("/api/admin", get(handlers::api_admin))
        .route("/admin/audit", get(handlers::admin_audit))
        .route("/admin/support/feedback", get(handlers::admin_support_feedback))
        .route("/admin/email/config", get(handlers::admin_email_config))
        .route("/admin/consumption", get(handlers::admin_consumption))
        .route("/admin/runner/logs", get(handlers::admin_runner_logs))
        .route("/admin/sidecar-logs/:kind", get(handlers::admin_sidecar_logs))
        .route("/admin/announce", post(handlers::admin_announce))
        .route("/admin/settings", post(handlers::admin_settings))
        .route("/admin/ocr/catalog/add", post(handlers::admin_ocr_catalog_add))
        .route("/admin/voice/catalog/add", post(handlers::admin_voice_catalog_add))
        .route("/admin/:kind/catalog/delete/:cid", post(handlers::admin_catalog_delete))
        .route("/admin/users/:username/revoke-sessions", post(handlers::admin_revoke_sessions))
        .route("/admin/users/:username/unblock", post(handlers::admin_unblock))
        .route("/admin/users/:username/block", post(handlers::admin_block))
        .route("/admin/users/:username/budget/set", post(handlers::admin_budget_set))
        .route("/admin/budget/approve/:id", post(handlers::admin_budget_approve))
        .route("/admin/budget/reject/:id", post(handlers::admin_budget_reject))
        .route("/admin/groups/create", post(handlers::admin_groups_create))
        .route("/admin/groups/delete/:name", post(handlers::admin_groups_delete))
        .route("/admin/users/create", post(handlers::admin_users_create))
        .route("/admin/users/update/:uid", post(handlers::admin_users_update))
        .route("/admin/users/delete/:uid", post(handlers::admin_users_delete))
        .route("/admin/users/:username/purge", post(handlers::admin_user_purge))
        .route("/admin/maintenance/toggle", post(handlers::admin_maintenance_toggle))
        .route("/admin/email/test", post(handlers::admin_email_test))
        .route("/api/account/delete", post(handlers::api_account_delete))
        .route("/api/account/password", post(handlers::api_account_password))
        .route("/api/setup", post(handlers::api_setup))
        .route("/login/sso", get(handlers::login_sso))
        .route("/api/oauth2-redirect", get(handlers::oauth_callback))
        .route("/admin/storage/migrate", post(handlers::admin_storage_migrate))
        .route("/api/admin/users", get(handlers::api_admin_users))
        .route("/admin/users/:username/detail", get(handlers::admin_user_detail))
        .route("/admin/platform", get(handlers::admin_platform))
        .route("/admin/update/:id", post(handlers::admin_update_request))
        .route("/admin/config", get(handlers::admin_config_get).post(handlers::admin_config_post))
        .route("/admin/config/test/:part", post(handlers::admin_config_test))
        .route("/admin/validate", post(handlers::admin_validate))
        .route("/admin/validate/state", get(handlers::admin_validate_state))
        .route("/discord/link", get(handlers::discord_link))
        .route("/discord/callback", get(handlers::discord_callback))
        .route("/admin/branding", post(handlers::admin_branding))
        .route("/admin/storage", get(handlers::admin_storage).post(handlers::admin_storage_save))
        .route("/admin/runner/stream", get(handlers::admin_runner_stream))
        .route("/admin/model/add", post(handlers::admin_model_add))
        .route("/admin/model/launch", post(handlers::admin_model_launch))
        .route("/admin/model/stop", post(handlers::admin_model_stop))
        .route("/admin/ocr/catalog/launch", post(handlers::admin_ocr_launch))
        .route("/admin/voice/catalog/launch", post(handlers::admin_voice_launch))
        .route("/admin/image/launch", post(handlers::admin_image_launch))
        .route("/admin/model/edit/:mid", post(handlers::admin_model_edit))
        .route("/admin/model/delete/:mid", post(handlers::admin_model_delete))
        .route("/admin/:kind/:action", post(handlers::admin_sidecar_control))
        .route("/request", post(handlers::request_model))
        .route("/api/model/request", post(handlers::api_model_request))
        .route("/api/voice/generate", post(handlers::api_voice_generate))
        .route("/api/playground/title", post(handlers::api_playground_title))
        .route("/api/playground/summarize", post(handlers::api_playground_summarize))
        .route("/api/video/generate", post(handlers::api_video_generate))
        .route("/api/video/status/:prompt_id", get(handlers::api_video_status))
        .route("/api/video/cancel/:prompt_id", post(handlers::api_video_cancel))
        .route("/video/file/:prompt_id", get(handlers::video_file))
        .route("/api/transcribe", post(handlers::api_transcribe))
        .route("/api/voice/chat/start", post(handlers::api_voice_chat_start))
        .route("/api/voice/speak", post(handlers::api_voice_speak))
        .route("/api/ocr/extract", post(handlers::api_ocr_extract))
        .route("/mcp", post(handlers::mcp_form))
        .route("/keys", post(handlers::keys_form))
        .route("/api/security", get(handlers::api_security))
        .route("/playground/preview", post(handlers::playground_preview_create))
        .route("/playground/preview/:pid", get(handlers::playground_preview_show))
        .route("/api/conversations/:client_id", get(handlers::api_conversation))
        .route("/api/notifications", get(handlers::api_notifications))
        .route("/api/announcements", get(handlers::api_announcements))
        .route("/api/skills", get(handlers::api_skills))
        .route("/api/notifications/seen", post(handlers::api_notifications_seen))
        .route("/api/announcements/seen", post(handlers::api_announcements_seen))
        
        .layer(axum::middleware::from_fn_with_state(state.clone(), portal_rs::relay::admin_layer))
        .layer(axum::middleware::from_fn(portal_rs::security::layer))
        .layer(TraceLayer::new_for_http())
        .layer(DefaultBodyLimit::max(16 * 1024 * 1024)) // 16 MB max body
        .with_state(state);

    // Run server
    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{}", port)).await?;
    axum::serve(listener, app.into_make_service_with_connect_info::<std::net::SocketAddr>()).await?;

    Ok(())
}
