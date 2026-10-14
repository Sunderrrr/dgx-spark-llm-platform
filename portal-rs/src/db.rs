use sqlx::sqlite::{SqlitePool, SqliteConnectOptions};
use std::str::FromStr;
use anyhow::Result;

pub struct Database {
    pool: SqlitePool,
}

impl Database {
    pub async fn new(database_url: &str) -> Result<Self> {
        // Ensure parent directory exists
        if let Some(path) = database_url.strip_prefix("sqlite:") {
            if let Some(file_path) = path.split('?').next() {
                let path = std::path::Path::new(file_path);
                if let Some(parent) = path.parent() {
                    if !parent.as_os_str().is_empty() {
                        std::fs::create_dir_all(parent)?;
                    }
                }
            }
        }

        // Connect with create=true if missing
        let url = if database_url.contains('?') {
            format!("{}&mode=rwc", database_url)
        } else {
            format!("{}?mode=rwc", database_url)
        };

        let options = SqliteConnectOptions::from_str(&url)?
            .create_if_missing(true);

        let pool = SqlitePool::connect_with(options).await?;

        // Set WAL mode for concurrency
        sqlx::raw_sql("PRAGMA journal_mode = WAL;")
            .execute(&pool)
            .await?;

        Ok(Self { pool })
    }

    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }
}
