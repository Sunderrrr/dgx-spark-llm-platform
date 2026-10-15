//! The playground's conversation as the model will read it (chat_routes.py):
//! messages parsed and bounded, images kept for a model that sees, outdated
//! versions of a file replaced by a line, whole oldest turns dropped to fit
//! the window, the clock appended, the API shape built.
use serde_json::{json, Map, Value};

use crate::conversations::MSG_MAX_CHARS;

/// Images sent to the model per request, all messages combined (most recent kept).
pub const MAX_IMAGES_PER_REQUEST: usize = 8;
/// Attachments kept for the document-reading tool.
pub const MAX_ATTACHMENTS: usize = 12;
/// An image weighs this many characters in the window estimates.
pub const IMAGE_WEIGHT_CHARS: usize = 1500 * 3;

#[derive(Debug, Clone, PartialEq)]
pub struct Msg {
    pub role: String,
    pub content: String,
    pub images: Vec<String>,
}

impl Msg {
    pub fn text(role: &str, content: impl Into<String>) -> Msg {
        Msg { role: role.into(), content: content.into(), images: Vec::new() }
    }
}

/// Python's `str(x)` of a JSON value.
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => crate::handlers::py_repr(other),
    }
}

/// The request's messages: user and assistant turns, content bounded,
/// a user turn's valid images alongside.
pub fn parse_messages(data: &Map<String, Value>) -> Vec<Msg> {
    let Some(list) = data.get("messages").and_then(Value::as_array) else { return Vec::new() };
    let mut out = Vec::new();
    for m in list {
        let Some(o) = m.as_object() else { continue };
        let role = match o.get("role").and_then(Value::as_str) {
            Some(r @ ("user" | "assistant")) => r.to_string(),
            _ => continue,
        };
        let content = o.get("content").map(py_str).unwrap_or_default().chars().take(MSG_MAX_CHARS).collect();
        let images = if role == "user" { crate::conversations::valid_images(o) } else { Vec::new() };
        out.push(Msg { role, content, images });
    }
    out
}

/// The attachments the document tool may read: every joined image, the newest last.
pub fn attachments(history: &[Msg]) -> Vec<String> {
    let all: Vec<String> = history.iter().flat_map(|h| h.images.iter().cloned()).collect();
    all[all.len().saturating_sub(MAX_ATTACHMENTS)..].to_vec()
}

/// Images dropped for a model that does not see, else bounded to the most recent ones.
pub fn bound_images(history: &mut [Msg], sees: bool) {
    let mut remaining = if sees { MAX_IMAGES_PER_REQUEST } else { 0 };
    for h in history.iter_mut().rev() {
        if h.images.is_empty() {
            continue;
        }
        let keep = h.images.len().min(remaining);
        h.images = h.images[h.images.len() - keep..].to_vec();
        remaining -= keep;
    }
}

/// `_weight`: a message's weight in the window estimates (characters).
pub fn weight(m: &Msg) -> usize {
    m.content.chars().count() + IMAGE_WEIGHT_CHARS * m.images.len()
}

fn announcement() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"`([\w./-]+\.[A-Za-z0-9]{1,6})`[^\n]{0,40}$").expect("static regex"))
}

fn fence() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"```([^\n`]*)\n([\s\S]*?)```").expect("static regex"))
}

/// `_file_key`: under what name a code block is known.
pub fn file_key(info: &str, before: &str) -> String {
    let info = crate::memory::py_strip(info);
    let first = info.split(crate::memory::py_is_space).find(|s| !s.is_empty()).unwrap_or("");
    if first.contains('.') {
        return first.to_string();
    }
    // « Voici `index.html` : » just above the block.
    let lines: Vec<&str> = before.split('\n').collect();
    for line in lines[lines.len().saturating_sub(4)..].iter().rev() {
        if let Some(c) = announcement().captures(crate::memory::py_strip(line)) {
            return c[1].to_string();
        }
    }
    if first.is_empty() { "bloc".to_string() } else { first.to_string() }
}

