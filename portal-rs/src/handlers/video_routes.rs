//! Video generation through ComfyUI (video_routes.py + comfyui_client.py):
//! submit a MiniMax H3 graph (text only, or with a reference image), follow
//! it, cancel it, and serve the result — from the stored copy, else from
//! ComfyUI's output directory on disk, else pulled from ComfyUI and cached.
#![allow(unused_imports)]
use super::*;
use axum::extract::{FromRequest, Request};
use axum::http::Uri;

const VIDEO_HISTORY_LIMIT: i64 = 10;
const MAX_UPLOAD_BYTES: usize = 15 * 1024 * 1024;
const MAX_IMAGE_PIXELS: u64 = 40_000_000;
const T2V_TEMPLATE: &str = include_str!("../../workflows/h3_t2v_template.json");
const R2V_TEMPLATE: &str = include_str!("../../workflows/h3_r2v_template.json");

fn comfyui_url() -> String {
    media::sidecar_url("COMFYUI_URL", "http://host.docker.internal:8188")
}

fn client(secs: u64) -> Option<reqwest::Client> {
    reqwest::Client::builder().timeout(std::time::Duration::from_secs(secs)).build().ok()
}

/// `_dimensions_image`: (width, height) from the PNG/JPEG/WebP header.
pub(crate) fn image_dimensions(d: &[u8], mime: &str) -> Option<(u64, u64)> {
    let be16 = |i: usize| -> Option<u64> { Some(u16::from_be_bytes(d.get(i..i + 2)?.try_into().ok()?) as u64) };
    let le16 = |i: usize| -> Option<u64> { Some(u16::from_le_bytes(d.get(i..i + 2)?.try_into().ok()?) as u64) };
    let le24 = |i: usize| -> Option<u64> { let b = d.get(i..i + 3)?; Some(b[0] as u64 | (b[1] as u64) << 8 | (b[2] as u64) << 16) };
    match mime {
        "image/png" => {
            if d.get(..8)? != b"\x89PNG\r\n\x1a\n" || d.get(12..16)? != b"IHDR" {
                return None;
            }
            Some((u32::from_be_bytes(d.get(16..20)?.try_into().ok()?) as u64, u32::from_be_bytes(d.get(20..24)?.try_into().ok()?) as u64))
        }
        "image/jpeg" => {
            let mut i = 2;
            while i + 9 < d.len() {
                if d[i] != 0xFF {
                    return None;
                }
                let marker = d[i + 1];
                if marker == 0x01 || (0xD0..=0xD8).contains(&marker) {
                    i += 2;
                    continue;
                }
                let size = be16(i + 2)? as usize;
                if matches!(marker, 0xC0 | 0xC1 | 0xC2 | 0xC3 | 0xC5 | 0xC6 | 0xC7 | 0xC9 | 0xCA | 0xCB | 0xCD | 0xCE | 0xCF) {
                    return Some((be16(i + 7)?, be16(i + 5)?));
                }
                if marker == 0xDA {
                    return None;
                }
                i += 2 + size;
            }
            None
        }
        "image/webp" => {
            if d.get(..4)? != b"RIFF" || d.get(8..12)? != b"WEBP" {
                return None;
            }
            match d.get(12..16)? {
                b"VP8X" => Some((le24(24)? + 1, le24(27)? + 1)),
                b"VP8 " => Some((le16(26)? & 0x3FFF, le16(28)? & 0x3FFF)),
                b"VP8L" => {
                    let bits = u32::from_le_bytes(d.get(21..25)?.try_into().ok()?) as u64;
                    Some(((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1))
                }
                _ => None,
            }
        }
        _ => None,
    }
}

/// `_read_uploaded_image`: the reference image, or the refusal text.
pub(crate) fn reference_image(name: &str, mime: &str, data: &[u8]) -> Result<(), String> {
    if name.is_empty() {
        return Err("Aucune image fournie.".into());
    }
    if !["image/png", "image/jpeg", "image/webp"].contains(&mime) {
        return Err("Format d'image non supporté (PNG/JPEG/WebP uniquement).".into());
    }
    if data.len() > MAX_UPLOAD_BYTES {
        return Err("Image trop volumineuse (15 Mo max).".into());
    }
    let Some((w, h)) = image_dimensions(data, mime) else { return Err("Image illisible (en-tête PNG/JPEG/WebP non reconnu).".into()) };
    if w * h > MAX_IMAGE_PIXELS {
        return Err(format!("Image trop grande : {w}×{h} pixels ({} Mpx max).", MAX_IMAGE_PIXELS / 1_000_000));
    }
    Ok(())
}

/// `comfyui_generate` without the network: the graph to submit.
pub(crate) fn h3_graph(uploaded: Option<&str>, prompt: &str, duration: f64, seed: u32) -> Option<Value> {
    let mut g: Value = serde_json::from_str(if uploaded.is_some() { R2V_TEMPLATE } else { T2V_TEMPLATE }).ok()?;
    if let Some(name) = uploaded {
        *g.pointer_mut("/137/inputs/image")? = json!(name);
    }
    *g.pointer_mut("/138/inputs/value")? = json!(prompt.chars().take(10000).collect::<String>());
    // Python's max(2, min(15, d)): a bound comes back as the int literal.
    *g.pointer_mut("/132/inputs/value")? = if duration < 2.0 { json!(2) } else if duration > 15.0 { json!(15) } else { json!(duration) };
    *g.pointer_mut("/129/inputs/noise_seed")? = json!(seed);
    Some(g)
}

/// `comfyui_generate`: the prompt id, or None.
pub(crate) async fn comfyui_generate(image: Option<Vec<u8>>, prompt: &str, duration: f64) -> Option<String> {
    let mut uploaded = None;
    if let Some(bytes) = image {
        let part = reqwest::multipart::Part::bytes(bytes).file_name("ref.png");
        let form = reqwest::multipart::Form::new().part("image", part).text("type", "input");
        let r = client(30)?.post(format!("{}/upload/image", comfyui_url())).multipart(form).send().await.ok()?;
        if !r.status().is_success() {
            return None;
        }
        uploaded = Some(r.json::<Value>().await.ok()?.get("name")?.as_str()?.to_string());
    }
    let mut seed = [0u8; 4];
    getrandom::getrandom(&mut seed).ok()?;
    let graph = h3_graph(uploaded.as_deref(), prompt, duration, u32::from_le_bytes(seed))?;
    let r = client(15)?.post(format!("{}/prompt", comfyui_url())).json(&json!({"prompt": graph})).send().await.ok()?;
    if !r.status().is_success() {
        return None;
    }
    r.json::<Value>().await.ok()?.get("prompt_id")?.as_str().map(str::to_string)
}

/// `comfyui_status`: {'status', 'video_path', …} of a prompt.
pub(crate) async fn comfyui_status(prompt_id: &str) -> Value {
    let error = json!({"status": "error", "video_path": null});
    let probe = async {
        let c = client(5)?;
        let r = c.get(format!("{}/history/{prompt_id}", comfyui_url())).send().await.ok()?;
        if r.status().is_success() {
            let hist: Value = r.json().await.ok()?;
            if let Some(entry) = hist.get(prompt_id) {
                if entry.pointer("/status/status_str").and_then(Value::as_str) == Some("error") {
                    return Some(error.clone());
                }
                let videos = entry.pointer("/outputs/92/images").and_then(Value::as_array).cloned().unwrap_or_default();
                if let Some(v) = videos.first() {
                    return Some(json!({"status": "done", "video_path": v.get("filename").cloned().unwrap_or(Value::Null),
                        "video_subfolder": v.get("subfolder").cloned().unwrap_or(json!("")),
                        "video_type": v.get("type").cloned().unwrap_or(json!("output"))}));
                }
                return Some(error.clone());
            }
        }
        let q: Value = c.get(format!("{}/queue", comfyui_url())).send().await.ok()?.json().await.ok()?;
        let ids = |k: &str| -> Vec<String> {
            q.get(k).and_then(Value::as_array).map(|a| a.iter().filter_map(|i| i.get(1).and_then(Value::as_str).map(str::to_string)).collect()).unwrap_or_default()
        };
        if ids("queue_running").iter().any(|i| i == prompt_id) {
            return Some(json!({"status": "running", "video_path": null}));
        }
        if ids("queue_pending").iter().any(|i| i == prompt_id) {
            return Some(json!({"status": "pending", "video_path": null}));
        }
        None
    };
    probe.await.unwrap_or(error)
}

/// `comfyui_cancel`: removes a queued prompt, or interrupts the running one.
async fn comfyui_cancel(prompt_id: &str) {
    let Some(c) = client(10) else { return };
    let Ok(r) = c.get(format!("{}/queue", comfyui_url())).send().await else { return };
    let Ok(q) = r.json::<Value>().await else { return };
    let has = |k: &str| q.get(k).and_then(Value::as_array).is_some_and(|a| a.iter().any(|i| i.get(1).and_then(Value::as_str) == Some(prompt_id)));
    if has("queue_pending") {
        let _ = c.post(format!("{}/queue", comfyui_url())).json(&json!({"delete": [prompt_id]})).send().await;
    } else if has("queue_running") {
        let _ = c.post(format!("{}/interrupt", comfyui_url())).send().await;
    }
}

/// `comfyui_fetch_video`: ComfyUI's /view answer, streaming.
async fn comfyui_fetch(st: &Value) -> Option<reqwest::Response> {
    let s = |k: &str, d: &str| st.get(k).and_then(Value::as_str).unwrap_or(d).to_string();
    let r = client(30)?
        .get(format!("{}/view", comfyui_url()))
        .query(&[("filename", s("video_path", "")), ("subfolder", s("video_subfolder", "")), ("type", s("video_type", "output"))])
        .send()
        .await
        .ok()?;
    r.status().is_success().then_some(r)
}

/// `_video_name`: the stored file name of a job's video.
pub(crate) fn video_name(prompt_id: &str) -> Option<String> {
    let safe: String = prompt_id.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-').collect();
    (!safe.is_empty()).then(|| format!("{safe}.mp4"))
}

/// `_comfyui_output_file`: a file inside the mounted output dir, traversal refused.
async fn output_file(video_path: &str, subfolder: &str) -> Option<std::path::PathBuf> {
    if video_path.is_empty() {
        return None;
    }
    let root_dir = std::env::var("COMFYUI_OUTPUT_DIR").unwrap_or_else(|_| "/comfyui-output".to_string());
    let root = tokio::fs::canonicalize(&root_dir).await.ok()?;
    let cand = tokio::fs::canonicalize(root.join(subfolder).join(video_path)).await.ok()?;
    (cand != root && cand.starts_with(&root) && tokio::fs::metadata(&cand).await.ok()?.is_file()).then_some(cand)
}

/// `_cache_video_local`: download the finished MP4 once and store it; the stored name.
async fn cache_video(state: &AppState, prompt_id: &str, st: &Value) -> Option<String> {
    let name = video_name(prompt_id)?;
    if crate::media_store::exists(&state.db, "video_files", &name).await {
        return Some(name);
    }
    st.get("video_path").and_then(Value::as_str).filter(|p| !p.is_empty())?;
    let bytes = comfyui_fetch(st).await?.bytes().await.ok()?;
    if bytes.is_empty() {
        return None;
    }
    crate::media_store::save(&state.db, "video_files", &name, bytes.to_vec(), "video/mp4").await.ok()?;
    Some(name)
}

/// `_persist_video`: a known result written into the job, the MP4 cached.
pub(crate) async fn persist_video(state: &AppState, prompt_id: &str, username: &str, st: &Value, created_at: Option<&str>, duration_ms: Option<i64>) {
    let status = st.get("status").and_then(Value::as_str).unwrap_or("");
    if status != "done" && status != "error" {
        return;
    }
    let pool = state.db.pool();
    let _ = sqlx::query("UPDATE video_jobs SET status=?, video_path=?, video_subfolder=?, video_type=? WHERE prompt_id=? AND username=?")
        .bind(status)
        .bind(st.get("video_path").and_then(Value::as_str))
        .bind(st.get("video_subfolder").and_then(Value::as_str))
        .bind(st.get("video_type").and_then(Value::as_str))
        .bind(prompt_id)
        .bind(username)
        .execute(pool)
        .await;
    if status == "done" && duration_ms.is_none() {
        if let Some(created) = created_at.and_then(|c| chrono::NaiveDateTime::parse_from_str(c, "%Y-%m-%dT%H:%M:%S%.f").ok()) {
            let hard = (chrono::Local::now().naive_local() - created).num_milliseconds();
            if 0 < hard && hard < 3_600_000 {
                let _ = sqlx::query("UPDATE video_jobs SET duration_ms=? WHERE prompt_id=? AND username=? AND duration_ms IS NULL")
                    .bind(hard)
                    .bind(prompt_id)
                    .bind(username)
                    .execute(pool)
                    .await;
            }
        }
    }
    if status == "done" {
        cache_video(state, prompt_id, st).await;
    }
}

/// POST /api/video/generate - submit a video (text only, or with a reference image)
pub async fn api_video_generate(State(state): State<AppState>, req: Request) -> Response {
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
    // Only the total ABSENCE of the field switches to text-only.
    let mut image = None;
    if let Some((name, mime, data)) = files.get("image").filter(|(n, _, _)| !n.is_empty()) {
        if let Err(e) = reference_image(name, mime, data) {
            return (StatusCode::BAD_REQUEST, Json(json!({"error": e}))).into_response();
        }
        image = Some(data.clone());
    }
    let prompt = form.get("prompt").map(|p| p.trim_matches(memory::py_is_space).to_string()).unwrap_or_default();
    if prompt.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "Un prompt texte est requis."}))).into_response();
    }
    let duration = form.get("duration").map_or(Some(5.0), |d| d.trim().parse::<f64>().ok()).filter(|d| d.is_finite()).unwrap_or(5.0);
    let Some(pid) = comfyui_generate(image, &prompt, duration).await else {
        let msg = format!("ComfyUI inaccessible ou requête refusée.{}", guards::shared_memory_note());
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": msg}))).into_response();
    };
    let user = &session.username;
    let pool = state.db.pool();
    let stored = async {
        sqlx::query("INSERT INTO video_jobs (username, prompt_id, prompt, created_at, req_duration_s) VALUES (?,?,?,?,?)")
            .bind(user)
            .bind(&pid)
            .bind(&prompt)
            .bind(keys::iso(&chrono::Local::now().naive_local()))
            .bind(duration.trunc() as i64)
            .execute(pool)
            .await?;
        let evicted: Vec<String> = sqlx::query_scalar(
            "SELECT prompt_id FROM video_jobs WHERE username=? AND id NOT IN (SELECT id FROM video_jobs WHERE username=? ORDER BY id DESC LIMIT ?)",
        )
        .bind(user)
        .bind(user)
        .bind(VIDEO_HISTORY_LIMIT)
        .fetch_all(pool)
        .await?;
        sqlx::query("DELETE FROM video_jobs WHERE username=? AND id NOT IN (SELECT id FROM video_jobs WHERE username=? ORDER BY id DESC LIMIT ?)")
            .bind(user)
            .bind(user)
            .bind(VIDEO_HISTORY_LIMIT)
            .execute(pool)
            .await?;
        Ok::<_, sqlx::Error>(evicted)
    }
    .await;
    let Ok(evicted) = stored else { return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
    for old in evicted {
        if let Some(name) = video_name(&old) {
            crate::media_store::delete(&state.db, "video_files", &name).await;
        }
    }
    Json(json!({"prompt_id": pid})).into_response()
}

