//! Voice cloning (voice_routes.py `/api/voice/generate`): a reference sample
//! and a text in, a synthesized clip out, stored and kept in the history.
//! Generation is SYNCHRONOUS on the sidecar side; two protocols depending on
//! the loaded engine (Qwen3-TTS: one multipart POST; Chatterbox: upload the
//! reference, then generate by its file name). Kyutai cannot clone.
#![allow(unused_imports)]
use super::*;
use axum::extract::{FromRequest, Request};

const VOICE_HISTORY_LIMIT: i64 = 20;
const MAX_VOICE_UPLOAD_BYTES: usize = 15 * 1024 * 1024;
const ALLOWED_AUDIO_TYPES: [&str; 4] = ["audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3"];

fn audio_ext(mime: &str) -> &'static str {
    match mime {
        "audio/mpeg" | "audio/mp3" => "mp3",
        _ => "wav",
    }
}

fn voice_url() -> String {
    media::sidecar_url("VOICE_URL", "http://voice:8004")
}

/// An uploaded file: (file name, mimetype without parameters, bytes).
type Upload = (String, String, Vec<u8>);

/// `request.form` + `request.files` of a multipart body (first value of each name).
async fn multipart_parts(state: &AppState, req: Request) -> (HashMap<String, String>, HashMap<String, Upload>) {
    let mut fields = HashMap::new();
    let mut files = HashMap::new();
    let is_multipart = req
        .headers()
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("multipart/form-data"));
    if !is_multipart {
        let headers = req.headers().clone();
        let body = axum::body::to_bytes(req.into_body(), 64 * 1024).await.unwrap_or_default();
        return (form_of(&headers, &body), files);
    }
    let Ok(mut mp) = axum::extract::Multipart::from_request(req, state).await else { return (fields, files) };
    while let Ok(Some(field)) = mp.next_field().await {
        let name = field.name().unwrap_or_default().to_string();
        match field.file_name().map(str::to_string) {
            Some(file_name) => {
                let mime = field.content_type().unwrap_or("").split(';').next().unwrap_or("").trim().to_lowercase();
                let Ok(bytes) = field.bytes().await else { break };
                files.entry(name).or_insert((file_name, mime, bytes.to_vec()));
            }
            None => match field.text().await {
                Ok(t) => {
                    fields.entry(name).or_insert(t);
                }
                Err(_) => break,
            },
        }
    }
    (fields, files)
}

/// `_read_uploaded_audio`: the reference sample, or the refusal text.
fn reference_audio(files: &HashMap<String, Upload>) -> Result<(Vec<u8>, String), &'static str> {
    let Some((name, mime, data)) = files.get("reference").filter(|(n, _, _)| !n.is_empty()) else {
        return Err("Aucun échantillon audio fourni.");
    };
    let _ = name;
    if !ALLOWED_AUDIO_TYPES.contains(&mime.as_str()) {
        return Err("Format audio non supporté (WAV/MP3 uniquement).");
    }
    if data.len() > MAX_VOICE_UPLOAD_BYTES {
        return Err("Échantillon audio trop volumineux (15 Mo max).");
    }
    Ok((data.clone(), mime.clone()))
}

/// `_wav_duration_ms`: duration of a PCM WAV buffer, None when Python's `wave` would refuse it.
pub(crate) fn wav_duration_ms(b: &[u8]) -> Option<i64> {
    if b.len() < 12 || &b[0..4] != b"RIFF" || &b[8..12] != b"WAVE" {
        return None;
    }
    let (mut pos, mut fmt, mut data_len) = (12usize, None, None);
    while pos + 8 <= b.len() {
        let id = &b[pos..pos + 4];
        let size = u32::from_le_bytes(b[pos + 4..pos + 8].try_into().ok()?) as usize;
        let body = pos + 8;
        if id == b"fmt " && body + 16 <= b.len() {
            let tag = u16::from_le_bytes([b[body], b[body + 1]]);
            let channels = u16::from_le_bytes([b[body + 2], b[body + 3]]) as usize;
            let rate = u32::from_le_bytes(b[body + 4..body + 8].try_into().ok()?) as usize;
            let bits = u16::from_le_bytes([b[body + 14], b[body + 15]]) as usize;
            if tag != 1 {
                return None; // wave.Error: unknown format
            }
            fmt = Some((channels, rate, bits.div_ceil(8)));
        } else if id == b"data" {
            data_len = Some(size.min(b.len().saturating_sub(body)));
            break;
        }
        pos = body + size + (size & 1);
    }
    let (channels, rate, width) = fmt?;
    let data_len = data_len?;
    if channels == 0 || width == 0 || rate == 0 {
        return None;
    }
    let frames = data_len / (channels * width);
    Some((frames as i64 * 1000) / rate as i64)
}

