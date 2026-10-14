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

// ── Save and delete (POST /conversations), parity with conversation_routes.py ──

pub const MSG_MAX_CHARS: usize = 400_000;
pub const CONV_MAX_CHARS: usize = 2_000_000;
pub const CONVERSATIONS_MAX: i64 = 30;
const MAX_FILES: usize = 40;
const MAX_FILE_CHARS: usize = 400_000;
const MAX_TOTAL_CHARS: usize = 1_200_000;
const IMAGES_PER_MESSAGE: usize = 4;
const IMAGE_MAX_CHARS: usize = 3_000_000;

/// A JSON value as Python's `json.dumps(..., ensure_ascii=False)` writes it:
/// `, ` and `: ` separators, non-ASCII kept, control characters escaped.
pub fn py_dumps(v: &Value) -> String {
    let mut out = String::new();
    write_py(v, &mut out);
    out
}

fn write_py_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_py(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => match n.as_f64() {
            Some(f) if n.is_f64() && f.fract() == 0.0 && f.is_finite() => out.push_str(&format!("{f:.1}")),
            _ => out.push_str(&n.to_string()),
        },
        Value::String(s) => write_py_str(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_py(x, out);
            }
            out.push(']');
        }
        Value::Object(o) => {
            out.push('{');
            for (i, (k, x)) in o.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_py_str(k, out);
                out.push_str(": ");
                write_py(x, out);
            }
            out.push('}');
        }
    }
}

/// Python's `str(x)` for a message's content.
fn py_str(v: Option<&Value>) -> String {
    match v {
        None => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) => "None".to_string(),
        Some(Value::Bool(b)) => (if *b { "True" } else { "False" }).to_string(),
        Some(other) => other.to_string(),
    }
}

/// file_tools.parse_files: {name: content} bounded and sanitized, first-seen order.
fn parse_files(raw: Option<&Value>) -> Vec<(String, String)> {
    let mut files: Vec<(String, String)> = Vec::new();
    let mut total = 0usize;
    let Some(Value::Array(list)) = raw else { return files };
    for f in list.iter().take(MAX_FILES) {
        let Some(obj) = f.as_object() else { continue };
        let name: String = py_str(obj.get("name").filter(|v| crate::keys::truthy(v))).trim().chars().take(200).collect();
        let Some(Value::String(content)) = obj.get("content") else { continue };
        let len = content.chars().count();
        if name.is_empty() || len > MAX_FILE_CHARS {
            continue;
        }
        if total + len > MAX_TOTAL_CHARS {
            break;
        }
        total += len;
        match files.iter_mut().find(|(n, _)| *n == name) {
            Some(entry) => entry.1 = content.clone(),
            None => files.push((name, content.clone())),
        }
    }
    files
}

/// conversation_routes.valid_images: image data: URLs only, bounded.
fn valid_images(m: &serde_json::Map<String, Value>) -> Vec<String> {
    let Some(Value::Array(imgs)) = m.get("images") else { return Vec::new() };
    imgs.iter()
        .take(IMAGES_PER_MESSAGE)
        .filter_map(Value::as_str)
        .filter(|u| u.chars().count() <= IMAGE_MAX_CHARS && is_image_data_url(u))
        .map(str::to_string)
        .collect()
}

/// `data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}`, whole string.
fn is_image_data_url(u: &str) -> bool {
    let Some(rest) = ["png", "jpeg", "webp", "gif"]
        .iter()
        .find_map(|t| u.strip_prefix(&format!("data:image/{t};base64,")))
    else {
        return false;
    };
    let body = rest.trim_end_matches('=');
    let pad = rest.len() - body.len();
    !body.is_empty() && pad <= 2 && body.chars().all(|c| c.is_ascii_alphanumeric() || c == '+' || c == '/')
}

