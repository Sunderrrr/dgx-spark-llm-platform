//! `POST /api/v1/media/{kind}/{rest…}` — the media API behind LiteLLM.
//!
//! Why a proxy and not a real API: the four media services (image, dictation,
//! voice, video) each expose their own HTTP shape, and only THIS process can
//! reach them — they live on `image_net` / `asr_net` / `voice_net` and ComfyUI
//! behind the loopback relay, while `litellm` sits alone on the default
//! network. So the gateway forwards, and the shapes stay the sidecars' own
//! (documented in docs/api.md, with a curl example per capability).
//!
//! Authentication is NOT done here: LiteLLM owns it. A pass-through endpoint
//! with `auth: true` validates the caller's API key, applies budgets and logs
//! the request (`cost_per_request`) BEFORE calling this route; the shared token
//! below only proves "this call comes from the gateway", and never leaves the
//! docker network. Fail-closed: with no token configured, the routes refuse.

use axum::{
    body::Bytes,
    extract::{Path, State},
    http::{header, HeaderMap, Method, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

use crate::handlers::AppState;
use crate::sidecars;

/// The internal token, compared in constant time by `token_ok`.
fn gateway_token() -> String {
    std::env::var("RUNNER_TOKEN").unwrap_or_default()
}

/// Constant-time comparison: a length difference is answered without early
/// exit, so a wrong token cannot be guessed byte by byte from timing.
fn token_ok(given: &str, expected: &str) -> bool {
    if expected.is_empty() {
        return false;
    }
    let (a, b) = (given.as_bytes(), expected.as_bytes());
    let mut diff = (a.len() ^ b.len()) as u8;
    for i in 0..a.len().max(b.len()) {
        let x = *a.get(i).unwrap_or(&0);
        let y = *b.get(i).unwrap_or(&0);
        diff |= x ^ y;
    }
    diff == 0
}

/// The upstream base URL of a media family, or None for an unknown one.
///
/// Only these four are reachable, and only with the paths their server
/// documents: this is not an open proxy.
fn upstream(kind: &str) -> Option<String> {
    match kind {
        "image" => Some(sidecars::image_url()),
        "asr" => Some(crate::media::sidecar_url("ASR_URL", "http://asr:8006")),
        "tts" => Some(crate::media::sidecar_url("VOICE_URL", "http://voice:8004")),
        "video" => Some(crate::media::sidecar_url("COMFYUI_URL", "http://host.docker.internal:8188")),
        _ => None,
    }
}

/// The paths each family accepts, so a stray path cannot reach an admin route
/// of the same server.
fn path_allowed(kind: &str, rest: &str) -> bool {
    match kind {
        "image" => matches!(rest, "generate" | "health" | "model-info"),
        "asr" => matches!(rest, "transcribe" | "api/model-info"),
        "tts" => matches!(rest, "tts" | "clone" | "upload_reference" | "api/model-info"),
        "video" => {
            rest == "prompt" || rest.starts_with("history/") || rest.starts_with("view")
        }
        _ => false,
    }
}

/// The gateway route. Every refusal names the reason: a silent 404 on a media
/// call looks like "the feature is gone", which is exactly what it is not.
pub async fn api_media(
    State(_state): State<AppState>,
    Path((kind, rest)): Path<(String, String)>,
    method: Method,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let given = headers.get("x-media-token").and_then(|v| v.to_str().ok()).unwrap_or("");
    if !token_ok(given, &gateway_token()) {
        return (StatusCode::UNAUTHORIZED, Json(json!({
            "error": "Jeton interne absent ou invalide : cette route n'est servie qu'à la passerelle (LiteLLM)."
        }))).into_response();
    }
    let Some(base) = upstream(&kind) else {
        return (StatusCode::NOT_FOUND, Json(json!({
            "error": format!("Famille « {kind} » inconnue (image, asr, tts, video).")
        }))).into_response();
    };
    if !path_allowed(&kind, &rest) {
        return (StatusCode::NOT_FOUND, Json(json!({
            "error": format!("Chemin « {kind}/{rest} » non exposé par l'API média.")
        }))).into_response();
    }
    let url = format!("{}/{}", base.trim_end_matches('/'), rest);
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/json")
        .to_string();
    let mut req = sidecars::http()
        .request(method.clone(), &url)
        // Media generation is slow by nature (a video is minutes); the caller's
        // own HTTP client decides when to give up.
        .timeout(std::time::Duration::from_secs(420))
        .header(header::CONTENT_TYPE, content_type);
    if !body.is_empty() {
        req = req.body(body);
    }
    let sent = match req.send().await {
        Ok(r) => r,
        Err(e) => {
            // The internal address stays in the log, never in the answer.
            eprintln!("[media] {kind}/{rest} upstream failed: {e}");
            return (StatusCode::BAD_GATEWAY, Json(json!({
                "error": "Service média injoignable : il est probablement arrêté (Admin ▸ Modèles)."
            }))).into_response();
        }
    };
    let status = StatusCode::from_u16(sent.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let out_type = sent
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .to_string();
    let bytes = sent.bytes().await.unwrap_or_default();
    (status, [(header::CONTENT_TYPE, out_type)], bytes).into_response()
}


// ── The OpenAI-shaped routes ────────────────────────────────────────────────
//
// LiteLLM's pass-through endpoints are gated by `allowed_passthrough_routes`,
// an ENTERPRISE feature (measured 2026-10-28: 403 then "only available for
// LiteLLM Enterprise users"). The open-source way to expose a non-OpenAI
// service is a registered MODEL with a mode, which goes through the normal key
// RBAC: `image_generation`, `audio_transcription`, `audio_speech`,
// `video_generation`. These three routes are that shape on this side; the
// gateway reaches them with `api_key = RUNNER_TOKEN`, and the caller's own key
// is checked and billed by LiteLLM before the call happens.

/// `Authorization: Bearer <RUNNER_TOKEN>` — what LiteLLM sends as the model's
/// api_key. Same constant-time comparison as the media token above.
fn bearer_ok(headers: &HeaderMap) -> bool {
    let given = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("");
    token_ok(given, &gateway_token())
}

/// `POST /v1/images/generations` — OpenAI's shape, one prompt, n variants.
pub async fn v1_images(State(_state): State<AppState>, headers: HeaderMap, Json(body): Json<serde_json::Value>) -> Response {
    if !bearer_ok(&headers) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": {"message": "unauthorized", "type": "auth_error"}}))).into_response();
    }
    let prompt = body.get("prompt").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    if prompt.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": {"message": "prompt is required", "type": "invalid_request_error"}}))).into_response();
    }
    // "1024x1024" → width/height; anything else keeps the model's default.
    let (w, h) = body
        .get("size")
        .and_then(|v| v.as_str())
        .and_then(|s| s.split_once('x'))
        .and_then(|(a, b)| Some((a.parse::<u32>().ok()?, b.parse::<u32>().ok()?)))
        .unwrap_or((1024, 1024));
    let form = [
        ("prompt", prompt),
        ("width", w.to_string()),
        ("height", h.to_string()),
        ("format", "png".to_string()),
    ];
    let sent = sidecars::http()
        .post(format!("{}/generate", sidecars::image_url()))
        .form(&form)
        .timeout(std::time::Duration::from_secs(420))
        .send()
        .await;
    let sent = match sent {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[media] image upstream failed: {e}");
            return (StatusCode::BAD_GATEWAY, Json(json!({"error": {"message": "image service unreachable (stopped?)", "type": "upstream_error"}}))).into_response();
        }
    };
    let status = sent.status();
    let bytes = sent.bytes().await.unwrap_or_default();
    if !status.is_success() {
        let detail = String::from_utf8_lossy(&bytes).chars().take(300).collect::<String>();
        return (StatusCode::BAD_GATEWAY, Json(json!({"error": {"message": detail, "type": "upstream_error"}}))).into_response();
    }
    use base64::Engine as _;
    let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
    Json(json!({
        "created": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0),
        "data": [{"b64_json": b64}],
    }))
    .into_response()
}

