//! The tool phase before the playground's answer (websearch_tools.py
//! `_tools_phase`): the model, called WITHOUT streaming on a short version of
//! the conversation, may search, read, generate, edit files, load a skill or
//! look into past conversations, a few rounds at most; every step is told to
//! the client as it happens. What was found goes back into the last user
//! message as TEXT — the final request carries no tool protocol.
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::chat::history::Msg;
use crate::chat::tools::{files, library, media, websearch};
use crate::chat::{data_frame, Out};
use crate::AppState;

const MAX_TOOL_ROUNDS: usize = 3;
const FILE_TOOL_ROUNDS: usize = 10;
const EDIT_SUMMARY_MAX: usize = 3000;
const FILE_TOOLS_MAX_DELAY: Duration = Duration::from_secs(300);
const FILE_TOOL_RESULT_MAX: usize = 40_000;
const TOOLS_MAX_DELAY: Duration = Duration::from_secs(60);
const TOOLS_MSG_MAX: usize = 1500;
const TOOLS_TOTAL_MAX: usize = 8000;
const TOOLS_LAST: usize = 6;
const TOOLS_RESULT_PROMPT: usize = 6000;
const TOOLS_PROMPT_MAX: usize = 40000;
const HEARTBEAT: Duration = Duration::from_secs(8);

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// What the phase may use.
pub struct Armed<'a> {
    pub web: bool,
    pub image: bool,
    pub video: bool,
    pub document: bool,
    pub files: Option<&'a mut files::Files>,
    pub skills: Option<&'a [library::SkillRow]>,
    pub history: bool,
}

/// What the phase brought back.
#[derive(Default)]
pub struct Outcome {
    pub step_log: Vec<Value>,
    pub findings: Vec<(String, String)>,
    pub changes: files::Changes,
    pub skills_loaded: Vec<String>,
    pub history_notes: Vec<String>,
    pub edit_summary: Option<String>,
}

/// `_tools_context`: the last few messages, each bounded (start and end kept).
pub fn tools_context(msgs: &[Msg]) -> Vec<Value> {
    let system: Vec<&Msg> = msgs.iter().take(1).filter(|m| m.role == "system").collect();
    let rest = &msgs[system.len()..];
    let (mut short, mut total) = (Vec::new(), 0usize);
    for m in rest[rest.len().saturating_sub(TOOLS_LAST)..].iter().rev() {
        let mut c = m.content.clone();
        let n = c.chars().count();
        if n > TOOLS_MSG_MAX {
            let half = TOOLS_MSG_MAX / 2;
            c = format!("{}\n…\n{}", take(&c, half), c.chars().skip(n - half).collect::<String>());
        }
        let cn = c.chars().count();
        if total + cn > TOOLS_TOTAL_MAX && !short.is_empty() {
            break;
        }
        total += cn;
        short.push(json!({"role": m.role, "content": c}));
    }
    short.reverse();
    system.iter().map(|m| json!({"role": m.role, "content": take(&m.content, TOOLS_MSG_MAX)})).chain(short).collect()
}

fn content_len(m: &Value) -> usize {
    match m.get("content") {
        None | Some(Value::Null) => 0,
        Some(Value::String(s)) => s.chars().count(),
        Some(v) => crate::chat::history::py_str(v).chars().count(),
    }
}

/// `_bound_tools_prompt`: drop the OLDEST complete tool rounds past the cap.
pub fn bound_tools_prompt(short: &mut Vec<Value>) {
    let mut total: usize = short.iter().map(content_len).sum();
    while total > TOOLS_PROMPT_MAX {
        let Some(idx) = short.iter().position(|m| m.get("role").and_then(Value::as_str) == Some("assistant") && m.get("tool_calls").is_some_and(crate::keys::truthy)) else { break };
        let mut end = idx + 1;
        while end < short.len() && short[end].get("role").and_then(Value::as_str) == Some("tool") {
            end += 1;
        }
        total -= short[idx..end].iter().map(content_len).sum::<usize>();
        short.drain(idx..end);
    }
}

