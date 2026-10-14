//! A shared conversation's read-only page, parity with dgx-portal
//! conversation_routes.py `share_view` (`GET /c/<token>`). Reserved to signed-in
//! viewers; every view counts (the one write of this route).
use serde_json::Value;
use sqlx::{Row, SqlitePool};

use crate::docs::escape;

/// One shared snapshot: (title, model, messages JSON, views).
pub struct Share {
    pub title: String,
    pub model: String,
    pub messages: Value,
}

/// Loads the snapshot and counts the view. None when the token is unknown.
pub async fn load_and_count(pool: &SqlitePool, token: &str) -> Result<Option<Share>, sqlx::Error> {
    let row = sqlx::query("SELECT title, model, messages FROM conversation_shares WHERE token = ?")
        .bind(token)
        .fetch_optional(pool)
        .await?;
    let Some(r) = row else { return Ok(None) };
    let title: String = r.try_get::<Option<String>, _>("title")?.unwrap_or_default();
    let model: String = r.try_get::<Option<String>, _>("model")?.unwrap_or_default();
    let text: String = r.try_get::<Option<String>, _>("messages")?.unwrap_or_default();
    // Unreadable stored messages show an empty conversation, as in Flask.
    let messages = serde_json::from_str::<Value>(&text).unwrap_or_else(|_| Value::Array(Vec::new()));
    sqlx::query("UPDATE conversation_shares SET views = views + 1 WHERE token = ?")
        .bind(token)
        .execute(pool)
        .await?;
    Ok(Some(Share { title, model, messages }))
}

/// The page itself. Hidden messages never leave the share; only user and
/// assistant messages are shown. Pure: tested below.
pub fn page(share: &Share, platform: &str) -> String {
    let title = escape(&share.title);
    let model = escape(&share.model);
    let mut blocks: Vec<String> = Vec::new();
    for m in share.messages.as_array().into_iter().flatten() {
        let role = m.get("role").and_then(Value::as_str).unwrap_or_default();
        if role != "user" && role != "assistant" {
            continue;
        }
        // Python truthiness: `if m.get('hidden')`.
        if m.get("hidden").is_some_and(crate::keys::truthy) {
            continue;
        }
        let content_raw = match m.get("content") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Null) | None => String::new(),
            Some(other) => other.to_string(),
        };
        let content = escape(&content_raw).replace('\n', "<br>");
        let who = if role == "user" { "Vous" } else { "Assistant" };
        let color = if role == "user" { "#1d4ed8" } else { "#059669" };
        blocks.push(format!(
            "<div style=\"margin:0 0 18px\"><div style=\"font:600 13px system-ui;color:{color};margin-bottom:6px\">{who}</div>\
             <div style=\"font:400 15px/1.6 system-ui;color:#111827;white-space:pre-wrap\">{content}</div></div>"
        ));
    }
    let body = blocks.join("\n");
    let model_shown = if model.is_empty() { "—".to_string() } else { model };
    format!(
        "<!doctype html>\n<html lang=\"fr\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n\
<title>{title}</title></head>\n\
<body style=\"margin:0;background:#f9fafb;color:#111827;font:15px/1.6 system-ui\">\n\
<div style=\"max-width:760px;margin:0 auto;padding:32px 20px\">\n\
<h1 style=\"font:700 22px system-ui;margin:0 0 4px\">{title}</h1>\n\
<p style=\"margin:0 0 24px;color:#6b7280;font:13px system-ui\">Modèle : {model_shown}</p>\n\
{body}\n\
<p style=\"margin-top:32px;color:#9ca3af;font:12px system-ui\">Partagé depuis {platform}</p>\n\
</div></body></html>",
        platform = escape(platform),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn hidden_and_other_roles_stay_out_and_content_is_escaped() {
        let share = Share {
            title: "<t>".into(),
            model: "m".into(),
            messages: json!([
                {"role": "user", "content": "a<b\nc"},
                {"role": "system", "content": "secret"},
                {"role": "assistant", "content": "hidden one", "hidden": true},
                {"role": "assistant", "content": "ok"},
            ]),
        };
        let page = page(&share, "DGX");
        assert!(page.contains("a&lt;b<br>c"));
        assert!(!page.contains("secret"));
        assert!(!page.contains("hidden one"));
        assert!(page.contains("<title>&lt;t&gt;</title>"));
        assert!(page.contains("Partagé depuis DGX"));
    }
}
