//! POST /support/chat and /support/confirm (chat_routes.py): the Support
//! assistant. Each model turn is STREAMED; a `<think>` the model writes into
//! its content is held back until it closes; tool calls are gathered from the
//! deltas and run server-side, at most four rounds, then a last turn without
//! tools. Sensitive actions are only proposed (a token to the UI); after an
//! MCP or skill result entered the turn, they — and key creation — are
//! refused. A completed turn is kept as the thread and feeds the memory.
use std::time::{Duration, Instant};

use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};

use crate::chat::playground::notice;
use crate::chat::support_tools::{self as st, Route};
use crate::chat::{data_frame, gauges, history, memory_turn, reasoning, Out};
use crate::AppState;

const PING_S: u64 = 8;
const TRANSPORT_ERR: i32 = -1;
/// The stream broke mid-way: Python lets that exception reach `model_timeout`.
const STREAM_ERR: i32 = -2;

fn text_frame(t: &str) -> String {
    data_frame(&json!({"choices": [{"delta": {"content": t}}]}))
}

fn reasoning_frame(t: &str) -> String {
    data_frame(&json!({"choices": [{"delta": {"reasoning_content": t}}]}))
}

/// `_clean_reply`: reasoning blocks out, a final-answer marker honoured.
fn clean_reply(text: &str) -> String {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"(?is)<think>.*?</think>|<reasoning>.*?</reasoning>").expect("static regex"));
    let mut t = re.replace_all(text, "").into_owned();
    for marker in ["### Réponse", "Réponse finale :", "Final answer:", "Voici ma réponse"] {
        if let Some(i) = t.rfind(marker) {
            t = t[i + marker.len()..].to_string();
        }
    }
    crate::memory::py_strip(crate::memory::py_strip(&t).trim_start_matches(':')).to_string()
}

fn tool_event(id: &str, name: &str, target: Option<&str>, status: &str, duration_ms: Option<i64>, error: Option<&str>) -> String {
    let mut tc = json!({"id": id, "name": name, "status": status});
    if let Some(t) = target {
        tc["target"] = json!(t);
    }
    if let Some(d) = duration_ms {
        tc["duration_ms"] = json!(d);
    }
    if let Some(e) = error.filter(|e| !e.is_empty()) {
        tc["error"] = json!(e);
    }
    data_frame(&json!({"tool_call": tc}))
}

fn ns() -> u128 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0)
}

/// A thread message as the route keeps it.
#[derive(Clone)]
struct ThreadMsg {
    role: String,
    content: String,
    model: Option<String>,
    reasoning: Option<String>,
    reasoning_ms: Option<i64>,
}

impl ThreadMsg {
    fn api(&self) -> Value {
        let mut m = json!({"role": self.role, "content": self.content});
        if let Some(x) = &self.model {
            m["model"] = json!(x);
        }
        if let Some(x) = &self.reasoning {
            m["reasoning"] = json!(x);
        }
        if let Some(x) = self.reasoning_ms {
            m["reasoning_ms"] = json!(x);
        }
        m
    }
}

/// Everything decided before the stream.
struct Turn {
    username: String,
    fullname: Option<String>,
    is_admin: bool,
    model: String,
    user_key: String,
    history: Vec<ThreadMsg>,
    msgs: Vec<Value>,
    tools: Vec<Value>,
    routing: std::collections::HashMap<String, Route>,
    think: bool,
    mem_on: bool,
    ldap: String,
}