/// A future awaited with `: heartbeat` comments every 8 s (the proxy cuts an idle stream).
async fn with_heartbeats<F: std::future::Future>(out: &Out, fut: F) -> F::Output {
    tokio::pin!(fut);
    loop {
        tokio::select! {
            r = &mut fut => return r,
            _ = tokio::time::sleep(HEARTBEAT) => { out.send(": heartbeat\n\n").await; }
        }
    }
}

async fn web_step(out: &Out, step: Value) {
    out.send(data_frame(&json!({"dgx_web": step}))).await;
}

/// `_tools_phase`.
#[allow(clippy::too_many_arguments)]
pub async fn run(state: &AppState, model: &str, msgs: &[Msg], user_key: &str, username: &str, attachments: &[String], mut armed: Armed<'_>, out: &Out) -> Outcome {
    let mut o = Outcome::default();
    let mut short = tools_context(msgs);
    let edit_ok = armed.files.as_ref().is_some_and(|f| !f.is_empty());
    let mut tools: Vec<Value> = Vec::new();
    if armed.web {
        tools.extend(websearch::tools());
    }
    if armed.image {
        tools.extend(media::image_tools());
    }
    if armed.video {
        tools.extend(media::video_tools());
    }
    if armed.document {
        tools.extend(media::document_tools());
    }
    if edit_ok {
        tools.extend(files::tools());
    }
    tools.extend(library::skill_tools(armed.skills.unwrap_or(&[])));
    if armed.history {
        tools.extend(library::history_tools());
    }
    if tools.is_empty() {
        return o;
    }
    if let Some(f) = armed.files.as_deref().filter(|_| edit_ok) {
        // Merged into THE system message: templates refuse a second one.
        let note = files::files_note(f);
        match short.first_mut().filter(|m| m.get("role").and_then(Value::as_str) == Some("system")) {
            Some(first) => {
                let c = first["content"].as_str().unwrap_or("").to_string();
                first["content"] = json!(format!("{c}\n\n{note}"));
            }
            None => short.insert(0, json!({"role": "system", "content": note})),
        }
    }
    let base = std::env::var("LITELLM_URL").unwrap_or_else(|_| "http://litellm:4000".to_string());
    let mut end = Instant::now() + if edit_ok { FILE_TOOLS_MAX_DELAY } else { TOOLS_MAX_DELAY };
    let rounds = if edit_ok { FILE_TOOL_ROUNDS } else { MAX_TOOL_ROUNDS };
    for round in 0..rounds {
        if Instant::now() > end {
            o.step_log.push(json!({"step": "timeout", "tool": "search", "error": "Recherche interrompue : trop longue."}));
            web_step(out, o.step_log.last().cloned().unwrap_or_default()).await;
            return o;
        }
        let body = json!({"model": model, "messages": short, "tools": tools,
            "tool_choice": if edit_ok && round == 0 { "required" } else { "auto" },
            "temperature": 0.2, "max_tokens": if edit_ok { 8192 } else { 1024 },
            "chat_template_kwargs": {"enable_thinking": false}});
        let secs = if edit_ok { 240 } else { 120 };
        let call = async {
            let c = reqwest::Client::builder().timeout(Duration::from_secs(secs)).build().map_err(|e| e.to_string())?;
            c.post(format!("{}/v1/chat/completions", base.trim_end_matches('/'))).bearer_auth(user_key).json(&body).send().await.map_err(|e| e.to_string())
        };
        let r = match with_heartbeats(out, call).await {
            Ok(r) => r,
            Err(e) => {
                eprintln!("[chat] tools {username}: decision call unreachable ({e})");
                web_step(out, json!({"step": "search_done", "tool": "litellm", "count": 0, "error": "assistant injoignable"})).await;
                return o;
            }
        };
        if !r.status().is_success() {
            let code = r.status().as_u16();
            eprintln!("[chat] tools {username}: tool decision refused (HTTP {code})");
            web_step(out, json!({"step": "search_done", "tool": "litellm", "count": 0, "error": format!("assistant en erreur ({code})")})).await;
            return o;
        }
        let Some(choice) = r.json::<Value>().await.ok().map(|v| v.pointer("/choices/0").cloned().unwrap_or_else(|| json!({}))) else {
            eprintln!("[chat] tools {username}: unreadable decision answer");
            web_step(out, json!({"step": "search_done", "tool": "litellm", "count": 0, "error": "réponse illisible"})).await;
            return o;
        };
        let message = choice.get("message").filter(|m| m.is_object()).cloned().unwrap_or_else(|| json!({}));
        let calls: Vec<Value> = message.get("tool_calls").and_then(Value::as_array).cloned().unwrap_or_default();
        if calls.is_empty() {
            // After applied edits, this text IS the short summary: it becomes the answer.
            let text = crate::memory::py_strip(message.get("content").and_then(Value::as_str).unwrap_or("")).to_string();
            if let Some(f) = armed.files.as_deref() {
                let edited = o.changes.iter().any(|(p, orig)| f.get(p) != orig.as_ref());
                if edit_ok && edited && !text.is_empty() && text.chars().count() <= EDIT_SUMMARY_MAX && !text.contains("```") {
                    o.edit_summary = Some(text);
                }
            }
            return o;
        }
        short.push(json!({"role": "assistant", "content": message.get("content").filter(|v| crate::keys::truthy(v)).cloned().unwrap_or(json!("")), "tool_calls": calls}));
        for call in calls.iter().take(4) {
            if Instant::now() > end {
                break;
            }
            let func = call.get("function").cloned().unwrap_or_else(|| json!({}));
            let name = func.get("name").and_then(Value::as_str).unwrap_or("").to_string();
            let args: Value = func.get("arguments").and_then(Value::as_str).filter(|s| !s.is_empty()).and_then(|s| serde_json::from_str(s).ok()).unwrap_or_else(|| json!({}));
            let args_obj = if args.is_object() { args.clone() } else { json!({}) };
            let call_id = call.get("id").cloned().unwrap_or(json!(""));
            let tool_msg = |content: String| json!({"role": "tool", "tool_call_id": call_id, "name": name, "content": content});
            if armed.history && (name == "search_conversations" || name == "recent_conversations") {
                let (result, _) = library::execute_history(state.db.pool(), &name, &args, username).await;
                o.history_notes.push(result.clone());
                web_step(out, json!({"step": "history", "tool": "history", "question": take(&args_obj.get("query").filter(|v| crate::keys::truthy(v)).map(crate::chat::history::py_str).unwrap_or_default(), 120)})).await;
                short.push(tool_msg(result));
                bound_tools_prompt(&mut short);
                continue;
            }
            if armed.skills.is_some_and(|s| !s.is_empty()) && (name == "use_skill" || name == "read_skill_file") {
                let arg = |k: &str| args_obj.get(k).map(crate::chat::history::py_str).unwrap_or_default();
                let (result, ok) = if name == "use_skill" {
                    library::use_skill(state.db.pool(), username, &arg("name")).await
                } else {
                    library::read_skill_file(state.db.pool(), username, &arg("name"), &arg("path")).await
                };
                let path = args_obj.get("path").filter(|v| crate::keys::truthy(v)).map(crate::chat::history::py_str);
                let mut step = json!({"step": "skill", "tool": "skills", "question": format!("{}{}", take(&args_obj.get("name").filter(|v| crate::keys::truthy(v)).map(crate::chat::history::py_str).unwrap_or_default(), 80), path.map(|p| format!(" · {p}")).unwrap_or_default())});
                if !ok {
                    step["error"] = json!("not found");
                } else {
                    o.skills_loaded.push(result.clone());
                }
                web_step(out, step).await;
                short.push(tool_msg(take(&result, FILE_TOOL_RESULT_MAX)));
                bound_tools_prompt(&mut short);
                continue;
            }
            if edit_ok && files::FILE_TOOL_NAMES.contains(&name.as_str()) {
                let mut step = files::announce(&name, &args_obj);
                let f = armed.files.as_deref_mut().expect("edit_ok implies files");
                let (result, error) = files::exec(&name, &args, f, &mut o.changes);
                if let Some(e) = error {
                    step["error"] = json!(e);
                }
                web_step(out, step).await;
                short.push(tool_msg(take(&result, FILE_TOOL_RESULT_MAX)));
                bound_tools_prompt(&mut short);
                continue;
            }
            let result = match name.as_str() {
                "generate_image" => {
                    web_step(out, media::announce_image(&args_obj)).await;
                    let r = media::exec_image(state, &args_obj, username, &mut o.step_log, out).await;
                    end = end.max(Instant::now() + TOOLS_MAX_DELAY);
                    r
                }
                "generate_video" => {
                    web_step(out, media::announce_video(&args_obj)).await;
                    let r = media::exec_video(state, &args_obj, username, &mut o.step_log, out).await;
                    end = end.max(Instant::now() + TOOLS_MAX_DELAY);
                    r
                }
                "lire_document" => {
                    web_step(out, media::announce_document(&args_obj)).await;
                    let r = media::exec_document(state, &args_obj, &mut o.step_log, attachments, out).await;
                    end = end.max(Instant::now() + TOOLS_MAX_DELAY);
                    r
                }
                _ => {
                    web_step(out, websearch::announce(&name, &args_obj)).await;
                    let mut log = std::mem::take(&mut o.step_log);
                    let r = with_heartbeats(out, async { let res = websearch::exec(&name, &args_obj, &mut log).await; (res, log) }).await;
                    o.step_log = r.1;
                    match r.0 {
                        Ok(text) => text,
                        Err(()) => {
                            eprintln!("[chat] tools {username}: {name} raised");
                            o.step_log.push(json!({"step": "search_done", "tool": "search", "count": 0, "error": "outil en panne"}));
                            web_step(out, o.step_log.last().cloned().unwrap_or_default()).await;
                            "La recherche a échoué (outil en panne). Dis-le tel quel à l'utilisateur.".to_string()
                        }
                    }
                }
            };
            web_step(out, o.step_log.last().cloned().unwrap_or_else(|| json!({}))).await;
            short.push(tool_msg(take(&result, TOOLS_RESULT_PROMPT)));
            o.findings.push((name.clone(), take(&result, 20000)));
            bound_tools_prompt(&mut short);
        }
    }
    o
}

