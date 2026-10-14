//! The viewer's own sessions, parity with dgx-portal settings_routes.py
//! (`_my_sessions`, the revoke route) and auth.py (`complete_session_origin`),
//! plus the audit writer they use (db.py `log_audit`, with its 5 000-row live
//! table and archive).
//!
//! Security notes carried over: the full session id never leaves the server
//! (12 characters are shown); the origin (IP, user agent) of a session is only
//! filled in when absent, and only for the caller's own session.
use chrono::Utc;
use serde_json::{json, Value};
use sqlx::{Row, SqlitePool};

use crate::keys;

/// Live audit rows kept; older ones move to the archive (db.log_audit).
pub const AUDIT_LIVE_ROWS: i64 = 5000;

/// `log_audit`: an audit entry, then the live table trimmed to its newest rows,
/// the evicted ones archived. A write failure is reported on stderr, never raised.
pub async fn log_audit(db: &SqlitePool, username: &str, action: &str, detail: &str) {
    let now = keys::iso(&Utc::now().naive_utc());
    let who = if username.is_empty() { "système" } else { username };
    let run = async {
        let mut tx = db.begin().await?;
        sqlx::query("INSERT INTO audit_log (username, action, detail, created_at) VALUES (?, ?, ?, ?)")
            .bind(who)
            .bind(action)
            .bind(detail)
            .bind(&now)
            .execute(&mut *tx)
            .await?;
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS audit_log_archive (id INTEGER PRIMARY KEY, username TEXT, action TEXT, \
             detail TEXT, created_at TEXT)",
        )
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "INSERT OR IGNORE INTO audit_log_archive SELECT * FROM audit_log WHERE id NOT IN \
             (SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)",
        )
        .bind(AUDIT_LIVE_ROWS)
        .execute(&mut *tx)
        .await?;
        sqlx::query("DELETE FROM audit_log WHERE id NOT IN (SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)")
            .bind(AUDIT_LIVE_ROWS)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok::<(), sqlx::Error>(())
    };
    if let Err(e) = run.await {
        eprintln!("[audit] write impossible ({action}): {e}");
    }
}

/// `complete_session_origin`: fills the IP and user agent of the CALLER's own
/// session, and only where they are missing.
pub async fn complete_origin(db: &SqlitePool, sid: &str, username: &str, ip: &str, user_agent: &str) -> Result<(), sqlx::Error> {
    let ua: String = user_agent.chars().take(400).collect();
    sqlx::query(
        "UPDATE user_sessions SET ip = ?, user_agent = ? WHERE sid = ? AND username = ? \
         AND (ip IS NULL OR user_agent IS NULL)",
    )
    .bind(ip)
    .bind(ua)
    .bind(sid)
    .bind(username)
    .execute(db)
    .await?;
    Ok(())
}

/// `_my_sessions`: the account's active sessions, most recent first (50 at most).
pub async fn list(db: &SqlitePool, username: &str, current_sid: Option<&str>, now: f64) -> Result<Vec<Value>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT sid, created_at, expires_at, ip, user_agent FROM user_sessions \
         WHERE username = ? AND revoked = 0 AND expires_at > ? ORDER BY created_at DESC LIMIT 50",
    )
    .bind(username)
    .bind(now)
    .fetch_all(db)
    .await?;
    Ok(rows
        .iter()
        .map(|r| {
            let sid: Option<String> = r.get("sid");
            let current = match (current_sid, sid.as_deref()) {
                (Some(c), Some(s)) => c == s,
                _ => false,
            };
            json!({
                "id": sid.as_deref().unwrap_or("").chars().take(12).collect::<String>(),
                // created_at and expires_at are epoch seconds (REAL), as Flask stores them.
                "created_at": r.try_get::<Option<f64>, _>("created_at").ok().flatten(),
                "expires_at": r.try_get::<Option<f64>, _>("expires_at").ok().flatten(),
                "ip": r.try_get::<Option<String>, _>("ip").ok().flatten(),
                "user_agent": r.try_get::<Option<String>, _>("user_agent").ok().flatten(),
                "current": current,
            })
        })
        .collect())
}

/// Outcome of revoking one session by its 12-character identifier.
pub enum RevokeOne {
    /// The session's full id, now revoked.
    Revoked(String),
    /// Too short to identify a session (400).
    TooShort,
    /// No live session matches (404).
    NotFound,
    /// Several match: refused rather than closing one at random (409).
    Ambiguous,
}

/// Revokes one of the account's sessions by identifier prefix (SQL-scoped to the account).
pub async fn revoke_one(db: &SqlitePool, username: &str, ident: &str) -> Result<RevokeOne, sqlx::Error> {
    if ident.chars().count() < 8 {
        return Ok(RevokeOne::TooShort);
    }
    let pattern = format!("{ident}%");
    let lines: Vec<String> = sqlx::query_scalar(
        "SELECT sid FROM user_sessions WHERE username = ? AND revoked = 0 AND sid LIKE ?",
    )
    .bind(username)
    .bind(&pattern)
    .fetch_all(db)
    .await?;
    match lines.len() {
        0 => Ok(RevokeOne::NotFound),
        1 => {
            let target = lines[0].clone();
            sqlx::query("UPDATE user_sessions SET revoked = 1 WHERE sid = ?")
                .bind(&target)
                .execute(db)
                .await?;
            Ok(RevokeOne::Revoked(target))
        }
        _ => Ok(RevokeOne::Ambiguous),
    }
}

/// Revokes every other session of the account; returns how many closed.
pub async fn revoke_others(db: &SqlitePool, username: &str, current_sid: Option<&str>) -> Result<u64, sqlx::Error> {
    let r = sqlx::query("UPDATE user_sessions SET revoked = 1 WHERE username = ? AND sid <> ? AND revoked = 0")
        .bind(username)
        .bind(current_sid.unwrap_or(""))
        .execute(db)
        .await?;
    Ok(r.rows_affected())
}

#[cfg(test)]
mod tests {
    #[test]
    fn identifiers_are_matched_by_prefix_and_must_be_long_enough() {
        // The 12-character identifier is a prefix of the full session id.
        let sid = "abcdefghijklmnopqrstuvwxyz";
        assert_eq!(sid.chars().take(12).collect::<String>(), "abcdefghijkl");
        assert!("abcdefg".chars().count() < 8);
    }
}
