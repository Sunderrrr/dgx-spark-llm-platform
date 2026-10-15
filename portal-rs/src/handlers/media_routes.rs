//! Stored media files (media_store.serve): an image of a job, an OCR scan, a
//! voice audio. Each is the viewer's own (checked in the same SQL query as the
//! lookup), read from the local volume first, then from the bucket with the
//! browser's Range passed through.
#![allow(unused_imports)]
use super::*;
use axum::http::Uri;

pub(crate) const KINDS: [&str; 4] = ["image_files", "video_files", "ocr_images", "voice_audio"];

/// `media_store._check`: a server-made bare file name, never a client path.
pub(crate) fn valid_media_name(kind: &str, name: &str) -> bool {
    let mut chars = name.chars();
    KINDS.contains(&kind)
        && chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && name.chars().count() <= 201
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
        && !name.contains("..")
}

/// `media_store.serve`: the file's answer, or None when it exists nowhere.
pub(crate) async fn serve_media(state: &AppState, headers: &HeaderMap, kind: &str, name: &str, mime: &str) -> Option<Response> {
    if !valid_media_name(kind, name) {
        return None;
    }
    let local = format!("/app/data/{kind}/{name}");
    if let Ok(meta) = tokio::fs::metadata(&local).await {
        if meta.is_file() && meta.len() > 0 {
            let data = tokio::fs::read(&local).await.ok()?;
            return Some(([(axum::http::header::CONTENT_TYPE, mime.to_string())], data).into_response());
        }
    }
    let cfg = s3::Config::load(&state.db).await?;
    let range = headers
        .get(axum::http::header::RANGE)
        .and_then(|v| v.to_str().ok())
        .filter(|r| r.strip_prefix("bytes=").is_some_and(|rest| {
            let (a, b) = rest.split_once('-').unwrap_or(("x", ""));
            a.chars().all(|c| c.is_ascii_digit()) && b.chars().all(|c| c.is_ascii_digit())
        }));
    let key = cfg.key(&["media", kind, name]);
    let resp = match s3::get(&cfg, &key, range).await {
        Ok(s3::Get::Found(r)) => r,
        Ok(_) => return None,
        Err(e) => {
            eprintln!("[media_store] S3 read failed for {kind}/{name}: {e}");
            return None;
        }
    };
    let content_range = resp.headers().get(reqwest::header::CONTENT_RANGE).and_then(|v| v.to_str().ok()).map(str::to_string);
    let length = resp.headers().get(reqwest::header::CONTENT_LENGTH).and_then(|v| v.to_str().ok()).map(str::to_string);
    let status = if content_range.is_some() { StatusCode::PARTIAL_CONTENT } else { StatusCode::OK };
    let mut builder = axum::http::Response::builder()
        .status(status)
        .header(axum::http::header::CONTENT_TYPE, mime)
        .header(axum::http::header::ACCEPT_RANGES, "bytes")
        .header(axum::http::header::CACHE_CONTROL, "private, max-age=3600");
    if let Some(l) = length {
        builder = builder.header(axum::http::header::CONTENT_LENGTH, l);
    }
    if let Some(cr) = content_range {
        builder = builder.header(axum::http::header::CONTENT_RANGE, cr);
    }
    builder.body(axum::body::Body::from_stream(resp.bytes_stream())).ok().map(IntoResponse::into_response)
}

/// IMAGE_EXT values, in Flask's order, and their types.
const IMAGE_EXTS: [&str; 3] = ["png", "jpg", "webp"];
fn image_mime(ext: &str) -> &'static str {
    match ext {
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        _ => "image/png",
    }
}

/// `find_image`: the stored name of image <idx> of a job — local first, then the bucket.
async fn find_image(state: &AppState, safe: &str, idx: i64) -> Option<String> {
    let mut names: Vec<String> = IMAGE_EXTS.iter().map(|e| format!("{safe}_{idx}.{e}")).collect();
    if idx == 0 {
        names.push(format!("{safe}.png"));
    }
    if !valid_media_name("image_files", &names[0]) {
        return None;
    }
    for n in &names {
        if tokio::fs::metadata(format!("/app/data/image_files/{n}")).await.is_ok_and(|m| m.is_file()) {
            return Some(n.clone());
        }
    }
    let cfg = s3::Config::load(&state.db).await?;
    for n in names {
        if s3::exists(&cfg, &cfg.key(&["media", "image_files", &n])).await {
            return Some(n);
        }
    }
    None
}

/// `login_required` on these page paths: signed out → 302 to the login page, not 401.
async fn page_viewer(state: &AppState, headers: &HeaderMap, path: &str) -> Result<auth::SessionData, Response> {
    match auth::require_session(headers, &state.db).await {
        Ok(s) => Ok(s),
        Err(auth::AuthError::Unauthorized) => Err(auth::login_redirect(path)),
        Err(e) => Err(e.into_response()),
    }
}

