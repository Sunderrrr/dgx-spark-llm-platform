//! The settings page's read side, parity with dgx-portal settings_routes.py
//! `api_settings`: the viewer's MCP servers and skills, preferences, the lists of
//! choices, the account block, the 182-day activity and the real quotas.
//! The writes of the page live under other prefixes (served by the Flask backend until its removal).
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use chrono::{Duration, NaiveDate};
use serde_json::{json, Value};
use sqlx::{PgPool, Row, SqlitePool};

use crate::keys::AccountInfo;

pub const MAX_MCP_SERVERS: i64 = 16;
pub const MAX_SKILLS: i64 = 50;
pub const CONVERSATIONS_MAX: i64 = 30;
pub const CHAT_RATE_MAX: i64 = 20;
pub const CHAT_RATE_WINDOW: f64 = 60.0;
pub const ASR_RATE_MAX: i64 = 75;

pub const AVATAR_IDS: [&str; 15] = [
    "claude", "anthropic", "openai", "copilot", "gemini", "grok", "mistral", "deepseek", "qwen", "meta", "ollama",
    "huggingface", "perplexity", "nvidia", "langchain",
];
pub const THEME_IDS: [&str; 10] =
    ["neutral", "indigo", "violet", "rose", "ambre", "emeraude", "cyan", "ardoise", "brique", "prune"];
pub const LANGS: [&str; 2] = ["fr", "en"];

pub fn avatar_label(id: &str) -> &str {
    match id {
        "claude" => "Claude",
        "anthropic" => "Anthropic",
        "openai" => "ChatGPT",
        "copilot" => "GitHub Copilot",
        "gemini" => "Gemini",
        "grok" => "Grok",
        "mistral" => "Mistral",
        "deepseek" => "DeepSeek",
        "qwen" => "Qwen",
        "meta" => "Llama (Meta)",
        "ollama" => "Ollama",
        "huggingface" => "Hugging Face",
        "perplexity" => "Perplexity",
        "nvidia" => "NVIDIA",
        "langchain" => "LangChain",
        other => other,
    }
}

/// Python's round() to an integer: ties to even on the exact binary value. Pure.
fn py_round0(x: f64) -> i64 {
    format!("{x:.0}").parse().unwrap_or(x as i64)
}

fn now_epoch() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `_rate_used`: requests consumed in the current window (0 once it expired).
/// Every bucket shares the chat window, as in Flask.
pub async fn rate_used(db: &SqlitePool, username: &str, bucket: &str) -> i64 {
    let row = sqlx::query("SELECT fails, first_at FROM login_attempts WHERE key = ?")
        .bind(format!("{bucket}|{username}"))
        .fetch_optional(db)
        .await
        .ok()
        .flatten();
    match row {
        Some(r) => {
            let first_at: f64 = r.get::<Option<f64>, _>("first_at").unwrap_or(0.0);
            if now_epoch() - first_at > CHAT_RATE_WINDOW {
                0
            } else {
                r.get::<Option<i64>, _>("fails").unwrap_or(0)
            }
        }
        None => 0,
    }
}

/// `_account_activity`: daily tokens over the last `days` days (local time).
pub async fn activity(pg: Option<&PgPool>, username: &str, days: i64) -> Value {
    let empty = json!({"days": [], "total": 0, "prompt": 0, "completion": 0, "peak": 0,
                       "peak_day": null, "active_days": 0, "avg": 0});
    let Some(pool) = pg else { return empty };
    let umap = crate::spend::key_user_map(pool).await;
    let mine: Vec<String> = umap.iter().filter(|(_, u)| u.as_str() == username).map(|(k, _)| k.clone()).collect();
    if mine.is_empty() {
        return empty;
    }
    let tz = crate::spend::local_tz();
    let (today, since_utc) = {
        let since: Option<String> = sqlx::query_scalar(
            "SELECT ((((now() AT TIME ZONE $1)::date - $2::int)::timestamp AT TIME ZONE $1) AT TIME ZONE 'UTC')::text",
        )
        .bind(&tz)
        .bind((days - 1) as i32)
        .fetch_one(pool)
        .await
        .ok();
        let today: Option<String> = sqlx::query_scalar("SELECT (now() AT TIME ZONE $1)::date::text")
            .bind(&tz)
            .fetch_one(pool)
            .await
            .ok();
        match (today.and_then(|t| NaiveDate::parse_from_str(&t, "%Y-%m-%d").ok()), since) {
            (Some(t), Some(s)) => (t, s),
            _ => return empty,
        }
    };
    let rows: Vec<(String, Option<i64>, Option<i64>)> = match sqlx::query_as(
        "SELECT d::text, p::bigint, c::bigint FROM (\
         SELECT ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE $1)::date AS d, \
         SUM(COALESCE(prompt_tokens,0)) AS p, SUM(COALESCE(completion_tokens,0)) AS c \
         FROM \"LiteLLM_SpendLogs\" WHERE \"startTime\" >= $2::timestamp AND api_key = ANY($3) \
         GROUP BY d ORDER BY d) s",
    )
    .bind(&tz)
    .bind(&since_utc)
    .bind(&mine)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => return empty,
    };
    // by_day in the SQL order (ascending date), tokens = prompt + completion.
    let by_day: Vec<(String, i64, i64, i64)> = rows
        .into_iter()
        .map(|(d, p, c)| {
            let (p, c) = (p.unwrap_or(0), c.unwrap_or(0));
            (d, p, c, p + c)
        })
        .collect();
    let lookup: HashMap<&str, i64> = by_day.iter().map(|(d, _, _, t)| (d.as_str(), *t)).collect();
    let series: Vec<Value> = (0..days)
        .map(|i| {
            let d = (today - Duration::days(days - 1 - i)).format("%Y-%m-%d").to_string();
            json!({"date": d, "tokens": lookup.get(d.as_str()).copied().unwrap_or(0)})
        })
        .collect();
    let total: i64 = by_day.iter().map(|x| x.3).sum();
    let active: Vec<&(String, i64, i64, i64)> = by_day.iter().filter(|x| x.3 > 0).collect();
    // Python max(): the first of equal maxima wins; no rows → (None, 0).
    let peak = by_day.iter().fold(None::<&(String, i64, i64, i64)>, |best, x| match best {
        Some(b) if b.3 >= x.3 => Some(b),
        _ => Some(x),
    });
    json!({
        "days": series,
        "total": total,
        "prompt": by_day.iter().map(|x| x.1).sum::<i64>(),
        "completion": by_day.iter().map(|x| x.2).sum::<i64>(),
        "peak": peak.map_or(0, |p| p.3),
        "peak_day": peak.map(|p| p.0.clone()),
        "active_days": active.len(),
        "avg": if active.is_empty() { 0 } else { py_round0(total as f64 / active.len() as f64) },
    })
}

