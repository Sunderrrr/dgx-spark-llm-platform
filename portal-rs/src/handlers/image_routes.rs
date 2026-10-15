//! Image generation (image_routes.py): start a job, cancel it, delete one image
//! of a batch. The job runs in a background task that calls the image sidecar
//! once per variation (sequentially: one image at a time keeps the GPU memory
//! spike at single-image level on unified memory).
#![allow(unused_imports)]
use super::*;
use axum::extract::{FromRequest, Request};

const IMAGE_HISTORY_LIMIT: i64 = 20;
const IMAGE_MAX_BATCH: i64 = 4;

/// `IMAGE_FORMATS`: accepted output formats, « jpg » normalized to « jpeg ».
fn image_format(raw: &str) -> &'static str {
    match raw.trim().to_lowercase().as_str() {
        "jpg" | "jpeg" => "jpeg",
        "webp" => "webp",
        _ => "png",
    }
}

/// `IMAGE_EXT`: the stored extension of a format.
fn image_ext(fmt: &str) -> &'static str {
    match fmt {
        "jpeg" => "jpg",
        "webp" => "webp",
        _ => "png",
    }
}

/// `request.form` of a urlencoded or multipart body.
async fn form_fields(state: &AppState, req: Request) -> HashMap<String, String> {
    let is_multipart = req
        .headers()
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("multipart/form-data"));
    if is_multipart {
        let mut out = HashMap::new();
        if let Ok(mut mp) = axum::extract::Multipart::from_request(req, state).await {
            while let Ok(Some(field)) = mp.next_field().await {
                let name = field.name().unwrap_or_default().to_string();
                if field.file_name().is_some() {
                    continue;
                }
                match field.text().await {
                    Ok(t) => {
                        out.entry(name).or_insert(t);
                    }
                    Err(_) => break,
                }
            }
        }
        return out;
    }
    let headers = req.headers().clone();
    let body = axum::body::to_bytes(req.into_body(), 4 * 1024 * 1024).await.unwrap_or_default();
    form_of(&headers, &body)
}

/// `int(form.get(key) or default)` clamped, the default on a bad value.
fn dim(form: &HashMap<String, String>, key: &str, default: i64, lo: i64, hi: i64) -> i64 {
    let v = form
        .get(key)
        .filter(|s| !s.is_empty())
        .map_or(Some(default), |s| support_routes_py_int(s))
        .unwrap_or(default);
    v.clamp(lo, hi)
}

fn support_routes_py_int(s: &str) -> Option<i64> {
    py_int(&Value::String(s.to_string()))
}

/// `secrets.token_hex(12)`.
fn prompt_id() -> Option<String> {
    let mut b = [0u8; 12];
    getrandom::getrandom(&mut b).ok()?;
    Some(hex::encode(b))
}