/// `POST /v1/audio/transcriptions` — multipart in, OpenAI JSON out.
pub async fn v1_transcriptions(State(_state): State<AppState>, headers: HeaderMap, request: axum::extract::Request) -> Response {
    if !bearer_ok(&headers) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": {"message": "unauthorized", "type": "auth_error"}}))).into_response();
    }
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("multipart/form-data")
        .to_string();
    let body = match axum::body::to_bytes(request.into_body(), 64 * 1024 * 1024).await {
        Ok(b) => b,
        Err(_) => return (StatusCode::PAYLOAD_TOO_LARGE, Json(json!({"error": {"message": "file too large", "type": "invalid_request_error"}}))).into_response(),
    };
    // OpenAI (and therefore LiteLLM) names the part `file`; the sidecar wants
    // `audio`. Renaming the FIELD of the raw multipart body is a byte rewrite,
    // not a re-encode: the boundary and every other part stay untouched.
    let body = rename_multipart_field(body, b"file", b"audio");
    let sent = sidecars::http()
        .post(format!("{}/transcribe", crate::media::sidecar_url("ASR_URL", "http://asr:8006")))
        .header(header::CONTENT_TYPE, content_type)
        .body(body)
        .timeout(std::time::Duration::from_secs(420))
        .send()
        .await;
    let sent = match sent {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[media] asr upstream failed: {e}");
            return (StatusCode::BAD_GATEWAY, Json(json!({"error": {"message": "transcription service unreachable (stopped?)", "type": "upstream_error"}}))).into_response();
        }
    };
    let status = sent.status();
    let bytes = sent.bytes().await.unwrap_or_default();
    if !status.is_success() {
        let detail = String::from_utf8_lossy(&bytes).chars().take(300).collect::<String>();
        return (StatusCode::BAD_GATEWAY, Json(json!({"error": {"message": detail, "type": "upstream_error"}}))).into_response();
    }
    let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_else(|_| json!({}));
    let text = parsed.get("text").cloned().unwrap_or(json!(""));
    Json(json!({"text": text}))
        .into_response()
}

