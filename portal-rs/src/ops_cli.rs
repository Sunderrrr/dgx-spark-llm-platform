//! The `portal` subcommands of the host's nightly backup (scripts/backup.sh),
//! answered without starting the server, like monitor_read:
//!   backup-settings             the backup settings as JSON (no secret in it)
//!   backup-upload <name> <keep> archive on stdin → bucket, verified, then pruned
//!   media-sweep [--dry-run]     removes the orphan generated files
//!   create-demo-account         (re)creates the `demo` screenshot account (env DEMO_PW)
//! The backup commands print one JSON line on stdout; a failure exits 1 (backup-upload
//! also prints `{"ok": false, "error": …}`, which backup.sh parses).
use std::sync::Arc;

use sqlx::SqlitePool;

use crate::handlers::admin_accounts::password_hash;
use crate::handlers::admin_groups::{locals, sync_local_budget};
use crate::{backups, keys, media_store, monitor_read, spend, AppState, Database};

const USAGE: &str = "usage: portal backup-settings | backup-upload <name> <keep> | media-sweep [--dry-run] | create-demo-account";

/// Some(exit code) when the command line asked for one of these, None to start the server.
pub async fn run_from_args(db_url: &str) -> Option<i32> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let command = args.first()?.clone();
    if !matches!(command.as_str(), "backup-settings" | "backup-upload" | "media-sweep" | "create-demo-account") {
        return None;
    }
    Some(run(&command, &args[1..], db_url).await)
}

async fn run(command: &str, args: &[String], db_url: &str) -> i32 {
    if command == "create-demo-account" {
        // Writes to the database, so it opens it read-write (not the read-only connection below).
        return create_demo_account(args, db_url).await;
    }
    let mut con = match monitor_read::connect(db_url).await {
        Ok(con) => con,
        Err(e) => {
            eprintln!("{e}");
            return 1;
        }
    };
    match (command, args) {
        ("backup-settings", []) => {
            println!("{}", backups::settings(&mut con).await);
            0
        }
        ("backup-upload", [name, keep]) => {
            let mut data = Vec::new();
            let result = match keep.parse::<i64>() {
                Err(e) => Err(e.to_string()),
                Ok(keep) => {
                    if let Err(e) = std::io::Read::read_to_end(&mut std::io::stdin().lock(), &mut data) {
                        Err(format!("cannot read the archive: {e}"))
                    } else {
                        backups::upload(&mut con, name, keep.max(1), data).await
                    }
                }
            };
            match result {
                Ok(mut out) => {
                    out["ok"] = serde_json::json!(true);
                    println!("{out}");
                    0
                }
                Err(e) => {
                    let error: String = e.chars().take(300).collect();
                    println!("{}", serde_json::json!({"ok": false, "error": error}));
                    1
                }
            }
        }
        ("media-sweep", [] | [_]) if args.iter().all(|f| f == "--dry-run") => {
            match media_store::sweep(&mut con, !args.is_empty()).await {
                Ok(out) => {
                    println!("{out}");
                    0
                }
                Err(e) => {
                    eprintln!("{e}");
                    1
                }
            }
        }
        _ => {
            eprintln!("{USAGE}");
            2
        }
    }
}

/// The `demo` account's password, read from DEMO_PW: 12 characters at least
/// (as the script it replaces); the password policy of the admin routes is not applied.
fn demo_password_error(password: &str) -> Option<&'static str> {
    (password.chars().count() < 12).then_some("DEMO_PW missing (12 characters minimum), see docs/development.md")
}

/// `create-demo-account`: (re)creates the account used for the README screenshots
/// (scripts/screenshots.py). Ordinary account (not admin, no group, default quota),
/// English UI, memory enabled, onboarding done. The password is never printed.
/// Its LiteLLM envelope is written through the same helper as the admin route,
/// so a failed quota write is reported (exit 1), never silently left unlimited.
async fn create_demo_account(args: &[String], db_url: &str) -> i32 {
    if !args.is_empty() {
        eprintln!("{USAGE}");
        return 2;
    }
    let username = std::env::var("DEMO_USER").unwrap_or_else(|_| "demo".to_string());
    let password = std::env::var("DEMO_PW").unwrap_or_default();
    if let Some(error) = demo_password_error(&password) {
        eprintln!("{error}");
        return 1;
    }
    let db = match Database::new(db_url).await {
        Ok(db) => db,
        Err(e) => {
            eprintln!("cannot open the database: {e}");
            return 1;
        }
    };
    let state = AppState { db: Arc::new(db), spend: spend::pool_from_env() };
    let Some(hash) = password_hash(&password).await else {
        eprintln!("cannot hash the password");
        return 1;
    };
    if let Err(e) = write_demo_account(state.db.pool(), &username, &hash).await {
        eprintln!("cannot write the account: {e}");
        return 1;
    }
    let rows = locals(state.db.pool(), "WHERE username=?", Some(&username)).await;
    let quota_ok = match rows.first() {
        Some(row) => sync_local_budget(&state, row).await,
        None => false,
    };
    if !quota_ok {
        eprintln!("quota NOT applied for {username} on LiteLLM: the account is unlimited until it is set");
        return 1;
    }
    println!("  account {username} ready (lang=en, neutral theme, memory enabled, not admin)");
    0
}

