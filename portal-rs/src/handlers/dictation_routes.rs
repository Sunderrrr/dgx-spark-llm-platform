//! Dictation and the playground's voice mode (asr_routes.py,
//! voice_chat_routes.py): mic audio → text, the voice container started on
//! first use, and one sentence → raw PCM streamed. The ASR sidecar's idle
//! reaper is a separate background job (asr_reaper.rs); these routes write the shared on-demand state.
#![allow(unused_imports)]
use super::*;
use crate::asr_ondemand;
use axum::extract::{FromRequest, Request};

const MAX_VOICE_UPLOAD_BYTES: usize = 15 * 1024 * 1024;
const ASR_RATE_MAX: i64 = 75;
const SPEAK_RATE_MAX: i64 = 90;
const SPEAK_MAX_CHARS: usize = 1000;
const VOICE_MEM_NEED_GB: f64 = 15.0;

fn maintenance(state_is_on: bool, session: &auth::SessionData) -> Option<Response> {
    (!session.is_admin && state_is_on)
        .then(|| (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "Mode maintenance en cours — réessaie plus tard."}))).into_response())
}

async fn rate(state: &AppState, user: &str, bucket: &str, max: i64) -> Option<Response> {
    match guards::rate_limited(state.db.pool(), user, bucket, max, 60.0).await {
        Ok(0) => None,
        Ok(wait) => Some((StatusCode::TOO_MANY_REQUESTS, Json(json!({"error": format!("Trop de requêtes. Réessaie dans {wait} s.")}))).into_response()),
        Err(_) => Some(auth::html_status(StatusCode::INTERNAL_SERVER_ERROR)),
    }
}

fn err503(msg: impl Into<String>) -> Response {
    (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": msg.into()}))).into_response()
}

/// POST /api/transcribe - dictation: mic audio → text, the sidecar started on demand
pub async fn api_transcribe(State(state): State<AppState>, req: Request) -> Response {
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
    if let Some(r) = maintenance(state.db.maintenance_active().await, &session) {
        return r;
    }
    if let Some(r) = rate(&state, &session.username, "rl-asr", ASR_RATE_MAX).await {
        return r;
    }
    let Some((_, _, data)) = files.get("audio").filter(|(n, _, _)| !n.is_empty()) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "Aucun audio fourni."}))).into_response();
    };
    if data.len() > MAX_VOICE_UPLOAD_BYTES {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "Enregistrement trop volumineux (15 Mo max)."}))).into_response();
    }
    let language: String = form.get("language").map(|l| l.trim_matches(memory::py_is_space).chars().take(10).collect()).unwrap_or_default();
    asr_ondemand::note_activity();
    let info = media::asr_info().await;
    if !info.get("loaded").is_some_and(keys::truthy) {
        let st = sidecars::proc_status("asr").await;
        if matches!(st.as_str(), "running" | "stopped" | "unknown") {
            let mut retry = false;
            if st != "running" {
                if let Some(reason) = asr_ondemand::memory_refusal() {
                    return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": reason, "starting": false}))).into_response();
                }
                let (ok, detail) = asr_ondemand::autostart(&state.db, &session.username).await;
                if !ok {
                    return err503(format!("Le service de dictée n'a pas pu démarrer{detail}."));
                }
            } else {
                let error_text = info.get("error").filter(|v| keys::truthy(v)).map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string()));
                if let Some(e) = &error_text {
                    if asr_ondemand::memory_refusal().is_some() {
                        return err503(format!("Le modèle de dictée n'a pas pu se charger : {e}"));
                    }
                    // Memory freed since the failed load: the sidecar retries on demand.
                    retry = true;
                }
            }
            if !retry {
                // What the 1 s poll tolerates silently: a 2xx with an empty text.
                return Json(json!({"text": "", "starting": true})).into_response();
            }
        }
    }
    let call = async {
        let _in_flight = asr_ondemand::InFlight::start();
        let part = reqwest::multipart::Part::bytes(data.clone()).file_name("rec.wav").mime_str("audio/wav").ok()?;
        let form = reqwest::multipart::Form::new().part("audio", part).text("language", language.clone());
        let c = reqwest::Client::builder().timeout(std::time::Duration::from_secs(180)).build().ok()?;
        Some(c.post(format!("{}/transcribe", media::sidecar_url("ASR_URL", "http://asr:8006"))).multipart(form).send().await)
    };
    match call.await {
        None => err503("Service de transcription injoignable."),
        Some(Err(e)) if e.is_timeout() => err503("La transcription a mis trop de temps."),
        Some(Err(_)) => err503("Service de transcription injoignable."),
        Some(Ok(r)) if !r.status().is_success() => {
            let status = r.status().as_u16();
            let detail = r.json::<Value>().await.ok().and_then(|b| b.get("detail").and_then(Value::as_str).map(str::to_string)).unwrap_or_default();
            let pick = |d: &str| if detail.is_empty() { d.to_string() } else { detail.clone() };
            if (400..500).contains(&status) {
                let code = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST);
                (code, Json(json!({"error": pick("Enregistrement refusé.")}))).into_response()
            } else if status == 503 {
                err503(pick("Modèle de transcription non chargé."))
            } else {
                err503(pick("Échec de la transcription."))
            }
        }
        Some(Ok(r)) => match r.json::<Value>().await {
            Ok(b) => Json(json!({"text": b.get("text").cloned().unwrap_or(json!(""))})).into_response(),
            Err(_) => err503("Service de transcription injoignable."),
        },
    }
}