/// The sidecar's refusal text (`{"detail": …}`), '' when there is none.
async fn refusal_reason(r: reqwest::Response) -> String {
    r.json::<Value>().await.ok().and_then(|v| v.get("detail").and_then(Value::as_str).map(str::to_string)).unwrap_or_default()
}

fn transport_error(e: &reqwest::Error) -> String {
    if e.is_timeout() { "Le service voix a mis trop de temps à répondre." } else { "Service voix injoignable." }.to_string()
}

/// `voice_clone`: the clip, or the refusal text.
pub(crate) async fn voice_clone(engine: &str, reference: Vec<u8>, mime: &str, text: &str, language: &str, ref_text: &str) -> Result<Vec<u8>, String> {
    if engine == "kyutai" {
        return Err("Le moteur vocal chargé est Kyutai TTS (mode vocal du playground) : il ne clone pas de voix. Un admin peut relancer Chatterbox ou Qwen3-TTS depuis Admin → Modèles.".to_string());
    }
    let client = |secs| reqwest::Client::builder().timeout(std::time::Duration::from_secs(secs)).build().map_err(|_| "Service voix injoignable.".to_string());
    let ext = audio_ext(mime);
    if engine == "qwen3-tts" {
        let part = reqwest::multipart::Part::bytes(reference).file_name(format!("ref.{ext}")).mime_str(mime).map_err(|_| "Service voix injoignable.".to_string())?;
        let form = reqwest::multipart::Form::new()
            .part("reference", part)
            .text("text", text.to_string())
            .text("language", language.to_string())
            .text("ref_text", ref_text.to_string());
        let r = client(180)?.post(format!("{}/clone", voice_url())).multipart(form).send().await.map_err(|e| transport_error(&e))?;
        if !r.status().is_success() {
            let d = refusal_reason(r).await;
            return Err(if d.is_empty() { "Échec de la génération vocale.".to_string() } else { d });
        }
        return r.bytes().await.map(|b| b.to_vec()).map_err(|e| transport_error(&e));
    }
    // Chatterbox: a random reference name (it silently reuses a colliding one).
    let mut rnd = [0u8; 16];
    getrandom::getrandom(&mut rnd).map_err(|_| "Service voix injoignable.".to_string())?;
    let ref_name = format!("{}.{ext}", hex::encode(rnd));
    let part = reqwest::multipart::Part::bytes(reference).file_name(ref_name.clone()).mime_str(mime).map_err(|_| "Service voix injoignable.".to_string())?;
    let r = client(30)?
        .post(format!("{}/upload_reference", voice_url()))
        .multipart(reqwest::multipart::Form::new().part("files", part))
        .send()
        .await
        .map_err(|e| transport_error(&e))?;
    // The refusal reason is in the body, whatever the status.
    let ok = r.status().is_success();
    let body: Value = r.json().await.unwrap_or(Value::Null);
    let errors = body.get("errors").filter(|v| keys::truthy(v)).and_then(Value::as_array).cloned().unwrap_or_default();
    if let Some(first) = errors.first() {
        let reason = first.get("error").and_then(Value::as_str).unwrap_or("");
        return Err(if reason.is_empty() { "Échantillon audio refusé par le service voix.".to_string() } else { format!("Échantillon audio refusé : {reason}") });
    }
    if !ok {
        return Err("Échec de l'envoi de l'échantillon audio.".to_string());
    }
    let r = client(120)?
        .post(format!("{}/tts", voice_url()))
        .json(&json!({"text": text, "voice_mode": "clone", "reference_audio_filename": ref_name, "output_format": "mp3", "language": language}))
        .send()
        .await
        .map_err(|e| transport_error(&e))?;
    if !r.status().is_success() {
        let d = refusal_reason(r).await;
        if d.to_lowercase().contains("failed to synthesize") {
            return Err("Échec de la génération — l'échantillon doit contenir plus de 5 secondes de voix.".to_string());
        }
        return Err(if d.is_empty() { "Échec de la génération vocale.".to_string() } else { d });
    }
    r.bytes().await.map(|b| b.to_vec()).map_err(|e| transport_error(&e))
}