/// The stored form of the messages: user and assistant only, content bounded,
/// the flags and attachments that matter on reload kept, the last 60.
pub fn stored_messages(messages: &[Value]) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::new();
    for m in messages {
        let Some(obj) = m.as_object() else { continue };
        let role = obj.get("role").and_then(Value::as_str).unwrap_or_default();
        if role != "user" && role != "assistant" {
            continue;
        }
        let mut item = serde_json::Map::new();
        item.insert("role".into(), Value::String(role.to_string()));
        let content: String = py_str(Some(obj.get("content").unwrap_or(&Value::String(String::new()))))
            .chars()
            .take(MSG_MAX_CHARS)
            .collect();
        item.insert("content".into(), Value::String(content));
        for flag in ["hidden", "isError", "truncated"] {
            if obj.get(flag).is_some_and(crate::keys::truthy) {
                item.insert(flag.into(), Value::Bool(true));
            }
        }
        if role == "assistant" {
            let edits = parse_files(obj.get("fileEdits"));
            if !edits.is_empty() {
                item.insert(
                    "fileEdits".into(),
                    Value::Array(edits.into_iter().map(|(n, c)| json!({"name": n, "content": c})).collect()),
                );
            }
        }
        if role == "user" {
            let imgs = valid_images(obj);
            if !imgs.is_empty() {
                item.insert("images".into(), json!(imgs));
            }
        }
        out.push(Value::Object(item));
    }
    let skip = out.len().saturating_sub(60);
    out.split_off(skip)
}

/// Drops the oldest messages until the serialized whole fits CONV_MAX_CHARS
/// (one kept at least), with Flask's incremental count then its exact check.
pub fn trim_to_budget(mut messages: Vec<Value>) -> Vec<Value> {
    let mut total = py_dumps(&Value::Array(messages.clone())).chars().count();
    while total > CONV_MAX_CHARS && messages.len() > 1 {
        let removed = messages.remove(0);
        total = total.saturating_sub(py_dumps(&removed).chars().count() + 2);
    }
    while py_dumps(&Value::Array(messages.clone())).chars().count() > CONV_MAX_CHARS && messages.len() > 1 {
        messages.remove(0);
    }
    messages
}

/// `action=save`: upsert the conversation, then keep only the 30 most recent.
pub async fn save(pool: &SqlitePool, username: &str, client_id: &str, title: &str, model: &str, messages: &[Value]) -> Result<(), sqlx::Error> {
    let stored = trim_to_budget(stored_messages(messages));
    let text = py_dumps(&Value::Array(stored));
    let now = crate::keys::iso(&chrono::Utc::now().naive_utc());
    let mut tx = pool.begin().await?;
    sqlx::query(
        "INSERT INTO conversations (username, client_id, title, model, messages, updated_at) VALUES (?, ?, ?, ?, ?, ?) \
         ON CONFLICT(username, client_id) DO UPDATE SET title = excluded.title, model = excluded.model, \
         messages = excluded.messages, updated_at = excluded.updated_at",
    )
    .bind(username)
    .bind(client_id)
    .bind(title)
    .bind(model)
    .bind(&text)
    .bind(&now)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "DELETE FROM conversations WHERE username = ? AND client_id NOT IN (\
         SELECT client_id FROM conversations WHERE username = ? ORDER BY updated_at DESC LIMIT ?)",
    )
    .bind(username)
    .bind(username)
    .bind(CONVERSATIONS_MAX)
    .execute(&mut *tx)
    .await?;
    tx.commit().await
}

/// `action=delete`: deletes ONE of the viewer's conversations; returns how many rows
/// went (0 for an unknown or someone else's id — no existence oracle).
pub async fn delete(pool: &SqlitePool, username: &str, client_id: &str) -> Result<u64, sqlx::Error> {
    let r = sqlx::query("DELETE FROM conversations WHERE username = ? AND client_id = ?")
        .bind(username)
        .bind(client_id)
        .execute(pool)
        .await?;
    Ok(r.rows_affected())
}

