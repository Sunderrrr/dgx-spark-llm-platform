//! Discord announcements (discord_notify.py): a private message to every user
//! who linked their account, sent by the bot configured in Admin → Config.
//! Entirely optional: without a bot token everything is inert, and per-user
//! failures (no mutual guild, DMs closed) are expected and swallowed.
use sqlx::SqlitePool;
use std::time::Duration;

/// `config.DISCORD_API`.
const DISCORD_API: &str = "https://discord.com/api/v10";

async fn setting(pool: &SqlitePool, key: &str) -> String {
    sqlx::query_scalar::<_, String>("SELECT value FROM settings WHERE key=?").bind(key).fetch_optional(pool).await.ok().flatten().unwrap_or_default()
}

/// `_discord_announce(kind, a, b)`: the four announcement kinds, in French.
pub fn announcement_text(kind: &str, a: &str, b: &str) -> Option<String> {
    let mut txt = match kind {
        "model_launch" => {
            let mut t = format!("🤖 **Nouveau modèle actif : {a}**");
            if !b.is_empty() {
                t += &format!("\n_Il remplace {b}._");
            }
            t
        }
        "model_add" => format!("✨ **Nouveau modèle disponible : {a}**"),
        "maintenance" if a == "on" => "🛠️ **Maintenance en cours** — le service peut être interrompu un moment.".to_string(),
        "maintenance" => "✅ **Fin de maintenance** — le service est rétabli.".to_string(),
        "site" => {
            let mut t = format!("📣 **{a}**");
            if !b.is_empty() {
                t += &format!("\n{b}");
            }
            t
        }
        _ => return None,
    };
    txt.push_str("\n\n— ");
    Some(txt)
}

/// `_discord_announce` + `discord_broadcast`: DM every linked user in the
/// background (~3 DMs/s), so the admin action is not held up.
pub async fn announce(pool: &SqlitePool, kind: &str, a: &str, b: &str) {
    let Some(mut txt) = announcement_text(kind, a, b) else { return };
    let token = setting(pool, "cfg_discord_bot_token").await;
    if token.is_empty() {
        return;
    }
    // The admin kill-switch: `get_setting('discord_dm', '1') != '1'`.
    let dm = sqlx::query_scalar::<_, String>("SELECT value FROM settings WHERE key='discord_dm'").fetch_optional(pool).await.ok().flatten();
    if dm.as_deref().unwrap_or("1") != "1" {
        return;
    }
    txt.push_str(&crate::chat::support_tools::platform_name(pool).await);
    let Ok(ids) = sqlx::query_scalar::<_, String>("SELECT CAST(discord_id AS TEXT) FROM discord_links").fetch_all(pool).await else { return };
    tokio::spawn(async move {
        for id in ids {
            send_dm(&token, &id, &txt).await;
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
    });
}

/// `_discord_send_dm`: open (or reuse) the DM channel, post once, one retry on 429.
async fn send_dm(token: &str, discord_id: &str, content: &str) -> bool {
    let c = crate::sidecars::http();
    let auth = format!("Bot {token}");
    let Ok(ch) = c.post(format!("{DISCORD_API}/users/@me/channels")).header("Authorization", &auth).json(&serde_json::json!({"recipient_id": discord_id})).timeout(Duration::from_secs(8)).send().await else {
        return false;
    };
    if !ch.status().is_success() {
        return false;
    }
    let Some(channel) = ch.json::<serde_json::Value>().await.ok().and_then(|v| v.get("id").and_then(|i| i.as_str().map(String::from))) else {
        return false;
    };
    let body = serde_json::json!({"content": content.chars().take(1900).collect::<String>()});
    let post = || c.post(format!("{DISCORD_API}/channels/{channel}/messages")).header("Authorization", &auth).json(&body).timeout(Duration::from_secs(8)).send();
    let Ok(r) = post().await else { return false };
    if r.status().as_u16() == 429 {
        let wait = r.json::<serde_json::Value>().await.ok().and_then(|v| v.get("retry_after").and_then(|x| x.as_f64())).unwrap_or(1.0).clamp(0.0, 5.0);
        tokio::time::sleep(Duration::from_secs_f64(wait)).await;
        return post().await.is_ok_and(|r| r.status().is_success());
    }
    r.status().is_success()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn texts_follow_flask() {
        assert_eq!(announcement_text("site", "T", "").as_deref(), Some("📣 **T**\n\n— "));
        assert_eq!(announcement_text("model_launch", "a", "b").as_deref(), Some("🤖 **Nouveau modèle actif : a**\n_Il remplace b._\n\n— "));
        assert!(announcement_text("maintenance", "off", "").unwrap().starts_with("✅"));
        assert_eq!(announcement_text("other", "", ""), None);
    }
}
