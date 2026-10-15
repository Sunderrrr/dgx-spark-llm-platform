//! Support thread and feedback, and the playground's model data
//! (chat_routes.py): the parts of the chat pages that are plain reads and writes.
#![allow(unused_imports)]
use super::*;

/// GET /api/support/thread - the viewer's kept Support thread
pub async fn api_support_thread(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let row = sqlx::query("SELECT messages, updated_at FROM support_thread WHERE username = ?")
        .bind(&session.username)
        .fetch_optional(state.db.pool())
        .await;
    let row = match row {
        Ok(r) => r,
        Err(_) => return db_down_500(),
    };
    let Some(row) = row else { return Json(json!({"messages": []})).into_response() };
    let raw: Option<String> = row.try_get("messages").ok().flatten();
    // `json.loads(row['messages'] or '[]')`, any failure → []
    let msgs = serde_json::from_str::<Value>(raw.as_deref().filter(|s| !s.is_empty()).unwrap_or("[]")).unwrap_or(json!([]));
    let updated_at = row
        .try_get::<Option<f64>, _>("updated_at")
        .ok()
        .flatten()
        .map(|f| json!(f))
        .unwrap_or(Value::Null);
    Json(json!({"messages": msgs, "updated_at": updated_at})).into_response()
}

/// Flask lets a database error escape as its 500 page.
fn db_down_500() -> Response {
    auth::html_status(StatusCode::INTERNAL_SERVER_ERROR)
}

/// POST /support/thread/clear - « Nouvelle conversation »: erase the kept thread
pub async fn support_thread_clear(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match sqlx::query("DELETE FROM support_thread WHERE username = ?").bind(&session.username).execute(state.db.pool()).await {
        Ok(_) => Json(json!({"ok": true})).into_response(),
        Err(_) => db_down_500(),
    }
}

/// Python's `int(x)` on a JSON value, None where it raises TypeError/ValueError.
pub(crate) fn py_int(v: &Value) -> Option<i64> {
    match v {
        Value::Bool(b) => Some(i64::from(*b)),
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f.trunc() as i64)),
        Value::String(s) => {
            let t = s.trim();
            let (neg, digits) = match t.as_bytes().first() {
                Some(b'-') => (true, &t[1..]),
                Some(b'+') => (false, &t[1..]),
                _ => (false, t),
            };
            // digits, with single underscores between them (PEP 515)
            if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
                return None;
            }
            if !digits.chars().all(|c| c.is_ascii_digit() || c == '_') {
                return None;
            }
            let n: i64 = digits.replace('_', "").parse().ok()?;
            Some(if neg { -n } else { n })
        }
        _ => None,
    }
}

/// `str(data.get(k) or '')[:limit]` for the JSON scalars a client sends.
pub(crate) fn py_str_field(data: &serde_json::Map<String, Value>, key: &str, limit: usize) -> String {
    let s = match data.get(key) {
        None | Some(Value::Null) => String::new(),
        Some(v) if !keys::truthy(v) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Number(n)) => match n.as_i64() {
            Some(i) => i.to_string(),
            None => n.as_f64().map(py_float).unwrap_or_default(),
        },
        Some(other) => conversations::py_dumps(other),
    };
    s.chars().take(limit).collect()
}

/// Python's `repr(float)` for the usual finite values.
fn py_float(f: f64) -> String {
    if f.fract() == 0.0 && f.abs() < 1e16 {
        format!("{f:.1}")
    } else {
        format!("{f}")
    }
}

/// POST /support/feedback - thumbs up/down on a Support reply
pub async fn support_feedback(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    let vote = data.get("vote").and_then(py_int);
    let Some(vote) = vote.filter(|v| *v == 1 || *v == -1) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "Vote invalide."}))).into_response();
    };
    let now = keys::iso(&chrono::Local::now().naive_local());
    let inserted = sqlx::query(
        "INSERT INTO support_feedback (username, vote, comment, question, answer, model, created_at) VALUES (?,?,?,?,?,?,?)",
    )
    .bind(&session.username)
    .bind(vote)
    .bind(py_str_field(&data, "comment", 1000))
    .bind(py_str_field(&data, "question", 2000))
    .bind(py_str_field(&data, "answer", 4000))
    .bind(py_str_field(&data, "model", 80))
    .bind(now)
    .execute(state.db.pool())
    .await;
    if inserted.is_err() {
        return db_down_500();
    }
    sessions::log_audit(state.db.pool(), &session.username, "support.feedback", if vote > 0 { "utile" } else { "pas utile" }).await;
    Json(json!({"ok": true})).into_response()
}