/// Python's `repr()` of the action, for the « Action inconnue » message.
pub fn py_repr(action: Option<&str>) -> String {
    match action {
        None => "None".to_string(),
        Some(s) if s.contains('\'') && !s.contains('"') => format!("\"{}\"", s.replace('\\', "\\\\")),
        Some(s) => format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'")),
    }
}

#[cfg(test)]
mod save_tests {
    use super::*;

    #[test]
    fn dumps_like_python() {
        let v = json!([{"role": "user", "content": "é\n\"x\"\u{01}"}]);
        assert_eq!(py_dumps(&v), "[{\"role\": \"user\", \"content\": \"é\\n\\\"x\\\"\\u0001\"}]");
    }

    #[test]
    fn stored_form_keeps_order_flags_and_last_60() {
        let mut msgs: Vec<Value> = (0..70).map(|i| json!({"role": "user", "content": i.to_string()})).collect();
        msgs.push(json!({"role": "system", "content": "x"}));
        msgs.push(json!({"role": "assistant", "content": "a", "hidden": 1, "fileEdits": [{"name": " f.py ", "content": "c"}]}));
        let out = stored_messages(&msgs);
        assert_eq!(out.len(), 60);
        let last = py_dumps(out.last().unwrap());
        assert_eq!(last, "{\"role\": \"assistant\", \"content\": \"a\", \"hidden\": true, \"fileEdits\": [{\"name\": \"f.py\", \"content\": \"c\"}]}");
    }

    #[test]
    fn data_urls_are_checked() {
        assert!(is_image_data_url("data:image/png;base64,QUJD"));
        assert!(is_image_data_url("data:image/png;base64,QQ=="));
        assert!(!is_image_data_url("data:image/svg+xml;base64,QUJD"));
        assert!(!is_image_data_url("data:image/png;base64,QQ==="));
    }

    #[test]
    fn repr_matches_python() {
        assert_eq!(py_repr(None), "None");
        assert_eq!(py_repr(Some("x")), "'x'");
        assert_eq!(py_repr(Some("it's")), "\"it's\"");
    }
}

/// `secrets.token_urlsafe(24)`: 24 random bytes, URL-safe base64 without padding.
pub fn share_token() -> Option<String> {
    use base64::Engine;
    let mut bytes = [0u8; 24];
    getrandom::getrandom(&mut bytes).ok()?;
    Some(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes))
}

/// `POST /conversations/share`: snapshots one of the viewer's conversations into a
/// share. None when it is not theirs.
pub async fn share(pool: &SqlitePool, username: &str, client_id: &str) -> Result<Option<String>, sqlx::Error> {
    let row = sqlx::query("SELECT title, model, messages FROM conversations WHERE username = ? AND client_id = ?")
        .bind(username)
        .bind(client_id)
        .fetch_optional(pool)
        .await?;
    let Some(r) = row else { return Ok(None) };
    let Some(token) = share_token() else {
        return Err(sqlx::Error::Protocol("no randomness for the share token".into()));
    };
    sqlx::query(
        "INSERT INTO conversation_shares (token, username, title, model, messages, created_at) VALUES (?, ?, ?, ?, ?, ?)",
    )
    .bind(&token)
    .bind(username)
    .bind(r.try_get::<Option<String>, _>("title")?)
    .bind(r.try_get::<Option<String>, _>("model")?)
    .bind(r.try_get::<Option<String>, _>("messages")?)
    .bind(crate::keys::iso(&chrono::Utc::now().naive_utc()))
    .execute(pool)
    .await?;
    Ok(Some(token))
}

#[cfg(test)]
mod token_tests {
    #[test]
    fn share_tokens_look_like_token_urlsafe_24() {
        let t = super::share_token().unwrap();
        assert_eq!(t.len(), 32);
        assert!(t.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'));
        assert_ne!(t, super::share_token().unwrap());
    }
}
