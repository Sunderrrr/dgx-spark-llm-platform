//! OCR extraction (ocr_routes.py): an image in, the OCR container's answer
//! relayed as SSE while it streams, the job kept in the history at the end.
#![allow(unused_imports)]
use super::*;
use axum::extract::Request;

const OCR_HISTORY_LIMIT: i64 = 20;
/// chandra-ocr's layout prompt, verbatim from dgx-portal/ocr_routes.py.
const CHANDRA_PROMPT: &str = include_str!("chandra_prompt.txt");

/// Python's `json.dumps` with its defaults (ASCII-escaped, `, ` and `: `).
pub(crate) fn py_dumps_ascii(v: &Value) -> String {
    let mut out = String::new();
    for c in conversations::py_dumps(v).chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut buf = [0u16; 2];
            for u in c.encode_utf16(&mut buf) {
                out.push_str(&format!("\\u{u:04x}"));
            }
        }
    }
    out
}

/// `_sse_msg`: one content message, then the end of the stream.
pub(crate) fn sse_msg(text: &str) -> String {
    format!("data: {}\n\ndata: [DONE]\n\n", py_dumps_ascii(&json!({"choices": [{"delta": {"content": text}}]})))
}

fn sse(status: StatusCode, body: String) -> Response {
    (status, [(axum::http::header::CONTENT_TYPE, "text/event-stream; charset=utf-8")], body).into_response()
}

fn image_ext(mime: &str) -> &'static str {
    match mime {
        "image/jpeg" => "jpg",
        "image/webp" => "webp",
        _ => "png",
    }
}

/// POST /api/ocr/extract - read an image, the text streamed as it comes
pub async fn api_ocr_extract(State(state): State<AppState>, req: Request) -> Response {
    let headers = req.headers().clone();
    let (form, files) = multipart_parts(&state, req).await;
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::csrf_refused(),
        Ok(true) => {}
    }
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    if !session.is_admin && state.db.maintenance_active().await {
        return sse(StatusCode::OK, sse_msg("Maintenance in progress — model access is temporarily suspended, please try again later."));
    }
    match guards::rate_limited(state.db.pool(), &session.username, "rl-media", guards::MEDIA_RATE_MAX, guards::MEDIA_RATE_WINDOW_S).await {
        Ok(0) => {}
        Ok(wait) => return sse(StatusCode::TOO_MANY_REQUESTS, sse_msg(&format!("Trop de requêtes. Réessaie dans {wait} s."))),
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
    let (data, mime) = match files.get("image").filter(|(n, _, _)| !n.is_empty()) {
        None => return sse(StatusCode::BAD_REQUEST, sse_msg("Aucune image fournie.")),
        Some((name, mime, data)) => match reference_image(name, mime, data) {
            Err(e) => return sse(StatusCode::BAD_REQUEST, sse_msg(&e)),
            Ok(()) => (data.clone(), mime.clone()),
        },
    };
    let instruction: String = form.get("instruction").map_or("document parsing.", String::as_str).trim_matches(memory::py_is_space).chars().take(500).collect();
    let started = std::time::Instant::now();
    let mut rnd = [0u8; 16];
    if getrandom::getrandom(&mut rnd).is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    // Saved BEFORE streaming: the history redisplays the analysed image.
    let image_name = format!("{}.{}", hex::encode(rnd), image_ext(&mime));
    if crate::media_store::save(&state.db, "ocr_images", &image_name, data.clone(), &mime).await.is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<axum::body::Bytes, std::convert::Infallible>>(64);
    let username = session.username.clone();
    tokio::spawn(async move {
        let text = relay(&tx, &data, &mime, &instruction).await;
        persist(&state, &username, &image_name, &text, started).await;
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

type Tx = tokio::sync::mpsc::Sender<Result<axum::body::Bytes, std::convert::Infallible>>;

/// `ocr_extract_stream`: the upstream SSE relayed line by line; the text gathered.
/// A departed client stops the relay; what was gathered is still kept.
async fn relay(tx: &Tx, image: &[u8], mime: &str, instruction: &str) -> String {
    use base64::Engine;
    let send = |s: String| async move { tx.send(Ok(axum::body::Bytes::from(s))).await.is_ok() };
    // A comment BEFORE any work: the headers leave at once (the proxy's connect delay is 15 s).
    if !send(": ouverture\n\n".into()).await {
        return String::new();
    }
    let model = sidecars::ocr_model().await.unwrap_or_else(|| "baidu/Unlimited-OCR".to_string());
    let chandra = model.to_lowercase().contains("chandra");
    let prompt = if chandra { CHANDRA_PROMPT.to_string() } else { format!("<image>{instruction}") };
    let b64 = base64::engine::general_purpose::STANDARD.encode(image);
    let mut body = json!({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": format!("data:{mime};base64,{b64}")}},
        ]}],
        "max_tokens": if chandra { 8192 } else { 4096 },
        "temperature": 0.0,
        "stream": true,
    });
    if !chandra {
        body["extra_body"] = json!({"skip_special_tokens": false, "vllm_xargs": {"ngram_size": 35, "window_size": 128}});
    }
    let mut full = String::new();
    let client = reqwest::Client::builder().connect_timeout(std::time::Duration::from_secs(10)).read_timeout(std::time::Duration::from_secs(180)).build();
    let resp = match client {
        Ok(c) => c.post(format!("{}/chat/completions", sidecars::ocr_url())).json(&body).send().await,
        Err(_) => {
            send(sse_msg("⚠ OCR stream interrupted.")).await;
            return full;
        }
    };
    let mut resp = match resp {
        Ok(r) if r.status().is_success() => r,
        Ok(_) => {
            send(sse_msg("OCR service unreachable.")).await;
            return full;
        }
        Err(_) => {
            send(sse_msg("⚠ OCR stream interrupted.")).await;
            return full;
        }
    };
    let mut pending: Vec<u8> = Vec::new();
    loop {
        match resp.chunk().await {
            Ok(Some(chunk)) => {
                pending.extend_from_slice(&chunk);
                while let Some(pos) = pending.iter().position(|b| *b == b'\n') {
                    let raw: Vec<u8> = pending.drain(..=pos).collect();
                    if !forward_line(&send, &raw[..raw.len() - 1], &mut full).await {
                        return full;
                    }
                }
            }
            Ok(None) => {
                if !pending.is_empty() {
                    let raw = std::mem::take(&mut pending);
                    forward_line(&send, &raw, &mut full).await;
                }
                return full;
            }
            Err(_) => {
                send(sse_msg("⚠ OCR stream interrupted.")).await;
                return full;
            }
        }
    }
}