/// `POST /v1/audio/speech` — OpenAI's shape to the Chatterbox/Kyutai service.
pub async fn v1_speech(State(_state): State<AppState>, headers: HeaderMap, Json(body): Json<serde_json::Value>) -> Response {
    if !bearer_ok(&headers) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": {"message": "unauthorized", "type": "auth_error"}}))).into_response();
    }
    let input = body.get("input").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    if input.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": {"message": "input is required", "type": "invalid_request_error"}}))).into_response();
    }
    let format = body.get("response_format").and_then(|v| v.as_str()).unwrap_or("mp3");
    // The voice service speaks `{text, voice_mode, language, output_format}`;
    // a caller may pass any of them through the OpenAI extras.
    let mut payload = json!({"text": input, "output_format": format});
    for key in ["voice_mode", "language", "reference_audio_filename"] {
        if let Some(v) = body.get(key) {
            payload[key] = v.clone();
        }
    }
    let sent = sidecars::http()
        .post(format!("{}/tts", crate::media::sidecar_url("VOICE_URL", "http://voice:8004")))
        .json(&payload)
        .timeout(std::time::Duration::from_secs(420))
        .send()
        .await;
    let sent = match sent {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[media] tts upstream failed: {e}");
            return (StatusCode::BAD_GATEWAY, Json(json!({"error": {"message": "voice service unreachable (stopped?)", "type": "upstream_error"}}))).into_response();
        }
    };
    let status = sent.status();
    let out_type = sent.headers().get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("audio/mpeg").to_string();
    let bytes = sent.bytes().await.unwrap_or_default();
    if !status.is_success() {
        let detail = String::from_utf8_lossy(&bytes).chars().take(300).collect::<String>();
        return (StatusCode::BAD_GATEWAY, Json(json!({"error": {"message": detail, "type": "upstream_error"}}))).into_response();
    }
    (StatusCode::OK, [(header::CONTENT_TYPE, out_type)], bytes).into_response()
}


