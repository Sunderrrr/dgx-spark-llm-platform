use sqlx::sqlite::{SqlitePool, SqliteConnectOptions};
use std::str::FromStr;
use anyhow::Result;
use serde_json::Value;
use serde::{Deserialize, Serialize};
use sha2::Digest;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionData {
    pub username: String,
    pub fullname: Option<String>,
    pub is_admin: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserPrefs {
    pub avatar_id: Option<String>,
    pub theme_id: Option<String>,
    pub lang: Option<String>,
}

#[derive(Debug, Clone)]
pub struct PasswordManagement {
    pub password_managed_by: String, // 'portail' | 'annuaire' | 'fournisseur_identite' | 'inconnu'
}

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

    /// Get session by cookie token (sha256 hash → web_sessions table)
    pub async fn get_session(&self, token: &str) -> Result<Option<SessionData>> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs_f64();

        let mut hasher = sha2::Sha256::new();
        hasher.update(token.as_bytes());
        let token_hash = hex::encode(hasher.finalize());

        let row: Option<(String,)> = sqlx::query_as(
            "SELECT data FROM web_sessions WHERE token_hash = ? AND expires_at > ?"
        )
        .bind(token_hash)
        .bind(now)
        .fetch_optional(&self.pool)
        .await?;

        if let Some((data,)) = row {
            if let Ok(session_data) = serde_json::from_str::<Value>(&data) {
                return Ok(Some(SessionData {
                    username: session_data.get("username")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown")
                        .to_string(),
                    fullname: session_data.get("fullname")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string()),
                    is_admin: session_data.get("is_admin")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false),
                }));
            }
        }

        Ok(None)
    }

    /// Read a setting from the database
    pub async fn get_setting(&self, key: &str) -> Result<Option<String>> {
        let row: Option<(String,)> = sqlx::query_as(
            "SELECT value FROM settings WHERE key = ?"
        )
        .bind(key)
        .fetch_optional(&self.pool)
        .await?;

        Ok(row.map(|(v,)| v))
    }

    /// Get user preferences (avatar, theme, lang)
    pub async fn get_user_prefs(&self, username: &str) -> Result<UserPrefs> {
        let row: Option<(Option<String>, Option<String>, Option<String>)> = sqlx::query_as(
            "SELECT avatar_id, theme_id, lang FROM user_prefs WHERE username = ?"
        )
        .bind(username)
        .fetch_optional(&self.pool)
        .await?;

        Ok(match row {
            Some((avatar_id, theme_id, lang)) => UserPrefs {
                avatar_id,
                theme_id,
                lang,
            },
            None => UserPrefs {
                avatar_id: None,
                theme_id: Some("neutral".to_string()),
                lang: Some("fr".to_string()),
            },
        })
    }

    /// Get password management source for a user
    /// local_users → 'portail', ldap → 'annuaire', sso → 'fournisseur_identite', unknown → 'inconnu'
    pub async fn get_password_management(&self, username: &str) -> Result<String> {
        // Check local_users first (authoritative)
        let is_local: Option<(i32,)> = sqlx::query_as(
            "SELECT 1 FROM local_users WHERE username = ?"
        )
        .bind(username)
        .fetch_optional(&self.pool)
        .await?;

        if is_local.is_some() {
            return Ok("portail".to_string());
        }

        // Check user_sources for last auth source
        let source: Option<(String,)> = sqlx::query_as(
            "SELECT last_source FROM user_sources WHERE username = ?"
        )
        .bind(username)
        .fetch_optional(&self.pool)
        .await?;

        Ok(match source {
            Some((src,)) => match src.as_str() {
                "ldap" => "annuaire".to_string(),
                "sso" => "fournisseur_identite".to_string(),
                _ => "inconnu".to_string(),
            },
            None => "inconnu".to_string(),
        })
    }
}
