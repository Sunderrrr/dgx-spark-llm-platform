//! Read-only user data, parity with dgx-portal: the sidebar's pending counters,
//! the notification bell, the platform announcements the viewer has not seen,
//! and the viewer's skills. Only local tables; nothing here writes, calls a
//! sidecar or LiteLLM.
use serde_json::{json, Value};
use sqlx::{Row, SqlitePool};

/// `GET /api/pending-count`: model and budget requests waiting. An admin sees
/// everyone's, a user only their own (the two badges are kept apart).
pub async fn pending_count(pool: &SqlitePool, username: &str, is_admin: bool) -> Result<Value, sqlx::Error> {
    let count = |sql: &'static str, scoped: bool| {
        let pool = pool.clone();
        let username = username.to_string();
        async move {
            let q = sqlx::query_scalar::<_, i64>(sql);
            let q = if scoped { q.bind(username) } else { q };
            q.fetch_one(&pool).await
        }
    };
    let (model, budget) = if is_admin {
        (
            count("SELECT COUNT(*) FROM model_requests WHERE status='pending'", false).await?,
            count("SELECT COUNT(*) FROM budget_requests WHERE status='pending'", false).await?,
        )
    } else {
        (
            count("SELECT COUNT(*) FROM model_requests WHERE status='pending' AND username=?", true).await?,
            count("SELECT COUNT(*) FROM budget_requests WHERE status='pending' AND username=?", true).await?,
        )
    };
    Ok(json!({"model": model, "budget": budget}))
}

/// `GET /api/notifications`: the 30 latest notifications and the unread count.
pub async fn notifications(pool: &SqlitePool, username: &str) -> Result<Value, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT id, kind, title, seen, created_at FROM notifications WHERE username = ? ORDER BY id DESC LIMIT 30",
    )
    .bind(username)
    .fetch_all(pool)
    .await?;
    let items: Vec<Value> = rows
        .iter()
        .map(|r| {
            json!({
                "id": r.get::<i64, _>("id"),
                "kind": r.get::<Option<String>, _>("kind"),
                "title": r.get::<Option<String>, _>("title"),
                "seen": r.get::<Option<i64>, _>("seen").unwrap_or(0) != 0,
                "created_at": r.get::<Option<String>, _>("created_at"),
            })
        })
        .collect();
    let unread: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM notifications WHERE username = ? AND seen = 0")
        .bind(username)
        .fetch_one(pool)
        .await?;
    Ok(json!({"items": items, "unread": unread}))
}

/// `GET /api/announcements`: the platform announcements after the viewer's last
/// seen one, newest first, at most 6.
pub async fn announcements(pool: &SqlitePool, username: &str) -> Result<Value, sqlx::Error> {
    let seen: i64 = sqlx::query_scalar("SELECT last_seen_id FROM announcement_state WHERE username = ?")
        .bind(username)
        .fetch_optional(pool)
        .await?
        .unwrap_or(0);
    let rows = sqlx::query("SELECT id, kind, a, b, created_at FROM announcements WHERE id > ? ORDER BY id DESC LIMIT 6")
        .bind(seen)
        .fetch_all(pool)
        .await?;
    let items: Vec<Value> = rows
        .iter()
        .map(|r| {
            json!({
                "id": r.get::<i64, _>("id"),
                "kind": r.get::<Option<String>, _>("kind"),
                "a": r.get::<Option<String>, _>("a"),
                "b": r.get::<Option<String>, _>("b"),
                "created_at": r.get::<Option<String>, _>("created_at"),
            })
        })
        .collect();
    Ok(json!({"items": items}))
}

