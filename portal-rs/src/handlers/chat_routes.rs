//! The playground's non-streamed model calls (chat_routes.py): the automatic
//! title and the summary of a conversation. Billed on the viewer's own key,
//! behind the same guards as the chat (maintenance, then a per-route bucket).
#![allow(unused_imports)]
use super::*;

/// Python's `repr()` of a JSON value — what `str(x)` gives for a list or dict.
pub(crate) fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".to_string(),
        Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        Value::Number(n) => match (n.as_i64(), n.as_f64()) {
            (Some(i), _) => i.to_string(),
            (None, Some(f)) if f.fract() == 0.0 && f.abs() < 1e16 => format!("{f:.1}"),
            _ => n.to_string(),
        },
        Value::String(s) => {
            let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
            let mut out = String::from(quote);
            for c in s.chars() {
                match c {
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    c if c == quote => {
                        out.push('\\');
                        out.push(c);
                    }
                    c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
                    c => out.push(c),
                }
            }
            out.push(quote);
            out
        }
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!("{{{}}}", o.iter().map(|(k, v)| format!("{}: {}", py_repr(&Value::String(k.clone())), py_repr(v))).collect::<Vec<_>>().join(", ")),
    }
}

/// `str(m.get('content', ''))`: a string as is, anything else as its repr.
fn py_str(v: Option<&Value>) -> String {
    match v {
        None => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => py_repr(other),
    }
}

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// The messages of the body kept for the model: user and assistant turns, content capped.
/// None when an entry is not an object (Flask's `m.get` raises: 500).
fn conversation(data: &serde_json::Map<String, Value>, cap: usize) -> Option<Vec<(String, String)>> {
    let list = match data.get("messages") {
        None => return Some(Vec::new()),
        Some(Value::Array(a)) => a.clone(),
        // Iterating an empty dict or string yields nothing; anything else raises.
        Some(Value::Object(o)) if o.is_empty() => Vec::new(),
        Some(Value::String(s)) if s.is_empty() => Vec::new(),
        Some(_) => return None,
    };
    let mut out = Vec::new();
    for m in &list {
        let m = m.as_object()?;
        let role = m.get("role").and_then(Value::as_str).unwrap_or("");
        if role == "user" || role == "assistant" {
            out.push((role.to_string(), take(&py_str(m.get("content")), cap)));
        }
    }
    Some(out)
}

/// `_non_stream`: one completion on the viewer's newest key; the text, or the refusal.
async fn non_stream(state: &AppState, username: &str, messages: Value, model: &str, max_tokens: i64, temperature: f64) -> Result<String, String> {
    let key: Option<String> = sqlx::query_scalar("SELECT key_value FROM api_keys WHERE username = ? ORDER BY created_at DESC LIMIT 1")
        .bind(username)
        .fetch_optional(state.db.pool())
        .await
        .ok()
        .flatten();
    let Some(key) = key else { return Err("Aucune clé API — crée une clé (budget de compte).".to_string()) };
    let base = std::env::var("LITELLM_URL").unwrap_or_else(|_| "http://litellm:4000".to_string());
    let call = async {
        let c = reqwest::Client::builder().connect_timeout(std::time::Duration::from_secs(10)).timeout(std::time::Duration::from_secs(130)).build()?;
        c.post(format!("{}/v1/chat/completions", base.trim_end_matches('/')))
            .bearer_auth(&key)
            .json(&json!({"model": model, "messages": messages, "stream": false, "temperature": temperature,
                          "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": false}}))
            .send()
            .await
    };
    let r = match call.await {
        Ok(r) => r,
        Err(e) => {
            // The detail (LiteLLM's internal address) stays in the logs.
            eprintln!("[chat] title/summary: LiteLLM call failed: {e}");
            return Err("Le modèle n'a pas répondu.".to_string());
        }
    };
    if !r.status().is_success() {
        return Err(format!("Erreur modèle ({})", r.status().as_u16()));
    }
    let data: Value = match r.json().await {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[chat] title/summary: unreadable LiteLLM answer: {e}");
            return Err("Le modèle n'a pas répondu.".to_string());
        }
    };
    let content = data.pointer("/choices/0/message/content").and_then(Value::as_str).unwrap_or("");
    Ok(content.trim_matches(memory::py_is_space).to_string())
}