/// GET /api/video/status/:prompt_id - where one of the viewer's videos stands
pub async fn api_video_status(State(state): State<AppState>, headers: HeaderMap, Path(prompt_id): Path<String>) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let row: Option<(Option<String>, Option<String>, Option<i64>)> = match sqlx::query_as(
        "SELECT status, created_at, duration_ms FROM video_jobs WHERE prompt_id=? AND username=?",
    )
    .bind(&prompt_id)
    .bind(&session.username)
    .fetch_optional(state.db.pool())
    .await
    {
        Ok(r) => r,
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    let Some((status, created_at, duration_ms)) = row else { return auth::html_status(StatusCode::NOT_FOUND) };
    if status.as_deref() == Some("cancelled") {
        return Json(json!({"status": "cancelled", "video_path": null})).into_response();
    }
    let st = comfyui_status(&prompt_id).await;
    persist_video(&state, &prompt_id, &session.username, &st, created_at.as_deref(), duration_ms).await;
    Json(st).into_response()
}

/// POST /api/video/cancel/:prompt_id - stop one of the viewer's videos
pub async fn api_video_cancel(State(state): State<AppState>, headers: HeaderMap, Path(prompt_id): Path<String>, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let status: Option<Option<String>> = match sqlx::query_scalar("SELECT status FROM video_jobs WHERE prompt_id=? AND username=?")
        .bind(&prompt_id)
        .bind(&session.username)
        .fetch_optional(state.db.pool())
        .await
    {
        Ok(s) => s,
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    let Some(status) = status else { return auth::html_status(StatusCode::NOT_FOUND) };
    match status.as_deref() {
        Some("done" | "cancelled") => return Json(json!({"ok": true})).into_response(),
        // Inserted « pending » (the column default); only the end is written back.
        Some("pending" | "running") => {}
        _ => return (StatusCode::BAD_REQUEST, Json(json!({"error": "Ce job n'est plus actif."}))).into_response(),
    }
    comfyui_cancel(&prompt_id).await;
    match sqlx::query("UPDATE video_jobs SET status='cancelled' WHERE prompt_id=? AND username=?")
        .bind(&prompt_id)
        .bind(&session.username)
        .execute(state.db.pool())
        .await
    {
        Ok(_) => Json(json!({"ok": true})).into_response(),
        Err(_) => auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
}

/// GET /video/file/:prompt_id - the MP4 of one of the viewer's videos
pub async fn video_file(State(state): State<AppState>, uri: Uri, headers: HeaderMap, Path(prompt_id): Path<String>) -> Response {
    if prompt_id.contains('/') {
        return auth::html_status(StatusCode::NOT_FOUND);
    }
    let session = match page_viewer(&state, &headers, uri.path()).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let row: Option<(Option<String>, Option<String>, Option<String>)> = match sqlx::query_as(
        "SELECT video_path, video_subfolder, video_type FROM video_jobs WHERE prompt_id=? AND username=?",
    )
    .bind(&prompt_id)
    .bind(&session.username)
    .fetch_optional(state.db.pool())
    .await
    {
        Ok(r) => r,
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    let Some((video_path, subfolder, vtype)) = row else { return auth::html_status(StatusCode::NOT_FOUND) };
    // 1) The stored copy (volume or bucket).
    let name = video_name(&prompt_id);
    if let Some(n) = &name {
        if let Some(r) = serve_media(&state, &headers, "video_files", n, "video/mp4").await {
            return r;
        }
    }
    let video_path = video_path.filter(|p| !p.is_empty());
    // 2) ComfyUI's output dir on disk (read-only mount), even with ComfyUI stopped.
    if let Some(p) = &video_path {
        if let Some(disk) = output_file(p, subfolder.as_deref().unwrap_or("")).await {
            if let Some(r) = serve_local_file(&headers, &disk, "video/mp4", "no-cache").await {
                return r;
            }
        }
    }
    // 3) Pulled from ComfyUI (and cached for next time).
    let st = match &video_path {
        Some(p) => json!({"video_path": p, "video_subfolder": subfolder.unwrap_or_default(), "video_type": vtype.unwrap_or_else(|| "output".into())}),
        None => {
            let st = comfyui_status(&prompt_id).await;
            if st["status"] != "done" || !st["video_path"].as_str().is_some_and(|p| !p.is_empty()) {
                return auth::html_status(StatusCode::NOT_FOUND);
            }
            st
        }
    };
    if let Some(cached) = cache_video(&state, &prompt_id, &st).await {
        if let Some(r) = serve_media(&state, &headers, "video_files", &cached, "video/mp4").await {
            return r;
        }
    }
    let Some(upstream) = comfyui_fetch(&st).await else { return auth::html_status(StatusCode::SERVICE_UNAVAILABLE) };
    (
        [(axum::http::header::CONTENT_TYPE, "video/mp4"), (axum::http::header::CONTENT_DISPOSITION, "inline")],
        axum::body::Body::from_stream(upstream.bytes_stream()),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dimensions_of_the_three_formats() {
        let mut png = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR".to_vec();
        png.extend_from_slice(&640u32.to_be_bytes());
        png.extend_from_slice(&480u32.to_be_bytes());
        assert_eq!(image_dimensions(&png, "image/png"), Some((640, 480)));
        assert_eq!(image_dimensions(&png[..20], "image/png"), None);
        // JPEG: SOI, an APP0 of 16 bytes, then SOF0 with height 300, width 400.
        let mut jpg = vec![0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10];
        jpg.extend(std::iter::repeat(0).take(14));
        jpg.extend_from_slice(&[0xFF, 0xC0, 0x00, 0x11, 0x08, 0x01, 0x2C, 0x01, 0x90, 0x03, 0, 0, 0, 0, 0, 0]);
        assert_eq!(image_dimensions(&jpg, "image/jpeg"), Some((400, 300)));
        let mut webp = b"RIFF\0\0\0\0WEBPVP8X".to_vec();
        webp.extend(std::iter::repeat(0).take(8));
        webp.extend_from_slice(&[0x7F, 0x02, 0x00, 0xDF, 0x01, 0x00]); // 640 x 480, minus one
        assert_eq!(image_dimensions(&webp, "image/webp"), Some((640, 480)));
    }

    #[test]
    fn graphs_substitute_the_four_fields() {
        let g = h3_graph(None, "un chat", 30.0, 7).unwrap();
        assert_eq!(g["138"]["inputs"]["value"], "un chat");
        assert_eq!(g["132"]["inputs"]["value"], json!(15));
        assert_eq!(h3_graph(None, "x", 5.0, 1).unwrap()["132"]["inputs"]["value"], json!(5.0));
        assert_eq!(g["129"]["inputs"]["noise_seed"], 7);
        let g = h3_graph(Some("ref_1.png"), "x", 1.0, 1).unwrap();
        assert_eq!(g["137"]["inputs"]["image"], "ref_1.png");
        assert_eq!(g["132"]["inputs"]["value"], json!(2));
    }

    #[test]
    fn video_names_are_sanitized() {
        assert_eq!(video_name("ab-c_1/../x").as_deref(), Some("ab-c_1x.mp4"));
        assert_eq!(video_name("../"), None);
    }
}