/// `_account_limits`: the quotas actually applied to the account.
pub async fn limits(
    db: &SqlitePool,
    username: &str,
    is_admin: bool,
    default_budget: f64,
    acct: &AccountInfo,
    servers: i64,
    skills: i64,
    running: &[String],
    ctx: Option<i64>,
) -> Value {
    let max_budget = if acct.exists { acct.max_budget } else { Some(default_budget) };
    let n_conv: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM conversations WHERE username = ?")
        .bind(username)
        .fetch_one(db)
        .await
        .unwrap_or(0);
    let used_spend = py_round0(acct.spend);
    json!([
        {"key": "budget", "label": "Budget de tokens",
         "desc": "Quota quotidien partagé par toutes tes clés API.",
         "used": used_spend, "max": if is_admin { Value::Null } else { json!(max_budget.map(py_round0).unwrap_or(0)) },
         "unit": "tokens", "unlimited": is_admin},
        {"key": "rate-support", "label": "Messages Support",
         "desc": format!("Maximum {CHAT_RATE_MAX} messages par minute."),
         "used": rate_used(db, username, "rl-support").await, "max": CHAT_RATE_MAX,
         "unit": "messages / min", "unlimited": false},
        {"key": "rate-playground", "label": "Messages Playground",
         "desc": format!("Maximum {CHAT_RATE_MAX} messages par minute."),
         "used": rate_used(db, username, "rl-playground").await, "max": CHAT_RATE_MAX,
         "unit": "messages / min", "unlimited": false},
        {"key": "rate-dictee", "label": "Dictées vocales",
         "desc": format!("Maximum {ASR_RATE_MAX} transcriptions par minute."),
         "used": rate_used(db, username, "rl-asr").await, "max": ASR_RATE_MAX,
         "unit": "transcriptions / min", "unlimited": false},
        {"key": "conversations", "label": "Conversations enregistrées",
         "desc": "Au-delà, les plus anciennes sont supprimées automatiquement.",
         "used": n_conv, "max": CONVERSATIONS_MAX, "unit": "conversations", "unlimited": false},
        {"key": "mcp", "label": "Serveurs MCP connectés",
         "desc": "Serveurs distants dont l'assistant peut utiliser les outils.",
         "used": servers, "max": Value::Null, "unit": "serveurs", "unlimited": true},
        {"key": "skills", "label": "Compétences définies",
         "desc": "Instructions réutilisables chargées à la demande.",
         "used": skills, "max": Value::Null, "unit": "compétences", "unlimited": true},
        {"key": "context", "label": "Fenêtre de contexte du modèle",
         "desc": running.first().cloned().unwrap_or_else(|| "Aucun modèle actif.".to_string()),
         "used": Value::Null, "max": ctx, "unit": "tokens", "unlimited": false},
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounding_is_python_round() {
        assert_eq!(py_round0(2.5), 2);
        assert_eq!(py_round0(3.5), 4);
        assert_eq!(py_round0(10.4), 10);
    }

    #[test]
    fn choice_lists_match_the_portal() {
        assert_eq!(AVATAR_IDS.len(), 15);
        assert_eq!(THEME_IDS.len(), 10);
        assert_eq!(avatar_label("openai"), "ChatGPT");
        assert_eq!(avatar_label("unknown-id"), "unknown-id");
    }
}