async fn image_file_impl(state: AppState, headers: HeaderMap, path: String, prompt_id: String, idx: i64) -> Response {
    // An encoded slash is a separator once werkzeug decodes the path: no route matches.
    if prompt_id.contains('/') {
        return auth::html_status(StatusCode::NOT_FOUND);
    }
    let session = match page_viewer(&state, &headers, &path).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let owned = sqlx::query_scalar::<_, i64>("SELECT 1 FROM image_jobs WHERE prompt_id = ? AND username = ?")
        .bind(&prompt_id)
        .bind(&session.username)
        .fetch_optional(state.db.pool())
        .await
        .ok()
        .flatten()
        .is_some();
    if !owned {
        return auth::html_status(StatusCode::NOT_FOUND);
    }
    let safe: String = prompt_id.chars().filter(|c| matches!(c, 'a'..='f' | '0'..='9')).collect();
    if safe.is_empty() {
        return auth::html_status(StatusCode::NOT_FOUND);
    }
    let idx = idx.clamp(0, 3);
    let Some(name) = find_image(&state, &safe, idx).await else { return auth::html_status(StatusCode::NOT_FOUND) };
    let ext = name.rsplit('.').next().unwrap_or("png").to_lowercase();
    serve_media(&state, &headers, "image_files", &name, image_mime(&ext))
        .await
        .unwrap_or_else(|| auth::html_status(StatusCode::NOT_FOUND))
}

/// GET /image/file/:prompt_id - image 0 of one of the viewer's jobs
pub async fn image_file(State(state): State<AppState>, uri: Uri, headers: HeaderMap, Path(prompt_id): Path<String>) -> Response {
    image_file_impl(state, headers, uri.path().to_string(), prompt_id, 0).await
}

/// GET /image/file/:prompt_id/:idx - image <idx> of one of the viewer's jobs
pub async fn image_file_idx(State(state): State<AppState>, uri: Uri, headers: HeaderMap, Path((prompt_id, raw)): Path<(String, String)>) -> Response {
    let Some(idx) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    image_file_impl(state, headers, uri.path().to_string(), prompt_id, idx).await
}

/// Python's `mimetypes.guess_type` for the scan extensions, as the container knows them.
fn guess_mime(path: &str) -> &'static str {
    match path.rsplit_once('.').map(|(_, e)| e.to_lowercase()).as_deref() {
        Some("png") => "image/png",
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("bmp") => "image/bmp",
        Some("tif" | "tiff") => "image/tiff",
        Some("pdf") => "application/pdf",
        Some("heic") => "image/heic",
        Some("avif") => "image/avif",
        Some("svg") => "image/svg+xml",
        _ => "application/octet-stream",
    }
}

/// GET /ocr/image/:job_id - the scan of one of the viewer's OCR jobs
pub async fn ocr_image(State(state): State<AppState>, uri: Uri, headers: HeaderMap, Path(raw): Path<String>) -> Response {
    let Some(job_id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match page_viewer(&state, &headers, uri.path()).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let path: Option<Option<String>> = sqlx::query_scalar("SELECT image_path FROM ocr_jobs WHERE id = ? AND username = ?")
        .bind(job_id)
        .bind(&session.username)
        .fetch_optional(state.db.pool())
        .await
        .ok()
        .flatten();
    let Some(Some(path)) = path.filter(|p| p.as_deref().is_some_and(|s| !s.is_empty())) else {
        return auth::html_status(StatusCode::NOT_FOUND);
    };
    serve_media(&state, &headers, "ocr_images", &path, guess_mime(&path))
        .await
        .unwrap_or_else(|| auth::html_status(StatusCode::NOT_FOUND))
}

/// GET /voice/audio/:job_id - the audio of one of the viewer's voice jobs
pub async fn voice_audio(State(state): State<AppState>, uri: Uri, headers: HeaderMap, Path(raw): Path<String>) -> Response {
    let Some(job_id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match page_viewer(&state, &headers, uri.path()).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let path: Option<Option<String>> = sqlx::query_scalar("SELECT audio_path FROM voice_jobs WHERE id = ? AND username = ?")
        .bind(job_id)
        .bind(&session.username)
        .fetch_optional(state.db.pool())
        .await
        .ok()
        .flatten();
    let Some(row) = path else { return auth::html_status(StatusCode::NOT_FOUND) };
    let Some(path) = row else { return auth::html_status(StatusCode::NOT_FOUND) };
    serve_media(&state, &headers, "voice_audio", &path, "audio/mpeg")
        .await
        .unwrap_or_else(|| auth::html_status(StatusCode::NOT_FOUND))
}
