use sqlx::sqlite::{SqlitePool, SqliteConnectOptions};
use std::str::FromStr;
use anyhow::Result;
use serde_json::Value;
use sha2::Digest;

#[derive(Debug, Clone, Default)]
pub struct UserPrefs {
    pub avatar_id: Option<String>,
    pub theme_id: Option<String>,
    pub lang: Option<String>,
    /// No row = an account that never set anything, hence never onboarded.
    pub onboarded: bool,
}

/// Who holds an account's password (local_users.password_management).
#[derive(Debug, Clone)]
pub struct PasswordManagement {
    /// 'portal' | 'ldap-directory' | 'sso-provider' | 'unknown'
    pub management: &'static str,
    /// Every login source seen, sorted (user_sources.sources is cumulative).
    pub sources: Vec<String>,
    pub local: bool,
    pub passkey: bool,
}

impl PasswordManagement {
    /// The rule of local_users.password_management: a local_users row is
    /// authoritative, then ldap, then sso. Pure: tested below.
    pub fn from_parts(local: bool, sources_csv: &str) -> Self {
        let mut sources: Vec<String> =
            sources_csv.split(',').filter(|s| !s.is_empty()).map(str::to_string).collect();
        sources.sort();
        let management = if local {
            "portal"
        } else if sources.iter().any(|s| s == "ldap") {
            "ldap-directory"
        } else if sources.iter().any(|s| s == "sso") {
            "sso-provider"
        } else {
            "unknown"
        };
        let passkey = matches!(management, "portal" | "ldap-directory");
        Self { management, sources, local, passkey }
    }
}

/// A local account's state: the portal is the sole master of it.
#[derive(Debug, Clone, Copy)]
pub struct LocalAccount {
    pub enabled: bool,
    /// Own flag OR its group's flag (local_users._local_user_is_admin).
    pub is_admin: bool,
}

/// What auth.account_state reads, gathered for a pure decision.
#[derive(Debug, Clone, Default)]
pub struct AccountFacts {
    pub blocked: bool,
    pub local: Option<LocalAccount>,
    pub is_owner: bool,
    /// The directory role recorded at the last login (None: no opinion).
    pub last_is_admin: Option<bool>,
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

