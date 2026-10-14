use std::sync::Arc;
pub mod handlers;
pub mod db;
pub mod error;
pub mod auth;
pub mod probes;

pub use db::Database;
pub use error::ApiError;

#[derive(Clone)]
pub struct AppState {
    pub db: Arc<Database>,
}

#[cfg(test)]
mod tests {
    #[test]
    fn it_works() {
        assert_eq!(2 + 2, 4);
    }
}