/// Byte offset → the text before it.
fn before(content: &str, at: usize) -> &str {
    &content[..at]
}

/// `_without_stale_versions`: only the LAST version of each long file (≥ 2000
/// characters) stays in the assistant turns; the older ones become a line.
pub fn without_stale_versions(history: &[Msg]) -> Vec<Msg> {
    let mut last: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for (i, m) in history.iter().enumerate() {
        if m.role != "assistant" {
            continue;
        }
        for c in fence().captures_iter(&m.content) {
            if c[2].chars().count() < 2000 {
                continue;
            }
            let start = c.get(0).map_or(0, |x| x.start());
            last.insert(file_key(&c[1], before(&m.content, start)), i);
        }
    }
    if last.is_empty() {
        return history.to_vec();
    }
    history
        .iter()
        .enumerate()
        .map(|(i, m)| {
            if m.role != "assistant" {
                return m.clone();
            }
            let content = &m.content;
            let replaced = fence().replace_all(content, |c: &regex::Captures| {
                if c[2].chars().count() < 2000 {
                    return c[0].to_string();
                }
                let start = c.get(0).map_or(0, |x| x.start());
                let key = file_key(&c[1], before(content, start));
                if last.get(&key) == Some(&i) {
                    c[0].to_string()
                } else {
                    format!("```\n[version précédente de `{key}` retirée du contexte — la version à jour figure plus bas dans la conversation]\n```")
                }
            });
            Msg { content: replaced.into_owned(), ..m.clone() }
        })
        .collect()
}

/// `_history_for_model`: WHOLE messages, the oldest dropped until it fits.
pub fn history_for_model(history: &[Msg], system: &str, ctx: Option<i64>) -> Vec<Msg> {
    let mut history = without_stale_versions(history);
    let Some(ctx) = ctx.filter(|c| *c != 0) else { return history };
    // ~3 characters per token (pessimistic), room kept to answer.
    let budget = 20_000i64.max((ctx - 8192) * 3);
    let mut total: i64 = history.iter().map(|m| weight(m) as i64).sum::<i64>() + system.chars().count() as i64;
    while history.len() > 2 && total > budget {
        total -= weight(&history[0]) as i64;
        history.remove(0);
    }
    history
}

/// `_without_inline_think`: an assistant turn without a `<think>` the model wrote into it.
pub fn without_inline_think(text: &str) -> String {
    let head: String = text.trim_start_matches(crate::memory::py_is_space).chars().take(7).collect();
    if head.to_lowercase() != "<think>" {
        return text.to_string();
    }
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"(?is)^\s*<think>.*?(?:</think>\s*|\n?\z)").expect("static regex"));
    let out = re.replacen(text, 1, "");
    let out = crate::memory::py_strip(&out);
    if out.is_empty() { "…".to_string() } else { out.to_string() }
}

/// `_msgs_api`: the messages in API shape, images as `image_url` parts.
pub fn msgs_api(msgs: &[Msg]) -> Value {
    Value::Array(
        msgs.iter()
            .map(|m| {
                let content = if m.role == "assistant" { without_inline_think(&m.content) } else { m.content.clone() };
                if m.images.is_empty() {
                    return json!({"role": m.role, "content": content});
                }
                let mut parts: Vec<Value> = m.images.iter().map(|u| json!({"type": "image_url", "image_url": {"url": u}})).collect();
                if !content.is_empty() {
                    parts.push(json!({"type": "text", "text": content}));
                }
                json!({"role": m.role, "content": parts})
            })
            .collect(),
    )
}

/// `_last_user_message`: the last user turn's text.
pub fn last_user_message(history: &[Msg]) -> String {
    history.iter().rev().find(|h| h.role == "user").map(|h| h.content.clone()).unwrap_or_default()
}

/// `_timestamp`: « Nous sommes le vendredi 2 octobre 2026 à 15:42 (Europe/Paris). »
pub fn timestamp() -> String {
    let tz_name = std::env::var("TZ_DISPLAY").unwrap_or_else(|_| "Europe/Paris".to_string());
    let tz: chrono_tz::Tz = tz_name.parse().unwrap_or(chrono_tz::Europe::Paris);
    stamp(chrono::Utc::now().with_timezone(&tz).naive_local(), &tz_name)
}