/// Rewrite `name="<from>"` into `name="<to>"` inside a multipart body.
///
/// Only the quoted field name is touched: a filename that happens to contain
/// the same bytes is preceded by `filename="`, not `name="`, so it is left
/// alone. The two names must be the same length for the body to stay valid —
/// `file` and `audio` are not, which is why the Content-Length is recomputed by
/// the HTTP client from the returned buffer (never copied from the caller).
fn rename_multipart_field(body: Bytes, from: &[u8], to: &[u8]) -> Bytes {
    let mut needle = Vec::with_capacity(from.len() + 8);
    needle.extend_from_slice(b"name=\"");
    needle.extend_from_slice(from);
    needle.push(b'"');
    let mut replacement = Vec::with_capacity(to.len() + 8);
    replacement.extend_from_slice(b"name=\"");
    replacement.extend_from_slice(to);
    replacement.push(b'"');
    let mut out = Vec::with_capacity(body.len());
    let mut i = 0usize;
    while i < body.len() {
        if body[i..].starts_with(&needle) {
            out.extend_from_slice(&replacement);
            i += needle.len();
        } else {
            out.push(body[i]);
            i += 1;
        }
    }
    Bytes::from(out)
}


// ── Video, OpenAI's shape over the ComfyUI workflow ──────────────────────────
//
// LiteLLM's `video_generation` mode speaks to an `openai/...` model through
// `/videos`, `/videos/{id}` and `/videos/{id}/content` (see its
// `llms/openai/videos/transformation.py`, api_base honoured). The portal already
// owns the ComfyUI workflow, so these three routes are a translation, not a new
// pipeline: `comfyui_generate` submits, `comfyui_status` polls, `/view` streams.

fn video_status_word(raw: &str) -> &'static str {
    match raw {
        "done" => "completed",
        "running" => "in_progress",
        "error" => "failed",
        _ => "queued",
    }
}

/// `POST /v1/videos` — submit a text-to-video job, return the OpenAI object.
pub async fn v1_videos(State(_state): State<AppState>, headers: HeaderMap, request: axum::extract::Request) -> Response {
    if !bearer_ok(&headers) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": {"message": "unauthorized", "type": "auth_error"}}))).into_response();
    }
    // LiteLLM's `/v1/videos` POSTs a FORM (it can also upload a reference file),
    // while an OpenAI SDK posts JSON. Accept both rather than answer 415 to the
    // gateway that is supposed to be the only caller.
    let is_form = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("multipart/form-data") || v.starts_with("application/x-www-form-urlencoded"));
    let raw = match axum::body::to_bytes(request.into_body(), 16 * 1024 * 1024).await {
        Ok(b) => b,
        Err(_) => return (StatusCode::PAYLOAD_TOO_LARGE, Json(json!({"error": {"message": "body too large", "type": "invalid_request_error"}}))).into_response(),
    };
    let body: serde_json::Value = if is_form {
        let text = String::from_utf8_lossy(&raw).to_string();
        json!({
            "prompt": form_value(&text, "prompt").unwrap_or_default(),
            "seconds": form_value(&text, "seconds").and_then(|v| v.parse::<f64>().ok()),
            "model": form_value(&text, "model").unwrap_or_default(),
        })
    } else {
        serde_json::from_slice(&raw).unwrap_or_else(|_| json!({}))
    };
    let prompt = body.get("prompt").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    if prompt.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": {"message": "prompt is required", "type": "invalid_request_error"}}))).into_response();
    }
    // `seconds` is OpenAI's field; the workflow clamps it to 2..15 anyway.
    let seconds = body.get("seconds").and_then(|v| v.as_f64()).unwrap_or(5.0);
    let Some(id) = crate::handlers::video_routes::comfyui_generate(None, &prompt, seconds).await else {
        return (StatusCode::BAD_GATEWAY, Json(json!({"error": {
            "message": "video service unreachable or refusing the workflow (ComfyUI stopped?)",
            "type": "upstream_error"
        }}))).into_response();
    };
    let model = body.get("model").and_then(|v| v.as_str()).unwrap_or("video").to_string();
    Json(json!({
        "id": id,
        "object": "video",
        "status": "queued",
        "created_at": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0),
        "model": model,
        // LiteLLM's VideoObject types `seconds` as a STRING ("3"), not a number:
        // a float here fails its validation with « Input should be a valid string ».
        "seconds": seconds.to_string(),
    }))
    .into_response()
}