/// POST /api/image/generate - start an image job (1-4 variations of one prompt)
pub async fn api_image_generate(State(state): State<AppState>, req: Request) -> Response {
    let headers = req.headers().clone();
    let form = form_fields(&state, req).await;
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
    let prompt = form.get("prompt").map(|p| p.trim_matches(memory::py_is_space).to_string()).unwrap_or_default();
    if prompt.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "Un prompt texte est requis."}))).into_response();
    }
    if !sidecars::image_ready().await {
        let msg = format!("Aucun modèle image configuré.{}", guards::shared_memory_note());
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": msg}))).into_response();
    }
    let count = match form.get("count") {
        None => 1,
        Some(c) => support_routes_py_int(c).unwrap_or(1),
    }
    .clamp(1, IMAGE_MAX_BATCH);
    let fmt = image_format(form.get("format").filter(|f| !f.is_empty()).map_or("png", String::as_str));
    let width = dim(&form, "width", 1024, 256, 1536);
    let height = dim(&form, "height", 1024, 256, 1536);
    let out_width = dim(&form, "out_width", 0, 0, 3840);
    let out_height = dim(&form, "out_height", 0, 0, 3840);
    let Some(pid) = prompt_id() else { return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
    let user = session.username.clone();
    // The slot is taken BEFORE the row: refused, the job must not stay « running » forever.
    if !guards::job_slot(&user) {
        return (StatusCode::TOO_MANY_REQUESTS, Json(json!({"error": "Trop de générations d'images en cours. Attends la fin des précédentes."})))
            .into_response();
    }
    let created = async {
        let mut tx = state.db.pool().begin().await?;
        sqlx::query(
            "INSERT INTO image_jobs (username, prompt_id, prompt, status, count, done_count, format, created_at) VALUES (?,?,?,?,?,?,?,?)",
        )
        .bind(&user)
        .bind(&pid)
        .bind(&prompt)
        .bind("running")
        .bind(count)
        .bind(0)
        .bind(fmt)
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .execute(&mut *tx)
        .await?;
        sqlx::query("DELETE FROM image_jobs WHERE username=? AND id NOT IN (SELECT id FROM image_jobs WHERE username=? ORDER BY id DESC LIMIT ?)")
            .bind(&user)
            .bind(&user)
            .bind(IMAGE_HISTORY_LIMIT)
            .execute(&mut *tx)
            .await?;
        tx.commit().await
    }
    .await;
    if created.is_err() {
        guards::job_done(&user);
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let job = ImageJob { db: state.db.clone(), username: user, prompt_id: pid.clone(), prompt, count, fmt, width, height, out_width, out_height };
    tokio::spawn(async move {
        let u = job.username.clone();
        job.run().await;
        guards::job_done(&u);
    });
    Json(json!({"prompt_id": pid, "count": count})).into_response()
}

/// `media_block_json`: maintenance first (admins pass), then the media rate.
pub(crate) async fn media_block(state: &AppState, session: &auth::SessionData) -> Option<Response> {
    if !session.is_admin && state.db.maintenance_active().await {
        return Some((StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "Mode maintenance en cours — réessaie plus tard."}))).into_response());
    }
    match guards::rate_limited(state.db.pool(), &session.username, "rl-media", guards::MEDIA_RATE_MAX, guards::MEDIA_RATE_WINDOW_S).await {
        Ok(0) => None,
        Ok(wait) => Some((StatusCode::TOO_MANY_REQUESTS, Json(json!({"error": format!("Trop de requêtes. Réessaie dans {wait} s.")}))).into_response()),
        Err(_) => Some(auth::html_status(StatusCode::INTERNAL_SERVER_ERROR)),
    }
}

struct ImageJob {
    db: std::sync::Arc<crate::db::Database>,
    username: String,
    prompt_id: String,
    prompt: String,
    count: i64,
    fmt: &'static str,
    width: i64,
    height: i64,
    out_width: i64,
    out_height: i64,
}

impl ImageJob {
    /// True once the user asked to stop this job (« Arrêter »).
    async fn cancelled(&self) -> bool {
        sqlx::query_scalar::<_, Option<String>>("SELECT status FROM image_jobs WHERE prompt_id=? AND username=?")
            .bind(&self.prompt_id)
            .bind(&self.username)
            .fetch_optional(self.db.pool())
            .await
            .ok()
            .flatten()
            .flatten()
            .as_deref()
            == Some("cancelled")
    }