/// One upstream line → `line\n\n` downstream, its content delta gathered.
/// False once the client is gone.
async fn forward_line<F, Fut>(send: &F, raw: &[u8], full: &mut String) -> bool
where
    F: Fn(String) -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    let line = String::from_utf8_lossy(raw);
    let line = line.trim_end_matches('\r');
    if line.is_empty() {
        return true;
    }
    if let Some(payload) = line.strip_prefix("data: ") {
        let payload = payload.trim();
        if !payload.is_empty() && payload != "[DONE]" {
            if let Ok(v) = serde_json::from_str::<Value>(payload) {
                if let Some(c) = v.pointer("/choices/0/delta/content").and_then(Value::as_str) {
                    full.push_str(c);
                }
            }
        }
    }
    send(format!("{line}\n\n")).await
}

/// `_persist`: the job kept (history trimmed with its images), or the image dropped.
async fn persist(state: &AppState, username: &str, image: &str, text: &str, started: std::time::Instant) {
    if text.is_empty() {
        crate::media_store::delete(&state.db, "ocr_images", image).await;
        return;
    }
    let pool = state.db.pool();
    let _ = sqlx::query("INSERT INTO ocr_jobs (username, text, image_path, created_at, duration_ms) VALUES (?,?,?,?,?)")
        .bind(username)
        .bind(text)
        .bind(image)
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .bind(started.elapsed().as_millis() as i64)
        .execute(pool)
        .await;
    let stale: Vec<(i64, Option<String>)> = sqlx::query_as(
        "SELECT id, image_path FROM ocr_jobs WHERE username=? AND id NOT IN (SELECT id FROM ocr_jobs WHERE username=? ORDER BY id DESC LIMIT ?)",
    )
    .bind(username)
    .bind(username)
    .bind(OCR_HISTORY_LIMIT)
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    for (id, path) in stale {
        if let Some(p) = path.filter(|p| !p.is_empty()) {
            crate::media_store::delete(&state.db, "ocr_images", &p).await;
        }
        let _ = sqlx::query("DELETE FROM ocr_jobs WHERE id=?").bind(id).execute(pool).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sse_messages_like_python() {
        assert_eq!(sse_msg("é"), "data: {\"choices\": [{\"delta\": {\"content\": \"\\u00e9\"}}]}\n\ndata: [DONE]\n\n");
        assert_eq!(py_dumps_ascii(&json!("😀")), "\"\\ud83d\\ude00\"");
        assert!(CHANDRA_PROMPT.starts_with("OCR this image to HTML") && CHANDRA_PROMPT.ends_with("natural."));
    }
}