/// `GET /v1/videos/{id}` — poll one job.
pub async fn v1_video_status(headers: HeaderMap, Path(id): Path<String>) -> Response {
    if !bearer_ok(&headers) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": {"message": "unauthorized", "type": "auth_error"}}))).into_response();
    }
    if id.contains('/') || id.contains("..") {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": {"message": "invalid id", "type": "invalid_request_error"}}))).into_response();
    }
    let state = crate::handlers::video_routes::comfyui_status(&id).await;
    let word = state.get("status").and_then(|v| v.as_str()).unwrap_or("");
    let mut out = json!({
        "id": id,
        "object": "video",
        "model": "video",
        "status": video_status_word(word),
    });
    if let Some(p) = state.get("video_path").filter(|v| !v.is_null()) {
        out["video_path"] = p.clone();
    }
    Json(out).into_response()
}

/// `GET /v1/videos/{id}/content` — the bytes, once the job is done.
pub async fn v1_video_content(headers: HeaderMap, Path(id): Path<String>) -> Response {
    if !bearer_ok(&headers) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": {"message": "unauthorized", "type": "auth_error"}}))).into_response();
    }
    if id.contains('/') || id.contains("..") {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": {"message": "invalid id", "type": "invalid_request_error"}}))).into_response();
    }
    let state = crate::handlers::video_routes::comfyui_status(&id).await;
    if video_status_word(state.get("status").and_then(|v| v.as_str()).unwrap_or("")) != "completed" {
        return (StatusCode::CONFLICT, Json(json!({"error": {"message": "the video is not ready", "type": "invalid_request_error"}}))).into_response();
    }
    let name = state.get("video_path").and_then(|v| v.as_str()).unwrap_or("");
    let sub = state.get("video_subfolder").and_then(|v| v.as_str()).unwrap_or("");
    let kind = state.get("video_type").and_then(|v| v.as_str()).unwrap_or("output");
    if name.is_empty() {
        return (StatusCode::BAD_GATEWAY, Json(json!({"error": {"message": "no file for this job", "type": "upstream_error"}}))).into_response();
    }
    // ComfyUI serves its own outputs: stream them rather than reading a mount
    // the API must not assume (`/comfyui-output` is the portal's business).
    let url = format!(
        "{}/view?filename={}&subfolder={}&type={}",
        crate::media::sidecar_url("COMFYUI_URL", "http://host.docker.internal:8188").trim_end_matches('/'),
        urlencode(name),
        urlencode(sub),
        urlencode(kind)
    );
    let sent = sidecars::http().get(&url).timeout(std::time::Duration::from_secs(120)).send().await;
    match sent {
        Ok(r) if r.status().is_success() => {
            let bytes = r.bytes().await.unwrap_or_default();
            (StatusCode::OK, [(header::CONTENT_TYPE, "video/mp4")], bytes).into_response()
        }
        _ => (StatusCode::BAD_GATEWAY, Json(json!({"error": {"message": "cannot read the video from ComfyUI", "type": "upstream_error"}}))).into_response(),
    }
}