    /// `_image_worker`: one sidecar call per variation, cancellation checked before each.
    async fn run(self) {
        let started = std::time::Instant::now();
        let ext = image_ext(self.fmt);
        let client = reqwest::Client::builder().timeout(std::time::Duration::from_secs(600)).build().ok();
        let mut done = 0i64;
        for idx in 0..self.count {
            if self.cancelled().await {
                break;
            }
            let Some(client) = client.as_ref() else { break };
            let prompt: String = self.prompt.chars().take(10000).collect();
            let fields = [
                ("prompt", prompt),
                ("format", self.fmt.to_string()),
                ("width", self.width.to_string()),
                ("height", self.height.to_string()),
                ("out_width", self.out_width.to_string()),
                ("out_height", self.out_height.to_string()),
            ];
            let Ok(r) = client.post(format!("{}/generate", sidecars::image_url())).form(&fields).send().await else { continue };
            let ctype = r.headers().get(reqwest::header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
            if !r.status().is_success() || !ctype.starts_with("image/") {
                continue;
            }
            let Ok(bytes) = r.bytes().await else { continue };
            let name = format!("{}_{idx}.{ext}", self.prompt_id);
            if crate::media_store::save(&self.db, "image_files", &name, bytes.to_vec(), &ctype).await.is_ok() {
                done += 1;
                let _ = sqlx::query("UPDATE image_jobs SET done_count=? WHERE prompt_id=? AND username=?")
                    .bind(done)
                    .bind(&self.prompt_id)
                    .bind(&self.username)
                    .execute(self.db.pool())
                    .await;
            }
        }
        let cancelled = self.cancelled().await;
        let status = if cancelled { "cancelled" } else if done > 0 { "done" } else { "error" };
        let duration = (done > 0).then(|| started.elapsed().as_millis() as i64);
        let _ = sqlx::query("UPDATE image_jobs SET status=?, duration_ms=?, done_count=? WHERE prompt_id=? AND username=?")
            .bind(status)
            .bind(duration)
            .bind(done)
            .bind(&self.prompt_id)
            .bind(&self.username)
            .execute(self.db.pool())
            .await;
        if !cancelled {
            let title = if done > 0 { format!("Génération image terminée ({done}/{}).", self.count) } else { "Échec de la génération image.".to_string() };
            add_notification(self.db.pool(), &self.username, "image", &title).await;
        }
    }
}

/// db.add_notification: an in-app notification, the history bounded to 50 per account.
pub(crate) async fn add_notification(pool: &sqlx::SqlitePool, username: &str, kind: &str, title: &str) {
    let who = if username.is_empty() { "système" } else { username };
    let _ = sqlx::query("INSERT INTO notifications (username, kind, title, seen, created_at) VALUES (?,?,?,0,?)")
        .bind(who)
        .bind(kind)
        .bind(title)
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .execute(pool)
        .await;
    let _ = sqlx::query("DELETE FROM notifications WHERE username=? AND id NOT IN (SELECT id FROM notifications WHERE username=? ORDER BY id DESC LIMIT 50)")
        .bind(username)
        .bind(username)
        .execute(pool)
        .await;
}

/// POST /api/image/cancel/:prompt_id - stop one of the viewer's jobs (the current image finishes)
pub async fn api_image_cancel(State(state): State<AppState>, headers: HeaderMap, Path(prompt_id): Path<String>, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let status: Option<Option<String>> = match sqlx::query_scalar("SELECT status FROM image_jobs WHERE prompt_id=? AND username=?")
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
        Some("running") => {}
        _ => return (StatusCode::BAD_REQUEST, Json(json!({"error": "Ce job n'est plus actif."}))).into_response(),
    }
    match sqlx::query("UPDATE image_jobs SET status='cancelled' WHERE prompt_id=? AND username=?")
        .bind(&prompt_id)
        .bind(&session.username)
        .execute(state.db.pool())
        .await
    {
        Ok(_) => Json(json!({"ok": true})).into_response(),
        Err(_) => auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
}

/// POST /api/image/delete/:prompt_id/:idx - remove ONE image of a batch (the job stays)
pub async fn api_image_delete(State(state): State<AppState>, headers: HeaderMap, Path((prompt_id, raw)): Path<(String, String)>, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let Some(idx) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let owned = match sqlx::query_scalar::<_, i64>("SELECT 1 FROM image_jobs WHERE prompt_id=? AND username=?")
        .bind(&prompt_id)
        .bind(&session.username)
        .fetch_optional(state.db.pool())
        .await
    {
        Ok(o) => o.is_some(),
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    if !owned {
        return auth::html_status(StatusCode::NOT_FOUND);
    }
    let safe: String = prompt_id.chars().filter(|c| matches!(c, 'a'..='f' | '0'..='9')).collect();
    if safe.is_empty() {
        return auth::html_status(StatusCode::NOT_FOUND);
    }
    let idx = idx.clamp(0, IMAGE_MAX_BATCH - 1);
    if let Some(name) = find_image(&state, &safe, idx).await {
        crate::media_store::delete(&state.db, "image_files", &name).await;
    }
    Json(json!({"ok": true})).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fake image sidecar: answers a PNG to each /generate, counts the calls.
    async fn fake_sidecar(calls: std::sync::Arc<std::sync::atomic::AtomicUsize>, delay_ms: u64) -> String {
        use axum::routing::post;
        let app = axum::Router::new().route(
            "/generate",
            post(move |axum::extract::Form(f): axum::extract::Form<HashMap<String, String>>| {
                let calls = calls.clone();
                async move {
                    calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    tokio::time::sleep(std::time::Duration::from_millis(delay_ms)).await;
                    assert_eq!(f.get("format").map(String::as_str), Some("png"));
                    assert_eq!(f.get("width").map(String::as_str), Some("512"));
                    ([(axum::http::header::CONTENT_TYPE, "image/png")], b"\x89PNG fake".to_vec())
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        format!("http://{addr}")
    }

    async fn scratch_db(dir: &std::path::Path) -> std::sync::Arc<crate::db::Database> {
        let db = crate::db::Database::new(&format!("sqlite:{}/t.db", dir.display())).await.unwrap();
        for ddl in [
            "CREATE TABLE IF NOT EXISTS image_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, prompt_id TEXT, prompt TEXT, status TEXT, count INTEGER, done_count INTEGER, format TEXT, duration_ms INTEGER, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, kind TEXT, title TEXT, seen INTEGER, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
        ] {
            sqlx::query(ddl).execute(db.pool()).await.unwrap();
        }
        std::sync::Arc::new(db)
    }

    fn job(db: std::sync::Arc<crate::db::Database>, pid: &str, count: i64) -> ImageJob {
        ImageJob { db, username: "u".into(), prompt_id: pid.into(), prompt: "a cat".into(), count, fmt: "png", width: 512, height: 512, out_width: 0, out_height: 0 }
    }

    async fn insert(db: &crate::db::Database, pid: &str, count: i64) {
        sqlx::query("INSERT INTO image_jobs (username, prompt_id, prompt, status, count, done_count, format, created_at) VALUES ('u',?,'a cat','running',?,0,'png','x')")
            .bind(pid).bind(count).execute(db.pool()).await.unwrap();
    }

    /// One test drives both scenarios: IMAGE_URL and MEDIA_ROOT are process-wide.
    #[tokio::test]
    async fn the_worker_stores_each_image_and_honours_a_cancel() {
        let dir = std::env::temp_dir().join(format!("portal-rs-img-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::env::set_var("MEDIA_ROOT", &dir);
        let calls = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        std::env::set_var("IMAGE_URL", fake_sidecar(calls.clone(), 300).await);
        let db = scratch_db(&dir).await;

        // A whole batch: two files, status done, one notification.
        insert(&db, "aa11", 2).await;
        job(db.clone(), "aa11", 2).run().await;
        let (status, done): (String, i64) = sqlx::query_as("SELECT status, done_count FROM image_jobs WHERE prompt_id='aa11'").fetch_one(db.pool()).await.unwrap();
        assert_eq!((status.as_str(), done), ("done", 2));
        assert!(dir.join("image_files/aa11_0.png").is_file() && dir.join("image_files/aa11_1.png").is_file());
        let title: String = sqlx::query_scalar("SELECT title FROM notifications WHERE username='u'").fetch_one(db.pool()).await.unwrap();
        assert_eq!(title, "Génération image terminée (2/2).");

        // Cancelled during the first image: the batch stops there, the status
        // stays « cancelled », and no notification is added.
        insert(&db, "bb22", 4).await;
        let worker = tokio::spawn(job(db.clone(), "bb22", 4).run());
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        sqlx::query("UPDATE image_jobs SET status='cancelled' WHERE prompt_id='bb22'").execute(db.pool()).await.unwrap();
        worker.await.unwrap();
        let (status, done): (String, i64) = sqlx::query_as("SELECT status, done_count FROM image_jobs WHERE prompt_id='bb22'").fetch_one(db.pool()).await.unwrap();
        assert_eq!((status.as_str(), done), ("cancelled", 1));
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 3);
        let notes: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM notifications").fetch_one(db.pool()).await.unwrap();
        assert_eq!(notes, 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn formats_and_extensions() {
        assert_eq!(image_format(" JPG "), "jpeg");
        assert_eq!(image_format("gif"), "png");
        assert_eq!(image_ext("jpeg"), "jpg");
        assert_eq!(image_ext("webp"), "webp");
    }

    #[test]
    fn dimensions_are_clamped_with_defaults() {
        let f: HashMap<String, String> = [("width", "99999"), ("height", ""), ("out_width", "x")].iter().map(|(a, b)| (a.to_string(), b.to_string())).collect();
        assert_eq!(dim(&f, "width", 1024, 256, 1536), 1536);
        assert_eq!(dim(&f, "height", 1024, 256, 1536), 1024);
        assert_eq!(dim(&f, "out_width", 0, 0, 3840), 0);
        assert_eq!(prompt_id().unwrap().len(), 24);
    }
}