/// The text re-injected into the last user message after the phase.
pub fn reinjection(o: &Outcome, files: Option<&files::Files>) -> String {
    let mut txt = websearch::findings_text(&o.findings);
    if let Some(f) = files {
        txt.push_str(&files::edits_note(f, &o.changes));
    }
    if !o.skills_loaded.is_empty() {
        txt.push_str(&take(&format!("\n\n---\n[Skill content loaded for this request — follow it:]\n\n{}", o.skills_loaded.join("\n\n")), 60000));
    }
    if !o.history_notes.is_empty() {
        txt.push_str(&take(&format!("\n\n---\n[From the user's past conversations — use it as context, cite the conversation title when you rely on it:]\n\n{}", o.history_notes.join("\n\n")), 30000));
    }
    txt
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_short_context_keeps_head_and_tail() {
        let msgs = vec![Msg::text("system", "s".repeat(2000)), Msg::text("user", format!("{}{}", "a".repeat(1000), "b".repeat(1000))), Msg::text("assistant", "ok")];
        let c = tools_context(&msgs);
        assert_eq!(c[0]["content"].as_str().unwrap().chars().count(), 1500);
        let u = c[1]["content"].as_str().unwrap();
        assert!(u.starts_with(&"a".repeat(750)) && u.ends_with(&"b".repeat(750)) && u.contains("\n…\n"));
        assert_eq!(c[2]["content"], "ok");
    }

    #[test]
    fn oldest_tool_rounds_go_first() {
        let big = "x".repeat(30000);
        let mut short = vec![
            json!({"role": "user", "content": "q"}),
            json!({"role": "assistant", "content": "", "tool_calls": [{}]}),
            json!({"role": "tool", "content": big}),
            json!({"role": "assistant", "content": "", "tool_calls": [{}]}),
            json!({"role": "tool", "content": "x".repeat(20000)}),
        ];
        bound_tools_prompt(&mut short);
        assert_eq!(short.len(), 3);
        assert_eq!(short[2]["content"].as_str().unwrap().len(), 20000);
    }
}