/// The language actually spoken: the asked one if loaded, else 'en', else the first loaded.
fn pick_language(asked: &str, langs: &serde_json::Map<String, Value>) -> String {
    if langs.contains_key(asked) {
        asked.to_string()
    } else if langs.is_empty() || langs.contains_key("en") {
        "en".to_string()
    } else {
        langs.keys().next().cloned().unwrap_or_else(|| "en".to_string())
    }
}

/// POST /api/voice/generate - clone a voice from a sample and speak a text
pub async fn api_voice_generate(State(state): State<AppState>, req: Request) -> Response {
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
    if let Some(refusal) = media_block(&state, &session).await {
        return refusal;
    }
    let (reference, mime) = match reference_audio(&files) {
        Ok(x) => x,
        Err(e) => return (StatusCode::BAD_REQUEST, Json(json!({"error": e}))).into_response(),
    };
    let field = |k: &str, n: usize| -> String { form.get(k).map(|v| v.trim_matches(memory::py_is_space).chars().take(n).collect()).unwrap_or_default() };
    let text = field("text", 2000);
    if text.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "Un texte est requis."}))).into_response();
    }
    let langs = media::voice_model_info().await.get("supported_languages").filter(|v| keys::truthy(v)).and_then(Value::as_object).cloned().unwrap_or_default();
    let language = pick_language(&field("language", 10), &langs);
    let ref_text = field("ref_text", 2000);
    let started = std::time::Instant::now();
    let engine = sidecars::voice_engine().await;
    let audio = match voice_clone(&engine, reference, &mime, &text, &language, &ref_text).await {
        Ok(a) => a,
        Err(e) => return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": e}))).into_response(),
    };
    let duration_ms = started.elapsed().as_millis() as i64;
    let audio_ms = wav_duration_ms(&audio);
    let mut rnd = [0u8; 16];
    if getrandom::getrandom(&mut rnd).is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let file_name = format!("{}.mp3", hex::encode(rnd));
    if crate::media_store::save(&state.db, "voice_audio", &file_name, audio, "audio/mpeg").await.is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    match store_job(&state, &session.username, &text, &file_name, duration_ms, audio_ms).await {
        Ok(id) => Json(json!({"id": id})).into_response(),
        Err(_) => auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
}