/// `_warm_asr`: start the transcription with the voice mode; never blocks.
async fn warm_asr(state: &AppState, user: &str) -> &'static str {
    asr_ondemand::note_activity();
    if media::asr_info().await.get("loaded").is_some_and(keys::truthy) {
        return "ready";
    }
    if matches!(sidecars::proc_status("asr").await.as_str(), "stopped" | "unknown") && (asr_ondemand::memory_refusal().is_some() || !asr_ondemand::autostart(&state.db, user).await.0) {
        return "unavailable";
    }
    "starting"
}

async fn kyutai_ready() -> bool {
    let info = media::voice_chat_info_aged(std::time::Duration::ZERO).await;
    info.get("engine").and_then(Value::as_str) == Some("kyutai") && info.get("loaded").is_some_and(keys::truthy)
}

static VOICE_STARTS: std::sync::Mutex<Option<(std::time::Instant, bool)>> = std::sync::Mutex::new(None);

/// `sidecar_autostart('voice', _kyutai_ready, 120)`: Ok, or the reason.
async fn voice_autostart(state: &AppState) -> Result<(), String> {
    if kyutai_ready().await {
        return Ok(());
    }
    if let Some(a) = asr_ondemand::mem_available_gb() {
        if a < VOICE_MEM_NEED_GB {
            return Err(format!(
                "Mémoire insuffisante pour démarrer voice : {a:.0} Go libres, ~{} Go requis. Arrête un autre backend, ou réduis le contexte du modèle de chat, puis réessaie.",
                VOICE_MEM_NEED_GB as i64
            ));
        }
    }
    let last = *VOICE_STARTS.lock().unwrap_or_else(|e| e.into_inner());
    let recent = last.is_some_and(|(t, _)| t.elapsed() < std::time::Duration::from_secs(60));
    if recent && last.is_some_and(|(_, ok)| !ok) {
        return Err("le service est arrêté et son démarrage a échoué à l'instant".into());
    }
    if !recent {
        let (ok, detail) = asr_ondemand::sidecar_action(&state.db, "voice", "start", "système", "démarrage au premier usage (outil du playground)").await;
        *VOICE_STARTS.lock().unwrap_or_else(|e| e.into_inner()) = Some((std::time::Instant::now(), ok));
        if !ok {
            return Err(format!("le service est arrêté et n'a pas pu démarrer{detail}"));
        }
    }
    let end = std::time::Instant::now() + std::time::Duration::from_secs(120);
    while std::time::Instant::now() < end {
        if kyutai_ready().await {
            return Ok(());
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
    Err("le service a démarré mais ne répond pas encore".into())
}

/// POST /api/voice/chat/start - wake the voice mode (voice container + dictation)
pub async fn api_voice_chat_start(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    if let Some(r) = maintenance(state.db.maintenance_active().await, &session) {
        return r;
    }
    let asr = warm_asr(&state, &session.username).await;
    let merge = |mut b: Value, extra: Value| {
        if let (Some(o), Some(e)) = (b.as_object_mut(), extra.as_object()) {
            o.extend(e.clone());
        }
        b
    };
    if !kyutai_ready().await {
        if let Err(reason) = voice_autostart(&state).await {
            let body = media::voice_chat_body().await;
            if body.get("wrong_engine").and_then(Value::as_bool) == Some(true) {
                return (StatusCode::CONFLICT, Json(merge(body, json!({"ok": false, "code": "voice_engine", "asr": asr, "error": "Le moteur vocal chargé n'est pas Kyutai TTS."})))).into_response();
            }
            return (StatusCode::SERVICE_UNAVAILABLE, Json(merge(body, json!({"ok": false, "code": "voice_unavailable", "asr": asr, "error": reason})))).into_response();
        }
    }
    Json(merge(media::voice_chat_body().await, json!({"ok": true, "asr": asr}))).into_response()
}

/// POST /api/voice/speak - one sentence → raw PCM (s16le, mono), streamed
pub async fn api_voice_speak(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    if let Some(r) = maintenance(state.db.maintenance_active().await, &session) {
        return r;
    }
    if let Some(r) = rate(&state, &session.username, "rl-speak", SPEAK_RATE_MAX).await {
        return r;
    }
    let data = json_object(&headers, &body);
    let raw = match data.get("text") {
        Some(v) if keys::truthy(v) => chat_py_str(v),
        _ => String::new(),
    };
    let text = raw.split(memory::py_is_space).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ");
    if text.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "Texte vide."}))).into_response();
    }
    if text.chars().count() > SPEAK_MAX_CHARS {
        return (StatusCode::PAYLOAD_TOO_LARGE, Json(json!({"error": format!("Texte trop long ({SPEAK_MAX_CHARS} caractères max).")}))).into_response();
    }
    let voice: String = match data.get("voice") {
        Some(v) if keys::truthy(v) => chat_py_str(v).chars().take(16).collect(),
        _ => String::new(),
    };
    let Ok(c) = reqwest::Client::builder().connect_timeout(std::time::Duration::from_secs(5)).read_timeout(std::time::Duration::from_secs(60)).build() else {
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "La voix ne répond pas.", "code": "voice_unavailable"}))).into_response();
    };
    let up = match c.post(format!("{}/api/speak", media::sidecar_url("VOICE_URL", "http://voice:8004"))).json(&json!({"text": text, "voice": voice})).send().await {
        Ok(r) => r,
        Err(_) => return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "La voix ne répond pas.", "code": "voice_unavailable"}))).into_response(),
    };
    if !up.status().is_success() {
        if up.status().as_u16() == 404 {
            return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "Le moteur vocal chargé n'est pas Kyutai TTS.", "code": "voice_engine"}))).into_response();
        }
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "La voix a refusé la phrase.", "status": up.status().as_u16()}))).into_response();
    }
    let rate_hdr = up.headers().get("x-sample-rate").and_then(|v| v.to_str().ok()).unwrap_or("24000").to_string();
    (
        [
            (axum::http::header::CONTENT_TYPE, "application/octet-stream".to_string()),
            (axum::http::HeaderName::from_static("x-sample-rate"), rate_hdr),
            (axum::http::header::CACHE_CONTROL, "no-store".to_string()),
            (axum::http::HeaderName::from_static("x-accel-buffering"), "no".to_string()),
        ],
        axum::body::Body::from_stream(up.bytes_stream()),
    )
        .into_response()
}

/// Python's `str(x)` of a JSON value (repr for containers).
fn chat_py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}
