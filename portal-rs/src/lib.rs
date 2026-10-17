use std::sync::Arc;
pub mod handlers;
pub mod db;
pub mod error;
pub mod auth;
pub mod probes;
pub mod monitor_read;
pub mod ops_cli;
pub mod backups;
pub mod ranking;
pub mod models;
pub mod spend;
pub mod health;
pub mod home;
pub mod security;
pub mod system;
pub mod hf;
pub mod docs;
pub mod sidecars;
pub mod media;
pub mod userdata;
pub mod conversations;
pub mod keys;
pub mod settings;
pub mod sessions;
pub mod sharing;
pub mod memnorm;
pub mod memory;
pub mod skillstore;
pub mod s3;
pub mod media_store;
pub mod guards;
pub mod session_store;
pub mod asr_ondemand;
pub mod asr_reaper;
pub mod budget_reaper;
pub mod mcp;
pub mod chat;
pub mod login;
pub mod rowjson;
pub mod discord;
pub mod mailer;
pub mod litellm_models;
pub mod launches;
pub mod ldap;
pub mod sso;
pub mod webauthn;
pub mod latency;

pub use db::Database;
pub use error::ApiError;

#[derive(Clone)]
pub struct AppState {
    pub db: Arc<Database>,
    /// LiteLLM's database (consumption), None when LITELLM_DATABASE_URL is unset.
    pub spend: Option<sqlx::PgPool>,
}

#[cfg(test)]
mod tests {
    #[test]
    fn it_works() {
        assert_eq!(2 + 2, 4);
    }
}