/// A `name="…"` field of a form body (multipart or urlencoded), if present.
///
/// Deliberately small: the only caller is LiteLLM's video form, and the field
/// names are fixed. A full multipart parser would be more code than the two
/// fields it has to read.
fn form_value(body: &str, name: &str) -> Option<String> {
    let marker = format!("name=\"{name}\"");
    if let Some(at) = body.find(&marker) {
        // multipart: two CRLF after the part headers, then the value
        let rest = &body[at + marker.len()..];
        let start = rest.find("\r\n\r\n").map(|i| i + 4)?;
        let value = &rest[start..];
        let end = value.find("\r\n--").unwrap_or(value.len());
        return Some(value[..end].trim().to_string());
    }
    // urlencoded: name=value&…
    let key = format!("{name}=");
    let at = body.find(&key)?;
    let rest = &body[at + key.len()..];
    let end = rest.find('&').unwrap_or(rest.len());
    Some(rest[..end].replace('+', " ").trim().to_string())
}

/// Percent-encoding for a query value: the file names ComfyUI returns contain
/// `/` and spaces, and an unencoded one would break the URL.
fn urlencode(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for b in raw.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_gateway_token_is_compared_without_shortcuts() {
        assert!(token_ok("secret", "secret"));
        assert!(!token_ok("secre", "secret"));
        assert!(!token_ok("secrets", "secret"));
        assert!(!token_ok("Secret", "secret"));
        // An unset token refuses everything: fail-closed, not fail-open.
        assert!(!token_ok("", ""));
        assert!(!token_ok("anything", ""));
    }

    #[test]
    fn comfyui_states_map_to_openai_words() {
        assert_eq!(video_status_word("done"), "completed");
        assert_eq!(video_status_word("running"), "in_progress");
        assert_eq!(video_status_word("error"), "failed");
        // Anything unknown is still pending: never say "completed" on a guess.
        assert_eq!(video_status_word(""), "queued");
        assert_eq!(video_status_word("whatever"), "queued");
    }

    #[test]
    fn the_form_reader_finds_the_two_fields_that_matter() {
        let mp = "--b\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nun chat\r\n--b--\r\n";
        assert_eq!(form_value(mp, "prompt").as_deref(), Some("un chat"));
        assert_eq!(form_value(mp, "seconds"), None);
        let urlenc = "prompt=une+plage&seconds=3&model=video";
        assert_eq!(form_value(urlenc, "prompt").as_deref(), Some("une plage"));
        assert_eq!(form_value(urlenc, "seconds").as_deref(), Some("3"));
    }

    #[test]
    fn a_video_file_name_survives_the_query_string() {
        assert_eq!(urlencode("a b.mp4"), "a%20b.mp4");
        assert_eq!(urlencode("sub/dir/x.mp4"), "sub%2Fdir%2Fx.mp4");
        assert_eq!(urlencode("ok-_.~"), "ok-_.~");
    }

    #[test]
    fn the_multipart_field_is_renamed_and_nothing_else_is_touched() {
        let body = Bytes::from_static(
            b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a name=file.wav\"\r\n\r\nDATA\r\n--B--\r\n",
        );
        let out = rename_multipart_field(body, b"file", b"audio");
        let text = String::from_utf8(out.to_vec()).unwrap();
        assert!(text.contains("name=\"audio\""), "{text}");
        // The FILENAME still says `file`: only the field name is rewritten.
        assert!(text.contains("filename=\"a name=file.wav\""), "{text}");
        assert_eq!(text.matches("audio").count(), 1, "{text}");
    }

    #[test]
    fn only_the_documented_paths_are_reachable() {
        assert!(path_allowed("image", "generate"));
        assert!(path_allowed("asr", "transcribe"));
        assert!(path_allowed("video", "history/abc"));
        assert!(!path_allowed("image", "admin/stop"));
        assert!(!path_allowed("tts", "../image/generate"));
        assert!(!path_allowed("nope", "generate"));
        assert!(upstream("nope").is_none());
    }
}