/// POST /support/chat
pub async fn support_chat(state: AppState, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match crate::handlers::csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let sse = |status: StatusCode, b: String| (status, [(axum::http::header::CONTENT_TYPE, "text/event-stream; charset=utf-8")], b).into_response();
    let data = crate::handlers::json_object(&headers, &body);
    let Some(list) = data.get("messages").and_then(Value::as_array).filter(|l| !l.is_empty()) else {
        return sse(StatusCode::BAD_REQUEST, notice("empty_message", json!({}), true));
    };
    if !session.is_admin && state.db.maintenance_active().await {
        return sse(StatusCode::OK, crate::handlers::sse_msg("Maintenance in progress — model access is temporarily suspended, please try again later."));
    }
    let mut hist: Vec<ThreadMsg> = Vec::new();
    for m in list {
        let Some(o) = m.as_object() else { return crate::auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
        let role = o.get("role").and_then(Value::as_str).unwrap_or("");
        if role != "user" && role != "assistant" {
            continue;
        }
        let truthy = |k: &str| o.get(k).filter(|v| crate::keys::truthy(v));
        hist.push(ThreadMsg {
            role: role.into(),
            content: o.get("content").map(history::py_str).unwrap_or_default().chars().take(4000).collect(),
            model: truthy("model").map(|v| history::py_str(v).chars().take(80).collect()),
            reasoning: truthy("reasoning").map(|v| history::py_str(v).chars().take(8000).collect()),
            reasoning_ms: truthy("reasoningMs").map(|v| crate::handlers::py_int(v).unwrap_or(0)),
        });
    }
    let hist: Vec<ThreadMsg> = hist[hist.len().saturating_sub(12)..].to_vec();
    let user = session.username.clone();
    match crate::guards::rate_limited(state.db.pool(), &user, "rl-support", 20, 60.0).await {
        Ok(0) => {}
        Ok(wait) => return sse(StatusCode::OK, notice("chat_rate_limited", json!({"wait": wait}), true)),
        Err(_) => return crate::auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
    let running = crate::models::running_models().await;
    let Some(model) = running.first().cloned() else { return sse(StatusCode::OK, notice("no_model_running", json!({}), true)) };
    let key: Option<String> = sqlx::query_scalar("SELECT key_value FROM api_keys WHERE username = ? ORDER BY created_at DESC LIMIT 1").bind(&user).fetch_optional(state.db.pool()).await.ok().flatten();
    let Some(user_key) = key else { return sse(StatusCode::OK, notice("no_api_key", json!({}), true)) };
    if let Some(reset) = gauges::quota_exceeded_reset(&state.db, state.spend.as_ref(), &user).await {
        return sse(StatusCode::OK, notice("quota_exceeded", json!({"reset": reset}), true));
    }
    let mut ctx = st::context(&state, &user, session.is_admin).await;
    let mem_on = crate::memory::enabled(state.db.pool(), &user).await;
    if mem_on {
        let memctx = memory_turn::inject_context(state.db.pool(), &user).await;
        if !memctx.is_empty() {
            ctx = format!("{ctx}\n\n{memctx}");
        }
    }
    let system = format!("{}\n\n### CONTEXTE\n{}\n{ctx}", st::support_text(state.db.pool(), st::SUPPORT_SYSTEM).await, history::timestamp());
    let mut msgs = vec![json!({"role": "system", "content": system})];
    msgs.extend(hist.iter().map(ThreadMsg::api));
    let ldap = crate::handlers::ldap_host(&state).await;
    let (extra, routing) = st::extra_tools(&state, &user, &ldap).await;
    let mut tools = st::tools(session.is_admin);
    tools.extend(extra);
    let last_user = hist.iter().rev().find(|m| m.role == "user").map(|m| m.content.clone()).unwrap_or_default();
    let turn = Turn {
        username: user, fullname: session.fullname.clone(), is_admin: session.is_admin, model, user_key, history: hist, msgs, tools, routing,
        think: reasoning::reasonably_complex(&last_user), mem_on, ldap,
    };
    let (tx, rx) = tokio::sync::mpsc::channel(256);
    let out = Out(tx);
    tokio::spawn(async move {
        let rid = gauges::inflight_start(state.db.pool(), &turn.username).await;
        run(&state, turn, &out).await;
        gauges::inflight_end(state.db.pool(), &rid).await;
    });
    (
        [
            (axum::http::header::CONTENT_TYPE, "text/event-stream; charset=utf-8"),
            (axum::http::header::CACHE_CONTROL, "no-cache"),
            (axum::http::HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        axum::body::Body::from_stream(tokio_stream::wrappers::ReceiverStream::new(rx)),
    )
        .into_response()
}

/// The reasoning seen during the whole turn.
#[derive(Default)]
struct Thought {
    text: String,
    start: Option<Instant>,
    end: Option<Instant>,
}

/// One streamed model turn: (content, tool calls, status). Err = the client left.
async fn run_turn(t: &Turn, with_tools: bool, out: &Out, thought: &mut Thought) -> Result<(String, Vec<Value>, i32), ()> {
    let mut body = json!({"model": t.model, "messages": t.msgs, "temperature": 0.3, "max_tokens": 4096, "chat_template_kwargs": {"enable_thinking": t.think}, "stream": true});
    if with_tools {
        body["tools"] = json!(t.tools);
        body["tool_choice"] = json!("auto");
    }
    let base = std::env::var("LITELLM_URL").unwrap_or_else(|_| "http://litellm:4000".to_string());
    let Ok(client) = reqwest::Client::builder().timeout(Duration::from_secs(180)).build() else { return Ok((String::new(), Vec::new(), TRANSPORT_ERR)) };
    let send = client.post(format!("{}/v1/chat/completions", base.trim_end_matches('/'))).bearer_auth(&t.user_key).json(&body).send();
    tokio::pin!(send);
    // The POST blocks until the prefill ends: beat time meanwhile.
    let r = loop {
        tokio::select! {
            r = &mut send => break r,
            _ = tokio::time::sleep(Duration::from_secs(PING_S)) => { if !out.send(": ping\n\n").await { return Err(()); } }
        }
    };
    let mut r = match r {
        Ok(r) => r,
        Err(_) => return Ok((String::new(), Vec::new(), TRANSPORT_ERR)),
    };
    if !r.status().is_success() {
        return Ok((String::new(), Vec::new(), i32::from(r.status().as_u16())));
    }
    let (mut parts, mut acc): (Vec<String>, std::collections::BTreeMap<i64, (Option<String>, String, String)>) = (Vec::new(), Default::default());
    let (mut decided, mut thinking) = (false, false);
    let (mut pending, mut think_buf) = (String::new(), String::new());
    let mut last_emit = Instant::now();
    let mut buf: Vec<u8> = Vec::new();
    let mut done = false;
    while !done {
        let chunk = match r.chunk().await {
            Ok(Some(c)) => c,
            Ok(None) => break,
            Err(_) => return Ok((String::new(), Vec::new(), STREAM_ERR)),
        };
        buf.extend_from_slice(&chunk);
        while let Some(pos) = buf.iter().position(|b| *b == b'\n') {
            let raw: Vec<u8> = buf.drain(..=pos).collect();
            let line = String::from_utf8_lossy(&raw[..raw.len() - 1]).trim_end_matches('\r').to_string();
            if last_emit.elapsed() > Duration::from_secs(10) {
                last_emit = Instant::now();
                if !out.send(": ping\n\n").await {
                    return Err(());
                }
            }
            let Some(payload) = line.strip_prefix("data:") else { continue };
            let payload = crate::memory::py_strip(payload);
            if payload == "[DONE]" {
                done = true;
                break;
            }
            let Ok(v) = serde_json::from_str::<Value>(payload) else { continue };
            let delta = v.pointer("/choices/0/delta").cloned().unwrap_or(Value::Null);
            if let Some(rc) = delta.get("reasoning_content").and_then(Value::as_str).filter(|s| !s.is_empty()) {
                thought.start.get_or_insert_with(Instant::now);
                thought.text.push_str(rc);
                last_emit = Instant::now();
                if !out.send(reasoning_frame(rc)).await {
                    return Err(());
                }
            }
            for tc in delta.get("tool_calls").and_then(Value::as_array).cloned().unwrap_or_default() {
                let idx = tc.get("index").and_then(Value::as_i64).unwrap_or(0);
                let slot = acc.entry(idx).or_insert((None, String::new(), String::new()));
                if let Some(id) = tc.get("id").and_then(Value::as_str).filter(|s| !s.is_empty()) {
                    slot.0 = Some(id.to_string());
                }
                if let Some(n) = tc.pointer("/function/name").and_then(Value::as_str).filter(|s| !s.is_empty()) {
                    slot.1 = n.to_string();
                }
                if let Some(a) = tc.pointer("/function/arguments").and_then(Value::as_str) {
                    slot.2.push_str(a);
                }
            }
            let Some(chunk) = delta.get("content").and_then(Value::as_str).filter(|s| !s.is_empty()) else { continue };
            if thought.end.is_none() && thought.start.is_some() {
                thought.end = Some(Instant::now());
            }
            parts.push(chunk.to_string());
            if thinking {
                // Hidden until </think>: everything after is the real answer.
                think_buf.push_str(chunk);
                if let Some(i) = think_buf.find("</think>") {
                    thinking = false;
                    let rest = think_buf[i + "</think>".len()..].trim_start_matches(crate::memory::py_is_space).to_string();
                    think_buf.clear();
                    if !rest.is_empty() {
                        last_emit = Instant::now();
                        if !out.send(text_frame(&rest)).await {
                            return Err(());
                        }
                    }
                }
                continue;
            }
            if decided {
                last_emit = Instant::now();
                if !out.send(text_frame(chunk)).await {
                    return Err(());
                }
                continue;
            }
            pending.push_str(chunk);
            let head = pending.trim_start_matches(crate::memory::py_is_space).to_string();
            let head6: String = head.chars().take(6).collect::<String>().to_lowercase();
            if head.to_lowercase().starts_with("<think") {
                thinking = true;
                decided = true;
                think_buf = std::mem::take(&mut pending);
            } else if head.chars().count() >= 12 || !"<think".starts_with(head6.as_str()) {
                decided = true;
                last_emit = Instant::now();
                let first = head.trim_start_matches(':').trim_start_matches(crate::memory::py_is_space).to_string();
                if !out.send(text_frame(&first)).await {
                    return Err(());
                }
                pending.clear();
            }
        }
    }
    let content = parts.concat();
    if thinking {
        // A reasoning never closed: the cleaned text, in a few frames.
        let cleaned: Vec<char> = clean_reply(&content).chars().collect();
        for c in cleaned.chunks(96) {
            out.send(text_frame(&c.iter().collect::<String>())).await;
        }
    } else if !pending.is_empty() {
        out.send(text_frame(&pending)).await;
    }
    let calls: Vec<Value> = acc.values().filter(|(_, n, _)| !n.is_empty()).map(|(id, n, a)| json!({
        "id": id.clone().unwrap_or_else(|| format!("tc-{}", ns())), "type": "function",
        "function": {"name": n, "arguments": if a.is_empty() { "{}".to_string() } else { a.clone() }}})).collect();
    Ok((content, calls, 200))
}

/// The turn's stream; then the thread and the memory when it completed.
async fn run(state: &AppState, mut t: Turn, out: &Out) {
    if !out.send(": open\n\n").await {
        return;
    }
    let mut response: Vec<String> = Vec::new();
    let mut thought = Thought::default();
    let finished = turns(state, &mut t, out, &mut response, &mut thought).await;
    let text = crate::memory::py_strip(&response.concat()).to_string();
    if text.is_empty() {
        return;
    }
    if !finished {
        eprintln!("[support] {}: reply interrupted, thread and memory not updated", t.username);
        return;
    }
    let reasoning_ms = thought.start.map(|s| thought.end.unwrap_or_else(Instant::now).duration_since(s).as_millis() as i64);
    let (pool, user, model, key, hist, mem_on) = (state.db.pool().clone(), t.username.clone(), t.model.clone(), t.user_key.clone(), t.history.clone(), t.mem_on);
    let reason = thought.text;
    tokio::spawn(async move {
        // `_finish_support_body`: the thread (last 40), then the memory.
        let mut thread: Vec<Value> = hist.iter().map(|m| {
            let mut o = json!({"role": m.role, "content": m.content});
            if let Some(x) = &m.model { o["model"] = json!(x); }
            if let Some(x) = &m.reasoning { o["reasoning"] = json!(x); }
            if let Some(x) = m.reasoning_ms.filter(|x| *x != 0) { o["reasoningMs"] = json!(x); }
            o
        }).collect();
        let mut last = json!({"role": "assistant", "content": text.chars().take(8000).collect::<String>(), "model": model});
        if !reason.is_empty() { last["reasoning"] = json!(reason.chars().take(8000).collect::<String>()); }
        if let Some(ms) = reasoning_ms.filter(|x| *x != 0) { last["reasoningMs"] = json!(ms); }
        thread.push(last);
        let kept = Value::Array(thread[thread.len().saturating_sub(40)..].to_vec());
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        if let Err(e) = sqlx::query("INSERT INTO support_thread (username, messages, updated_at) VALUES (?,?,?) ON CONFLICT(username) DO UPDATE SET messages=excluded.messages, updated_at=excluded.updated_at")
            .bind(&user).bind(crate::conversations::py_dumps(&kept)).bind(now).execute(&pool).await {
            eprintln!("[support] {user}: thread not saved ({e})");
        }
        if mem_on {
            let mut excerpts: Vec<(String, String)> = hist[hist.len().saturating_sub(4)..].iter().map(|m| (m.role.clone(), m.content.clone())).collect();
            excerpts.push(("assistant".into(), text.chars().take(4000).collect()));
            memory_turn::extract_and_save(pool, user, model, key, excerpts).await;
        }
    });
}

/// `_gen_inner`: the rounds; true when the reply completed.
async fn turns(state: &AppState, t: &mut Turn, out: &Out, response: &mut Vec<String>, thought: &mut Thought) -> bool {
    let mut use_tools = true;
    let mut streamed_any = false;
    let mut untrusted_seen = false;
    for _ in 0..4 {
        let Ok((content, calls, status)) = run_turn(t, use_tools, out, thought).await else { return false };
        if !content.is_empty() {
            response.push(content.clone());
        }
        if status == STREAM_ERR {
            out.send(notice("model_timeout", json!({}), false)).await;
            out.send("data: [DONE]\n\n").await;
            return false;
        }
        if status == TRANSPORT_ERR {
            out.send(notice("model_unreachable", json!({}), false)).await;
            out.send("data: [DONE]\n\n").await;
            return false;
        }
        if status != 200 && use_tools {
            use_tools = false; // a model without tool support: once more without
            continue;
        }
        if status != 200 {
            out.send(notice("model_replied_error", json!({"status": status}), false)).await;
            out.send("data: [DONE]\n\n").await;
            return false;
        }
        streamed_any = streamed_any || !crate::memory::py_strip(&content).is_empty();
        if calls.is_empty() {
            if !streamed_any {
                out.send(notice("empty_reply", json!({}), false)).await;
            }
            out.send("data: [DONE]\n\n").await;
            return streamed_any;
        }
        t.msgs.push(json!({"role": "assistant", "content": content, "tool_calls": calls}));
        for tc in &calls {
            let fname = tc.pointer("/function/name").and_then(Value::as_str).unwrap_or("").to_string();
            let tc_id = tc.get("id").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string).unwrap_or_else(|| format!("tc-{}", ns()));
            let args: Value = tc.pointer("/function/arguments").and_then(Value::as_str).and_then(|a| serde_json::from_str(if a.is_empty() { "{}" } else { a }).ok()).unwrap_or_else(|| json!({}));
            let args_obj = if args.is_object() { args.clone() } else { json!({}) };
            let a = |k: &str| crate::memory::py_strip(&args_obj.get(k).filter(|v| crate::keys::truthy(v)).map(history::py_str).unwrap_or_default()).to_string();
            let route = t.routing.get(&fname);
            let (label, target) = match route {
                Some(Route::Mcp { server_name, tool_name, .. }) => (format!("MCP · {server_name} · {tool_name}"), None),
                Some(Route::Chats) => (format!("Historique · {}", if a("query").is_empty() { fname.clone() } else { a("query") }), None),
                Some(Route::Skill) => {
                    let path = (fname == "read_skill_file").then(|| args_obj.get("path").map(history::py_str).unwrap_or_else(|| "None".into()));
                    (format!("Compétence · {}{}", a("name"), path.as_ref().map(|p| format!(" · {p}")).unwrap_or_default()), None)
                }
                None => (st::label(&fname), st::target(&fname, &args_obj)),
            };
            let tool_msg = |content: String| json!({"role": "tool", "tool_call_id": tc.get("id").cloned().unwrap_or(Value::Null), "content": content});
            if untrusted_seen && st::refused_if_external(&fname) {
                out.send(tool_event(&tc_id, &label, target.as_deref(), "running", None, None)).await;
                out.send(tool_event(&tc_id, &label, target.as_deref(), "error", Some(0), Some("Action bloquée après lecture d'un contenu externe."))).await;
                t.msgs.push(tool_msg("REFUSÉ : cette action est bloquée dans ce tour parce que du contenu externe (MCP/compétence) a été lu. Explique-le à l'utilisateur et invite-le à faire l'action lui-même depuis l'interface.".into()));
                continue;
            }
            if route.is_none() && st::GUARDED_TOOLS.contains(&fname.as_str()) {
                let token = st::create_pending(state.db.pool(), &t.username, &fname, &args_obj, &label, target.as_deref()).await;
                out.send(data_frame(&json!({"dgx_confirm": {"token": token, "tool": fname, "label": label, "target": target}}))).await;
                t.msgs.push(tool_msg("NON EXÉCUTÉ : l'action est en attente de la confirmation de l'utilisateur dans l'interface (bouton « Confirmer » affiché ci-dessous). Ne la rappelle pas et n'affirme pas qu'elle est faite : explique en une phrase ce qui va se passer et termine ta réponse.".into()));
                continue;
            }
            if route.is_some() {
                untrusted_seen = true;
            }
            if !out.send(tool_event(&tc_id, &label, target.as_deref(), "running", None, None)).await {
                return false;
            }
            let started = Instant::now();
            let pool = state.db.pool();
            let (res, ok) = match route {
                Some(Route::Mcp { server_id, tool_name, .. }) => st::exec_mcp(pool, *server_id, tool_name, &args, &t.username, &t.ldap).await,
                Some(Route::Chats) => crate::chat::tools::library::execute_history(pool, &fname, &args, &t.username).await,
                Some(Route::Skill) => {
                    if fname == "read_skill_file" {
                        crate::chat::tools::library::read_skill_file(pool, &t.username, &a("name"), &args_obj.get("path").map(history::py_str).unwrap_or_else(|| "None".into())).await
                    } else {
                        crate::chat::tools::library::use_skill(pool, &t.username, &a("name")).await
                    }
                }
                None => st::exec(state, &fname, &args_obj, &t.username, t.fullname.as_deref(), t.is_admin).await,
            };
            let ms = started.elapsed().as_millis() as i64;
            out.send(tool_event(&tc_id, &label, target.as_deref(), if ok { "complete" } else { "error" }, Some(ms), if ok { None } else { Some(res.as_str()) })).await;
            t.msgs.push(tool_msg(res));
        }
    }
    // Too many tool rounds: a last answer WITHOUT tools.
    let Ok((content, _, status)) = run_turn(t, false, out, thought).await else { return false };
    if status == STREAM_ERR {
        out.send(notice("model_timeout", json!({}), false)).await;
        out.send("data: [DONE]\n\n").await;
        return false;
    }
    let finished = if status != 200 {
        out.send(notice("model_busy", json!({}), false)).await;
        false
    } else if crate::memory::py_strip(&content).is_empty() {
        out.send(notice("reformulate", json!({}), false)).await;
        false
    } else {
        response.push(content);
        true
    };
    out.send("data: [DONE]\n\n").await;
    finished
}

/// POST /support/confirm - run (or cancel) an action the model proposed
pub async fn support_confirm(state: AppState, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match crate::handlers::csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let answer = |code: StatusCode, v: Value| (code, axum::Json(v)).into_response();
    let data = crate::handlers::json_object(&headers, &body);
    let token = data.get("token").and_then(Value::as_str).map(|t| crate::memory::py_strip(t).to_string()).unwrap_or_default();
    if token.is_empty() {
        return answer(StatusCode::BAD_REQUEST, json!({"error": "Demande inconnue."}));
    }
    let user = &session.username;
    let pool = state.db.pool();
    let Some((tool, args, label)) = st::pending(pool, user, &token).await else {
        return answer(StatusCode::NOT_FOUND, json!({"error": "Cette demande a expiré (ou a déjà été traitée)."}));
    };
    let label = label.filter(|l| !l.is_empty()).unwrap_or_else(|| tool.clone());
    if data.get("cancel").is_some_and(crate::keys::truthy) {
        st::close(pool, user, &token, "cancelled").await;
        crate::sessions::log_audit(pool, user, "support.action_annulee", &label).await;
        return answer(StatusCode::OK, json!({"ok": true, "message": format!("Action annulée : {label}.")}));
    }
    if !st::close(pool, user, &token, "done").await {
        return answer(StatusCode::CONFLICT, json!({"error": "Cette demande a déjà été traitée."}));
    }
    let args: Value = serde_json::from_str(args.as_deref().filter(|a| !a.is_empty()).unwrap_or("{}")).unwrap_or_else(|_| json!({}));
    let args = if args.is_object() { args } else { json!({}) };
    let (res, ok) = st::exec(&state, &tool, &args, user, session.fullname.as_deref(), session.is_admin).await;
    crate::sessions::log_audit(pool, user, &format!("support.{tool}"), &format!("{label} — {}", if ok { "ok" } else { "échec" })).await;
    answer(StatusCode::OK, json!({"ok": ok, "message": res}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replies_are_cleaned_like_python() {
        assert_eq!(clean_reply("<think>x</think>  : Bonjour"), "Bonjour");
        assert_eq!(clean_reply("blabla ### Réponse : oui"), "oui");
        assert_eq!(clean_reply("<THINK>a\nb</think>ok"), "ok");
    }
}
