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
