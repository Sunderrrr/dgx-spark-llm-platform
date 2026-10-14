use sqlx::sqlite::SqlitePool;
use anyhow::Result;

pub struct Database {
    pool: SqlitePool,
}

impl Database {
    pub async fn new(database_url: &str) -> Result<Self> {
        let pool = SqlitePool::connect(database_url).await?;

        // Run migrations if needed
        sqlx::raw_sql("PRAGMA journal_mode = WAL;")
            .execute(&pool)
            .await?;

        Ok(Self { pool })
    }

    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }
}
