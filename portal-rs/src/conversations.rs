//! The viewer's conversations, read side, parity with dgx-portal
//! conversation_routes.py: `GET /api/conversations` (the list, with a size
//! budget) and `GET /api/conversations/<client_id>` (one whole conversation).
//! The writes (`POST /conversations`, sharing) stay on Flask: they live under
//! another prefix and are not routed here.
use serde_json::{json, Map, Value};
use sqlx::{Row, SqlitePool};

/// Budget of the list, in characters of the stored JSON (Python's len on str).
pub const LIST_MAX_CHARS: usize = 8_000_000;

/// One stored row: (client_id, title, model, messages JSON text, updated_at).
type Row5 = (String, Option<String>, Option<String>, String, Option<String>);

/// `_messages_json`: the stored list, or None when unreadable or not a list.
fn messages_of(text: &str) -> Option<Value> {
    serde_json::from_str::<Value>(text).ok().filter(Value::is_array)
}

async fn rows_of(pool: &SqlitePool, username: &str) -> Result<Vec<Row5>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT client_id, title, model, messages, updated_at FROM conversations \
         WHERE username = ? ORDER BY updated_at DESC",
    )
    .bind(username)
    .fetch_all(pool)
    .await?;
    Ok(rows
        .iter()
        .map(|r| {
            (
                r.get::<String, _>("client_id"),
                r.get::<Option<String>, _>("title"),
                r.get::<Option<String>, _>("model"),
                r.get::<Option<String>, _>("messages").unwrap_or_default(),
                r.get::<Option<String>, _>("updated_at"),
            )
        })
        .collect())
}

/// `GET /api/conversations`: newest first. The FIRST conversation is always
/// served in full; the others are dropped to their metadata once the budget is
/// spent (`omitted_messages`). Unreadable rows are skipped, as in Flask.
pub async fn list(pool: &SqlitePool, username: &str) -> Result<Value, sqlx::Error> {
    let mut out = Vec::new();
    let mut size = 0usize;
    for (id, title, model, text, ts) in rows_of(pool, username).await? {
        let Some(messages) = messages_of(&text) else { continue };
        let mut item = Map::new();
        item.insert("id".into(), json!(id));
        item.insert("title".into(), json!(title));
        item.insert("model".into(), json!(model));
        item.insert("ts".into(), json!(ts));
        let chars = text.chars().count();
        if out.is_empty() || size + chars <= LIST_MAX_CHARS {
            item.insert("messages".into(), messages);
            size += chars;
        } else {
            item.insert("messages".into(), json!([]));
            item.insert("omitted_messages".into(), json!(true));
        }
        out.push(Value::Object(item));
    }
    Ok(json!({"conversations": out}))
}

/// `GET /api/conversations/<client_id>`: one whole conversation. None when it is
/// not the viewer's (404); `Err` text when the stored messages are unreadable (500).
pub async fn one(pool: &SqlitePool, username: &str, client_id: &str) -> Result<Result<Value, &'static str>, sqlx::Error> {
    let row = sqlx::query(
        "SELECT client_id, title, model, messages, updated_at FROM conversations \
         WHERE username = ? AND client_id = ?",
    )
    .bind(username)
    .bind(client_id)
    .fetch_optional(pool)
    .await?;
    let Some(r) = row else {
        return Ok(Err("introuvable"));
    };
    let text: String = r.get::<Option<String>, _>("messages").unwrap_or_default();
    match messages_of(&text) {
        None => Ok(Err("illisible")),
        Some(messages) => Ok(Ok(json!({"ok": true, "conversation": {
            "id": r.get::<String, _>("client_id"),
            "title": r.get::<Option<String>, _>("title"),
            "model": r.get::<Option<String>, _>("model"),
            "ts": r.get::<Option<String>, _>("updated_at"),
            "messages": messages,
        }}))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stored_messages_must_be_a_list() {
        assert!(messages_of("[1,2]").is_some());
        assert!(messages_of("{\"a\":1}").is_none());
        assert!(messages_of("not json").is_none());
    }
}