/// The account row and its preferences, replaced in one transaction.
async fn write_demo_account(pool: &SqlitePool, username: &str, hash: &str) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    sqlx::query("DELETE FROM local_users WHERE username=?").bind(username).execute(&mut *tx).await?;
    sqlx::query("DELETE FROM user_prefs WHERE username=?").bind(username).execute(&mut *tx).await?;
    sqlx::query(
        "INSERT INTO local_users (username, password_hash, fullname, is_admin, group_name, max_budget, enabled, created_at) \
         VALUES (?,?,?,0,NULL,NULL,1,?)",
    )
    .bind(username)
    .bind(hash)
    .bind("Demo account")
    .bind(keys::iso(&chrono::Local::now().naive_local()))
    .execute(&mut *tx)
    .await?;
    // English UI (the published screenshots are in English); `onboarded` skips the guided tour.
    sqlx::query(
        "INSERT INTO user_prefs (username, avatar_id, theme_id, lang, onboarded, memory_enabled) VALUES (?,?,?,?,1,1)",
    )
    .bind(username)
    .bind("avatar-01")
    .bind("neutral")
    .bind("en")
    .execute(&mut *tx)
    .await?;
    tx.commit().await
}

#[cfg(test)]
mod tests {
    use super::*;
    use sqlx::sqlite::SqlitePoolOptions;

    /// A throwaway in-memory database with the columns the demo account touches.
    async fn scratch_pool() -> SqlitePool {
        let pool = SqlitePoolOptions::new().max_connections(1).connect("sqlite::memory:").await.unwrap();
        for ddl in [
            "CREATE TABLE local_users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, \
             password_hash TEXT NOT NULL, fullname TEXT, is_admin INTEGER NOT NULL DEFAULT 0, group_name TEXT, \
             max_budget INTEGER, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)",
            "CREATE TABLE user_prefs (username TEXT PRIMARY KEY, avatar_id TEXT, theme_id TEXT, lang TEXT, \
             onboarded INTEGER, memory_enabled INTEGER)",
        ] {
            sqlx::query(ddl).execute(&pool).await.unwrap();
        }
        pool
    }

    #[test]
    fn demo_password_needs_twelve_characters() {
        assert!(demo_password_error("").is_some());
        assert!(demo_password_error("short-pass1").is_some());
        assert_eq!(demo_password_error("twelve-chars"), None);
    }

    #[tokio::test]
    async fn writes_the_demo_account_and_replaces_it() {
        let pool = scratch_pool().await;
        let hash = password_hash("corr3ct-h0rse-demo").await.unwrap();
        write_demo_account(&pool, "demo", &hash).await.unwrap();
        write_demo_account(&pool, "demo", &hash).await.unwrap();

        let (count,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM local_users WHERE username='demo'").fetch_one(&pool).await.unwrap();
        assert_eq!(count, 1);
        let row: (String, i64, Option<String>, Option<i64>, i64) = sqlx::query_as(
            "SELECT fullname, is_admin, group_name, max_budget, enabled FROM local_users WHERE username='demo'",
        )
        .fetch_one(&pool)
        .await
        .unwrap();
        assert_eq!(row, ("Demo account".to_string(), 0, None, None, 1));
        let stored: (String,) = sqlx::query_as("SELECT password_hash FROM local_users WHERE username='demo'").fetch_one(&pool).await.unwrap();
        assert!(crate::login::check_password_hash(&stored.0, "corr3ct-h0rse-demo"));

        let prefs: (String, String, String, i64, i64) = sqlx::query_as(
            "SELECT avatar_id, theme_id, lang, onboarded, memory_enabled FROM user_prefs WHERE username='demo'",
        )
        .fetch_one(&pool)
        .await
        .unwrap();
        assert_eq!(prefs, ("avatar-01".to_string(), "neutral".to_string(), "en".to_string(), 1, 1));
    }
}