    /// The raw session data for a cookie token (sha256 → web_sessions), or
    /// None when unknown or expired. Validation is auth::require_session's job.
    pub async fn get_session(&self, token: &str) -> Result<Option<Value>> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs_f64();
        let token_hash = hex::encode(sha2::Sha256::digest(token.as_bytes()));
        let row: Option<(String,)> = sqlx::query_as(
            "SELECT data FROM web_sessions WHERE token_hash = ? AND expires_at > ?",
        )
        .bind(token_hash)
        .bind(now)
        .fetch_optional(&self.pool)
        .await?;
        // Unreadable data is a session we cannot vouch for: treated as absent.
        Ok(row.and_then(|(data,)| serde_json::from_str::<Value>(&data).ok()))
    }

    /// user_sessions registry row: (revoked, expires_at), None when absent.
    pub async fn get_session_registry(&self, sid: &str) -> Result<Option<(bool, f64)>> {
        let row: Option<(i64, f64)> =
            sqlx::query_as("SELECT revoked, expires_at FROM user_sessions WHERE sid = ?")
                .bind(sid)
                .fetch_optional(&self.pool)
                .await?;
        Ok(row.map(|(revoked, expires_at)| (revoked != 0, expires_at)))
    }

    /// The facts auth.account_state decides on, read in one place.
    pub async fn get_account_facts(&self, username: &str) -> Result<AccountFacts> {
        let blocked: Option<(i64,)> = sqlx::query_as("SELECT 1 FROM blocked_users WHERE username = ?")
            .bind(username)
            .fetch_optional(&self.pool)
            .await?;
        let local: Option<(i64, i64, Option<i64>)> = sqlx::query_as(
            "SELECT u.enabled, u.is_admin, g.is_admin FROM local_users u \
             LEFT JOIN user_groups g ON g.name = u.group_name WHERE u.username = ?",
        )
        .bind(username)
        .fetch_optional(&self.pool)
        .await?;
        let owner = self.get_setting_set("owner_username").await;
        let source: Option<(Option<i64>,)> =
            sqlx::query_as("SELECT last_is_admin FROM user_sources WHERE username = ?")
                .bind(username)
                .fetch_optional(&self.pool)
                .await?;
        Ok(AccountFacts {
            blocked: blocked.is_some(),
            local: local.map(|(enabled, own, group)| LocalAccount {
                enabled: enabled != 0,
                is_admin: own != 0 || group.unwrap_or(0) != 0,
            }),
            is_owner: owner.as_deref() == Some(username),
            last_is_admin: source.and_then(|(v,)| v).map(|v| v != 0),
        })
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

    /// A setting that is set: an empty or blank value counts as unset, like
    /// Flask's truthiness checks on `get_setting`.
    pub async fn get_setting_set(&self, key: &str) -> Option<String> {
        self.get_setting(key)
            .await
            .ok()
            .flatten()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
    }

    /// An instance setting from Admin → Config (instance_config.setting): stored
    /// as `cfg_<name>`, empty = unset. The code defaults of the settings read
    /// here are all empty, so unset is None.
    pub async fn get_config(&self, name: &str) -> Option<String> {
        self.get_setting_set(&format!("cfg_{name}")).await
    }

    /// The account's preferences, raw (the route applies the defaults).
    pub async fn get_user_prefs(&self, username: &str) -> Result<UserPrefs> {
        let row: Option<(Option<String>, Option<String>, Option<String>, Option<i64>)> = sqlx::query_as(
            "SELECT avatar_id, theme_id, lang, onboarded FROM user_prefs WHERE username = ?",
        )
        .bind(username)
        .fetch_optional(&self.pool)
        .await?;
        Ok(row
            .map(|(avatar_id, theme_id, lang, onboarded)| UserPrefs {
                avatar_id,
                theme_id,
                lang,
                onboarded: onboarded.unwrap_or(0) != 0,
            })
            .unwrap_or_default())
    }

    /// Who holds the password, parity with local_users.password_management.
    pub async fn get_password_management(&self, username: &str) -> Result<PasswordManagement> {
        let local: Option<(i64,)> = sqlx::query_as("SELECT 1 FROM local_users WHERE username = ?")
            .bind(username)
            .fetch_optional(&self.pool)
            .await?;
        let sources: Option<(Option<String>,)> =
            sqlx::query_as("SELECT sources FROM user_sources WHERE username = ?")
                .bind(username)
                .fetch_optional(&self.pool)
                .await?;
        let csv = sources.and_then(|(s,)| s).unwrap_or_default();
        Ok(PasswordManagement::from_parts(local.is_some(), &csv))
    }

    /// Accounts that really exist (stats._known_accounts): local, or recorded
    /// as a login source. None when unreadable — the caller concludes nothing.
    pub async fn known_accounts(&self) -> Option<std::collections::HashSet<String>> {
        let local: Vec<(String,)> = sqlx::query_as("SELECT username FROM local_users")
            .fetch_all(&self.pool)
            .await
            .ok()?;
        let sources: Vec<(String,)> = sqlx::query_as("SELECT DISTINCT username FROM user_sources")
            .fetch_all(&self.pool)
            .await
            .ok()?;
        Some(local.into_iter().chain(sources).map(|(u,)| u).collect())
    }

    /// account → its groups, local group + directory groups (ranking._user_groups),
    /// in first-seen order (Python iterates its dict that way); empty when unreadable.
    pub async fn user_groups(&self) -> Vec<(String, std::collections::BTreeSet<String>)> {
        let local: Result<Vec<(String, String)>, _> = sqlx::query_as(
            "SELECT username, group_name FROM local_users WHERE group_name IS NOT NULL AND group_name != ''",
        )
        .fetch_all(&self.pool)
        .await;
        let directory: Result<Vec<(String, String)>, _> =
            sqlx::query_as("SELECT username, group_name FROM user_directory_groups")
                .fetch_all(&self.pool)
                .await;
        let (Ok(local), Ok(directory)) = (local, directory) else {
            return Vec::new();
        };
        let mut out: Vec<(String, std::collections::BTreeSet<String>)> = Vec::new();
        for (u, g) in local.into_iter().chain(directory) {
            match out.iter_mut().find(|(name, _)| *name == u) {
                Some((_, groups)) => {
                    groups.insert(g);
                }
                None => out.push((u, std::iter::once(g).collect())),
            }
        }
        out
    }

    /// Accounts that chose to appear anonymously in the leaderboard.
    pub async fn anonymous_accounts(&self) -> std::collections::HashSet<String> {
        sqlx::query_as::<_, (String,)>("SELECT username FROM user_prefs WHERE ranking_anonymous = 1")
            .fetch_all(&self.pool)
            .await
            .map(|rows| rows.into_iter().map(|(u,)| u).collect())
            .unwrap_or_default()
    }

    /// Every account's avatar, in ONE read (not one per ranking row).
    pub async fn avatars(&self) -> std::collections::HashMap<String, Option<String>> {
        sqlx::query_as::<_, (String, Option<String>)>("SELECT username, avatar_id FROM user_prefs")
            .fetch_all(&self.pool)
            .await
            .map(|rows| rows.into_iter().collect())
            .unwrap_or_default()
    }

    /// Maintenance mode (db.maintenance_active).
    pub async fn maintenance_active(&self) -> bool {
        self.get_setting("maintenance_mode").await.ok().flatten().as_deref() == Some("1")
    }
}

#[cfg(test)]
mod tests {
    use super::PasswordManagement;

    #[test]
    fn a_local_row_is_authoritative() {
        let m = PasswordManagement::from_parts(true, "sso,ldap");
        assert_eq!(m.management, "portal");
        assert!(m.passkey);
        assert_eq!(m.sources, vec!["ldap", "sso"]);
    }

    #[test]
    fn directory_before_sso() {
        assert_eq!(PasswordManagement::from_parts(false, "sso,ldap").management, "ldap-directory");
        let sso = PasswordManagement::from_parts(false, "sso");
        assert_eq!(sso.management, "sso-provider");
        assert!(!sso.passkey);
    }

    #[test]
    fn nothing_known_is_unknown() {
        let m = PasswordManagement::from_parts(false, "");
        assert_eq!(m.management, "unknown");
        assert!(m.sources.is_empty());
    }
}