/// The job row, then the history trimmed to its newest rows (their files with them).
async fn store_job(state: &AppState, username: &str, text: &str, file_name: &str, duration_ms: i64, audio_ms: Option<i64>) -> Result<i64, sqlx::Error> {
    let pool = state.db.pool();
    let id = sqlx::query("INSERT INTO voice_jobs (username, text, audio_path, created_at, duration_ms, audio_ms) VALUES (?,?,?,?,?,?)")
        .bind(username)
        .bind(text)
        .bind(file_name)
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .bind(duration_ms)
        .bind(audio_ms)
        .execute(pool)
        .await?
        .last_insert_rowid();
    let stale: Vec<(i64, Option<String>)> = sqlx::query_as(
        "SELECT id, audio_path FROM voice_jobs WHERE username=? AND id NOT IN (SELECT id FROM voice_jobs WHERE username=? ORDER BY id DESC LIMIT ?)",
    )
    .bind(username)
    .bind(username)
    .bind(VOICE_HISTORY_LIMIT)
    .fetch_all(pool)
    .await?;
    for (_, path) in &stale {
        if let Some(p) = path.as_deref().filter(|p| !p.is_empty()) {
            crate::media_store::delete(&state.db, "voice_audio", p).await;
        }
    }
    for (stale_id, _) in &stale {
        sqlx::query("DELETE FROM voice_jobs WHERE id=?").bind(stale_id).execute(pool).await?;
    }
    Ok(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wav(rate: u32, channels: u16, bits: u16, frames: u32, tag: u16) -> Vec<u8> {
        let width = (bits / 8) as u32;
        let data_len = frames * width * channels as u32;
        let mut b = Vec::new();
        b.extend_from_slice(b"RIFF");
        b.extend_from_slice(&(36 + data_len).to_le_bytes());
        b.extend_from_slice(b"WAVEfmt ");
        b.extend_from_slice(&16u32.to_le_bytes());
        b.extend_from_slice(&tag.to_le_bytes());
        b.extend_from_slice(&channels.to_le_bytes());
        b.extend_from_slice(&rate.to_le_bytes());
        b.extend_from_slice(&(rate * width * channels as u32).to_le_bytes());
        b.extend_from_slice(&((width * channels as u32) as u16).to_le_bytes());
        b.extend_from_slice(&bits.to_le_bytes());
        b.extend_from_slice(b"data");
        b.extend_from_slice(&data_len.to_le_bytes());
        b.extend(std::iter::repeat(0u8).take(data_len as usize));
        b
    }

    #[test]
    fn wav_durations_like_python_wave() {
        assert_eq!(wav_duration_ms(&wav(24000, 1, 16, 36000, 1)), Some(1500));
        assert_eq!(wav_duration_ms(&wav(44100, 2, 16, 44100, 1)), Some(1000));
        assert_eq!(wav_duration_ms(&wav(24000, 1, 32, 100, 3)), None); // float: wave refuses it
        assert_eq!(wav_duration_ms(b"ID3 mp3 data"), None);
    }

    #[test]
    fn language_falls_back_like_flask() {
        let m = |ks: &[&str]| ks.iter().map(|k| (k.to_string(), json!(k))).collect::<serde_json::Map<_, _>>();
        assert_eq!(pick_language("fr", &m(&["en", "fr"])), "fr");
        assert_eq!(pick_language("de", &m(&["en", "fr"])), "en");
        assert_eq!(pick_language("de", &m(&[])), "en");
        assert_eq!(pick_language("de", &m(&["fr", "it"])), "fr");
    }

    /// Both cloning protocols against fake sidecars (VOICE_URL is process-wide: one test).
    #[tokio::test]
    async fn both_cloning_protocols() {
        use axum::routing::post;
        let app = axum::Router::new()
            .route("/clone", post(|mut mp: axum::extract::Multipart| async move {
                let mut seen = Vec::new();
                while let Ok(Some(f)) = mp.next_field().await {
                    seen.push(format!("{}={}", f.name().unwrap_or(""), f.file_name().unwrap_or("")));
                }
                assert!(seen.contains(&"reference=ref.wav".to_string()) && seen.contains(&"text=".to_string()));
                b"QWEN-AUDIO".to_vec()
            }))
            .route("/upload_reference", post(|mut mp: axum::extract::Multipart| async move {
                let f = mp.next_field().await.unwrap().unwrap();
                assert_eq!(f.name(), Some("files"));
                let name = f.file_name().unwrap().to_string();
                assert!(name.ends_with(".mp3") && name.len() == 36);
                if f.bytes().await.unwrap().as_ref() == b"short" {
                    return Json(json!({"errors": [{"error": "trop court"}]}));
                }
                Json(json!({"uploaded": [name]}))
            }))
            .route("/tts", post(|Json(b): Json<Value>| async move {
                assert_eq!(b["voice_mode"], "clone");
                assert_eq!(b["output_format"], "mp3");
                if b["text"] == "fail" {
                    return (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"detail": "Failed to synthesize speech"}))).into_response();
                }
                b"CHATTERBOX-AUDIO".to_vec().into_response()
            }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        std::env::set_var("VOICE_URL", format!("http://{addr}"));

        assert_eq!(voice_clone("qwen3-tts", b"ref".to_vec(), "audio/wav", "bonjour", "fr", "").await.unwrap(), b"QWEN-AUDIO");
        assert_eq!(voice_clone("chatterbox", b"ref".to_vec(), "audio/mpeg", "bonjour", "fr", "").await.unwrap(), b"CHATTERBOX-AUDIO");
        assert_eq!(voice_clone("chatterbox", b"short".to_vec(), "audio/mpeg", "x", "fr", "").await.unwrap_err(), "Échantillon audio refusé : trop court");
        assert!(voice_clone("chatterbox", b"ref".to_vec(), "audio/mpeg", "fail", "fr", "").await.unwrap_err().contains("plus de 5 secondes"));
        assert!(voice_clone("kyutai", vec![], "audio/wav", "x", "fr", "").await.unwrap_err().contains("Kyutai"));
    }
}