/// The guards both routes share, then the model to ask (None: answer already decided).
async fn guarded_model(state: &AppState, headers: &HeaderMap, body: &axum::body::Bytes, bucket: &str) -> Result<(auth::SessionData, serde_json::Map<String, Value>, String), Response> {
    let session = csrf_then_viewer(state, headers, body).await?;
    if !session.is_admin && state.db.maintenance_active().await {
        return Err((StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "Mode maintenance en cours — réessaie plus tard."}))).into_response());
    }
    match guards::rate_limited(state.db.pool(), &session.username, bucket, guards::MEDIA_RATE_MAX, guards::MEDIA_RATE_WINDOW_S).await {
        Ok(0) => {}
        Ok(wait) => return Err((StatusCode::TOO_MANY_REQUESTS, Json(json!({"error": format!("Trop de requêtes. Réessaie dans {wait} s.")}))).into_response()),
        Err(_) => return Err(auth::html_status(StatusCode::INTERNAL_SERVER_ERROR)),
    }
    let data = json_object(headers, body);
    let running = models::running_models().await;
    let Some(first) = running.first() else { return Err((StatusCode::CONFLICT, Json(json!({"error": "no_model"}))).into_response()) };
    let model = data.get("model").and_then(Value::as_str).filter(|m| running.iter().any(|r| r == m)).unwrap_or(first).to_string();
    Ok((session, data, model))
}

/// POST /api/playground/title - a 3-8 word title for the conversation
pub async fn api_playground_title(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (session, data, model) = match guarded_model(&state, &headers, &body, "rl-titre").await {
        Ok(x) => x,
        Err(r) => return r,
    };
    let Some(msgs) = conversation(&data, 600) else { return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
    if msgs.is_empty() {
        return Json(json!({"title": ""})).into_response();
    }
    let tail = &msgs[msgs.len().saturating_sub(6)..];
    let user = tail.iter().map(|(r, c)| format!("{r}: {}", take(c, 200))).collect::<Vec<_>>().join("\n");
    let prompt = json!([
        {"role": "system", "content": "Résume en 3 à 8 mots le sujet de cette conversation. Réponds UNIQUEMENT avec le titre, en français, sans guillemets ni point final."},
        {"role": "user", "content": user},
    ]);
    match non_stream(&state, &session.username, prompt, &model, 40, 0.2).await {
        Ok(t) => Json(json!({"title": t})).into_response(),
        Err(e) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": e}))).into_response(),
    }
}

/// POST /api/playground/summarize - a short summary of the conversation
pub async fn api_playground_summarize(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (session, data, model) = match guarded_model(&state, &headers, &body, "rl-resume").await {
        Ok(x) => x,
        Err(r) => return r,
    };
    let Some(msgs) = conversation(&data, 3000) else { return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
    if msgs.is_empty() {
        return Json(json!({"summary": ""})).into_response();
    }
    let tail = &msgs[msgs.len().saturating_sub(12)..];
    let user = tail.iter().map(|(r, c)| format!("{r}: {c}")).collect::<Vec<_>>().join("\n\n");
    let prompt = json!([
        {"role": "system", "content": "Résume cette conversation en quelques phrases claires (français). Garde les décisions, fichiers créés et points importants. Sois concis."},
        {"role": "user", "content": user},
    ]);
    match non_stream(&state, &session.username, prompt, &model, 500, 0.2).await {
        Ok(s) => Json(json!({"summary": s})).into_response(),
        Err(e) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": e}))).into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repr_like_python() {
        assert_eq!(py_repr(&json!([{"type": "text", "text": "l'été"}])), r#"[{'type': 'text', 'text': "l'été"}]"#);
        assert_eq!(py_repr(&json!({"a": null, "b": true, "c": 2.0, "d": "x\ny"})), r"{'a': None, 'b': True, 'c': 2.0, 'd': 'x\ny'}");
    }

    #[test]
    fn conversation_keeps_user_and_assistant_turns() {
        let d = json!({"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "héllo"}, {"role": "assistant", "content": 5}]}).as_object().unwrap().clone();
        assert_eq!(conversation(&d, 3).unwrap(), vec![("user".to_string(), "hél".to_string()), ("assistant".to_string(), "5".to_string())]);
        let bad = json!({"messages": ["x"]}).as_object().unwrap().clone();
        assert!(conversation(&bad, 3).is_none());
    }
}