pub fn stamp(n: chrono::NaiveDateTime, tz_name: &str) -> String {
    use chrono::{Datelike, Timelike};
    const DAYS: [&str; 7] = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];
    const MONTHS: [&str; 12] = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"];
    format!(
        "Nous sommes le {} {} {} {} à {:02}:{:02} ({tz_name}).",
        DAYS[n.weekday().num_days_from_monday() as usize],
        n.day(),
        MONTHS[n.month0() as usize],
        n.year(),
        n.hour(),
        n.minute()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msgs(v: &Value) -> Vec<Msg> {
        v.as_array()
            .unwrap()
            .iter()
            .map(|m| Msg {
                role: m["role"].as_str().unwrap().into(),
                content: m["content"].as_str().unwrap().into(),
                images: m.get("images").and_then(Value::as_array).map(|a| a.iter().map(|x| x.as_str().unwrap().to_string()).collect()).unwrap_or_default(),
            })
            .collect()
    }

    fn to_value(h: &[Msg]) -> Value {
        Value::Array(h.iter().map(|m| {
            let mut o = json!({"role": m.role, "content": m.content});
            if !m.images.is_empty() {
                o["images"] = json!(m.images);
            }
            o
        }).collect())
    }

    /// Outputs computed by dgx-portal/chat_routes.py on the same inputs.
    #[test]
    fn matches_python_on_the_fixture() {
        let fx: Value = serde_json::from_str(include_str!("../../tests/fixtures/chat_history.json")).unwrap();
        for case in fx["stale"].as_array().unwrap() {
            assert_eq!(to_value(&without_stale_versions(&msgs(&case["in"]))), case["out"], "stale {}", case["name"]);
        }
        for case in fx["window"].as_array().unwrap() {
            let ctx = case["ctx"].as_i64();
            let got = history_for_model(&msgs(&case["in"]), case["system"].as_str().unwrap(), ctx);
            assert_eq!(to_value(&got), case["out"], "window {}", case["name"]);
        }
        for case in fx["api"].as_array().unwrap() {
            assert_eq!(msgs_api(&msgs(&case["in"])), case["out"], "api {}", case["name"]);
        }
        for case in fx["think"].as_array().unwrap() {
            assert_eq!(without_inline_think(case["in"].as_str().unwrap()), case["out"].as_str().unwrap(), "think {:?}", case["in"]);
        }
        for case in fx["key"].as_array().unwrap() {
            assert_eq!(file_key(case["info"].as_str().unwrap(), case["before"].as_str().unwrap()), case["out"].as_str().unwrap());
        }
        for case in fx["parse"].as_array().unwrap() {
            let got = parse_messages(case["in"].as_object().unwrap());
            assert_eq!(to_value(&got), case["out"], "parse {}", case["name"]);
        }
    }

    #[test]
    fn the_clock_in_french() {
        let n = chrono::NaiveDate::from_ymd_opt(2026, 10, 2).unwrap().and_hms_opt(15, 42, 0).unwrap();
        assert_eq!(stamp(n, "Europe/Paris"), "Nous sommes le vendredi 2 octobre 2026 à 15:42 (Europe/Paris).");
    }

    #[test]
    fn images_are_bounded_newest_first() {
        let mut h = vec![
            Msg { role: "user".into(), content: "a".into(), images: (0..6).map(|i| format!("i{i}")).collect() },
            Msg { role: "user".into(), content: "b".into(), images: (6..11).map(|i| format!("i{i}")).collect() },
        ];
        bound_images(&mut h, true);
        assert_eq!(h[1].images.len(), 5);
        assert_eq!(h[0].images, vec!["i3", "i4", "i5"]);
        bound_images(&mut h, false);
        assert!(h.iter().all(|m| m.images.is_empty()));
    }
}