/// `_model_sees`: does this catalog entry read images, from ITS arguments.
pub(crate) fn model_sees(args: &str, engine: &str) -> bool {
    let toks: Vec<&str> = args.split_whitespace().collect();
    match engine {
        "llamacpp" => toks.contains(&"--mmproj"),
        "exllamav3" => toks.windows(2).any(|w| w[0] == "--vision" && w[1] == "true"),
        _ => toks.iter().any(|t| t.starts_with("--limit-mm-per-prompt")),
    }
}

/// GET /api/playground/data - served models, their windows, vision, and whether the viewer has a key
pub async fn api_playground_data(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let rows: Vec<(Option<String>, Option<String>, Option<String>)> =
        match sqlx::query_as("SELECT name, vllm_args, engine FROM model_configs").fetch_all(state.db.pool()).await {
            Ok(r) => r,
            Err(_) => return db_down_500(),
        };
    let running = models::running_models().await;
    let auto = std::env::var("AUTO_MODEL_NAME").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "auto-model".to_string());
    let mut limits = serde_json::Map::new();
    let mut vision = serde_json::Map::new();
    for (name, args, engine) in &rows {
        let name = name.clone().unwrap_or_default();
        let args = args.as_deref().unwrap_or("");
        let engine = engine.as_deref().filter(|e| !e.is_empty()).unwrap_or("vllm");
        if let Some(ctx) = health::effective_ctx(args, engine).filter(|c| *c != 0) {
            limits.insert(name.clone(), json!(ctx));
        }
        vision.insert(name, json!(model_sees(args, engine)));
    }
    if let Some(first) = running.first() {
        if let Some(ctx) = limits.get(first).filter(|v| keys::truthy(v)).cloned() {
            limits.insert(auto.clone(), ctx);
        }
        let sees = vision.get(first).cloned().unwrap_or(json!(false));
        vision.insert(auto, sees);
    }
    let has_key = sqlx::query_scalar::<_, i64>("SELECT 1 FROM api_keys WHERE username = ? LIMIT 1")
        .bind(&session.username)
        .fetch_optional(state.db.pool())
        .await
        .ok()
        .flatten()
        .is_some();
    Json(json!({
        "has_key": has_key,
        "model_limits": limits,
        "model_vision": vision,
        "running_models": running,
    }))
    .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_int_conversion() {
        assert_eq!(py_int(&json!(1)), Some(1));
        assert_eq!(py_int(&json!(-1.7)), Some(-1));
        assert_eq!(py_int(&json!(true)), Some(1));
        assert_eq!(py_int(&json!(" -1 ")), Some(-1));
        assert_eq!(py_int(&json!("1_0")), Some(10));
        assert_eq!(py_int(&json!("1.0")), None);
        assert_eq!(py_int(&json!(null)), None);
        assert_eq!(py_int(&json!([1])), None);
    }

    #[test]
    fn vision_is_read_from_the_arguments() {
        assert!(model_sees("-m x --mmproj p.gguf", "llamacpp"));
        assert!(!model_sees("-m x", "llamacpp"));
        assert!(model_sees("--vision true", "exllamav3"));
        assert!(!model_sees("--vision false", "exllamav3"));
        assert!(model_sees("--limit-mm-per-prompt '{\"image\":1}'", "vllm"));
    }

    #[test]
    fn string_fields_follow_python_str() {
        let d = json!({"a": 5, "b": true, "c": 0, "d": "héllo", "e": 2.0}).as_object().unwrap().clone();
        assert_eq!(py_str_field(&d, "a", 10), "5");
        assert_eq!(py_str_field(&d, "b", 10), "True");
        assert_eq!(py_str_field(&d, "c", 10), "");
        assert_eq!(py_str_field(&d, "d", 2), "hé");
        assert_eq!(py_str_field(&d, "e", 10), "2.0");
    }
}
