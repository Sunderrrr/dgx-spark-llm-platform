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