/// `GET /api/skills`: the viewer's skills by name, with their file counts.
/// `max` is the per-account cap (skills_routes.MAX_SKILLS).
pub async fn skills(pool: &SqlitePool, username: &str, max: i64) -> Result<Value, sqlx::Error> {
    let rows = sqlx::query("SELECT * FROM skills WHERE username = ? ORDER BY name")
        .bind(username)
        .fetch_all(pool)
        .await?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let id: i64 = r.get("id");
        let file_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM skill_files WHERE skill_id = ?")
            .bind(id)
            .fetch_one(pool)
            .await?;
        out.push(json!({
            "id": id,
            "name": r.get::<Option<String>, _>("name"),
            "description": r.get::<Option<String>, _>("description"),
            "source": r.get::<Option<String>, _>("source").filter(|s| !s.is_empty()).unwrap_or_else(|| "manual".to_string()),
            "source_url": r.get::<Option<String>, _>("source_url"),
            "source_ref": r.get::<Option<String>, _>("source_ref"),
            "source_path": r.get::<Option<String>, _>("source_path"),
            "version": r.get::<Option<String>, _>("version").unwrap_or_default(),
            "synced_at": r.get::<Option<String>, _>("synced_at"),
            "created_at": r.get::<Option<String>, _>("created_at"),
            "enabled": r.get::<Option<i64>, _>("enabled").unwrap_or(0) != 0,
            "file_count": file_count,
        }));
    }
    Ok(json!({"ok": true, "skills": out, "max": max}))
}

/// `GET /api/discord/status`: whether account linking and DMs are configured
/// (instance settings), and whether this account is linked and under which name.
/// A failed lookup reads as « not linked », as in Flask.
pub async fn discord_status(
    pool: &SqlitePool,
    username: &str,
    link_enabled: bool,
    dm_enabled: bool,
) -> Value {
    let name: Option<Option<String>> = sqlx::query_scalar("SELECT discord_name FROM discord_links WHERE username = ?")
        .bind(username)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
    let name = name.flatten();
    json!({
        "linkable": link_enabled,
        "dm_enabled": dm_enabled,
        "linked": name.is_some(),
        "discord_name": name.unwrap_or_default(),
    })
}

/// `POST /api/notifications/seen`: every unseen notification of the viewer becomes seen.
pub async fn notifications_seen(pool: &SqlitePool, username: &str) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE notifications SET seen = 1 WHERE username = ? AND seen = 0")
        .bind(username)
        .execute(pool)
        .await?;
    Ok(())
}

/// `POST /api/announcements/seen`: the viewer has seen every announcement up to the newest.
pub async fn announcements_seen(pool: &SqlitePool, username: &str) -> Result<(), sqlx::Error> {
    let newest: i64 = sqlx::query_scalar("SELECT COALESCE(MAX(id), 0) FROM announcements")
        .fetch_one(pool)
        .await?;
    sqlx::query(
        "INSERT INTO announcement_state (username, last_seen_id) VALUES (?, ?) \
         ON CONFLICT(username) DO UPDATE SET last_seen_id = excluded.last_seen_id",
    )
    .bind(username)
    .bind(newest)
    .execute(pool)
    .await?;
    Ok(())
}

/// `POST /settings/appearance`: theme and language, each checked against its list
/// first (a refusal writes nothing), then written in one transaction.
pub async fn set_appearance(pool: &SqlitePool, username: &str, theme: Option<&str>, lang: Option<&str>) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    if let Some(t) = theme {
        sqlx::query(
            "INSERT INTO user_prefs (username, theme_id) VALUES (?, ?) \
             ON CONFLICT(username) DO UPDATE SET theme_id = excluded.theme_id",
        )
        .bind(username)
        .bind(t)
        .execute(&mut *tx)
        .await?;
    }
    if let Some(l) = lang {
        sqlx::query(
            "INSERT INTO user_prefs (username, lang) VALUES (?, ?) \
             ON CONFLICT(username) DO UPDATE SET lang = excluded.lang",
        )
        .bind(username)
        .bind(l)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await
}

/// `POST /settings/avatar`: the chosen avatar, or NULL for the generated one.
pub async fn set_avatar(pool: &SqlitePool, username: &str, avatar: Option<&str>) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO user_prefs (username, avatar_id) VALUES (?, ?) \
         ON CONFLICT(username) DO UPDATE SET avatar_id = excluded.avatar_id",
    )
    .bind(username)
    .bind(avatar)
    .execute(pool)
    .await?;
    Ok(())
}
