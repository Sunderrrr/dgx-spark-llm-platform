use std::sync::Arc;
pub mod handlers;
pub mod db;
pub mod error;
pub mod auth;
pub mod probes;
pub mod ranking;
pub mod models;
pub mod spend;
pub mod health;
pub mod home;
pub mod security;
pub mod system;
pub mod hf;

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
