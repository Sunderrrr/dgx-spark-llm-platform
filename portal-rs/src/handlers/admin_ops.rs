//! The instance's identity (admin_config.py `/admin/branding`) and the
//! runner's live log stream (admin_models.py `/admin/runner/stream`).
use super::*;
use super::admin_writes::admin_form;

/// `[\w .\-À-ɏ]{1,60}` (Python's `\w` is Unicode-aware).
fn platform_name_ok(name: &str) -> bool {
    (1..=60).contains(&name.chars().count()) && name.chars().all(|c| c.is_alphanumeric() || matches!(c, '_' | ' ' | '.' | '-' | '\u{c0}'..='\u{24f}'))
}

/// `data:image/(png|jpeg|webp|svg\+xml|gif);base64,[A-Za-z0-9+/=]+`.
fn logo_ok(logo: &str) -> bool {
    let Some(rest) = logo.strip_prefix("data:image/") else { return false };
    let Some((kind, data)) = rest.split_once(";base64,") else { return false };
    ["png", "jpeg", "webp", "svg+xml", "gif"].contains(&kind) && !data.is_empty() && data.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'/' | b'='))
}

/// POST /admin/branding - the name in the sidebar and the logo (a data URL;
/// `-` removes it, empty leaves it).
pub async fn admin_branding(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/branding").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let name: String = crate::memory::py_strip(form.get("platform_name").map(String::as_str).unwrap_or("")).chars().take(60).collect();
    let logo = crate::memory::py_strip(form.get("platform_logo").map(String::as_str).unwrap_or("")).to_string();
    let bad = |m: &str| (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": m}))).into_response();
    if !name.is_empty() && !platform_name_ok(&name) {
        return bad("Nom invalide.");
    }
    // `-` removes the logo: it is not a data URL to check.
    if !logo.is_empty() && logo != "-" && !logo_ok(&logo) {
        return bad("Logo attendu en PNG, JPEG, WebP, SVG ou GIF.");
    }
    if logo.chars().count() > 2 * 1024 * 1024 {
        return bad("Logo trop lourd (2 Mo maximum).");
    }
    let pool = state.db.pool();
    let upsert = "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value";
    if !name.is_empty() {
        let _ = sqlx::query(upsert).bind("platform_name").bind(&name).execute(pool).await;
    }
    if logo == "-" {
        let _ = sqlx::query("DELETE FROM settings WHERE key='platform_logo'").execute(pool).await;
    } else if !logo.is_empty() {
        let _ = sqlx::query(upsert).bind("platform_logo").bind(&logo).execute(pool).await;
    }
    let logo_state = if logo == "-" { "retiré" } else if logo.is_empty() { "inchangé" } else { "posé" };
    let shown = if name.is_empty() { "(inchangé)" } else { name.as_str() };
    crate::sessions::log_audit(pool, &s.username, "branding.modifie", &format!("nom={shown} logo={logo_state}")).await;
    Json(json!({"ok": true})).into_response()
}

/// `backups.MAX_KEEP`.
const BACKUP_MAX_KEEP: i64 = 365;

/// POST /admin/storage - where new media files go, the backups' destination
/// and retention; only the fields present change. S3 cannot be chosen before
/// it is configured.
pub async fn admin_storage_save(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/storage").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let s3_ready = crate::s3::Config::load(&state.db).await.is_some();
    let pool = state.db.pool();
    let set = |k: &'static str, v: String| async move {
        let _ = sqlx::query("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(k).bind(v).execute(pool).await;
    };
    let unconfigured = "Configure d'abord le stockage S3 (Admin → Config).";
    let mut changes = Vec::new();
    if let Some(v) = form.get("media_storage") {
        if v != "local" && v != "s3" {
            return super::admin_writes::json_error("Emplacement inconnu.", StatusCode::BAD_REQUEST);
        }
        if v == "s3" && !s3_ready {
            return super::admin_writes::json_error(unconfigured, StatusCode::BAD_REQUEST);
        }
        set("media_storage", v.clone()).await;
        changes.push(format!("fichiers={v}"));
    }
    if let Some(v) = form.get("backup_destination") {
        if !["local", "s3", "both"].contains(&v.as_str()) {
            return super::admin_writes::json_error("Destination inconnue.", StatusCode::BAD_REQUEST);
        }
        if v != "local" && !s3_ready {
            return super::admin_writes::json_error(unconfigured, StatusCode::BAD_REQUEST);
        }
        set("backup_destination", v.clone()).await;
        changes.push(format!("sauvegardes={v}"));
    }
    for name in ["backup_keep_local", "backup_keep_s3"] {
        if let Some(v) = form.get(name) {
            let Some(n) = super::support_routes::py_int(&Value::String(v.clone())) else {
                return super::admin_writes::json_error("La rétention doit être un nombre de sauvegardes.", StatusCode::BAD_REQUEST);
            };
            if !(1..=BACKUP_MAX_KEEP).contains(&n) {
                return super::admin_writes::json_error(&format!("La rétention va de 1 à {BACKUP_MAX_KEEP} sauvegardes."), StatusCode::BAD_REQUEST);
            }
            set(name, n.to_string()).await;
            changes.push(format!("{name}={n}"));
        }
    }
    let detail = if changes.is_empty() { "(rien)".to_string() } else { changes.join(", ") };
    crate::sessions::log_audit(pool, &s.username, "stockage.modifie", &detail).await;
    Json(json!({"ok": true})).into_response()
}

fn sse(body: axum::body::Body) -> Response {
    (
        [
            (axum::http::header::CONTENT_TYPE, "text/event-stream; charset=utf-8"),
            (axum::http::header::CACHE_CONTROL, "no-cache"),
            (axum::http::HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        body,
    )
        .into_response()
}

/// GET /admin/runner/stream - the runner's log stream, relayed (the browser
/// can neither reach the runner nor send its token), routine access lines
/// left out. A runner that does not answer is one error frame, then [DONE].
pub async fn admin_runner_stream(State(state): State<AppState>, headers: HeaderMap) -> Response {
    // A fetch path (auth._API_FETCH_PATHS): signed out is a 401, not the login page.
    if let Err(r) = super::admin_routes::admin_guard(&state, &headers, "/api/admin/runner/stream").await {
        return r;
    }
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let upstream = match reqwest::Client::builder().connect_timeout(std::time::Duration::from_secs(5)).build() {
        Ok(c) => c.get(format!("{}/stream", crate::probes::runner_url())).bearer_auth(token).send().await.ok(),
        Err(_) => None,
    };
    let Some(mut up) = upstream.filter(|r| r.status().is_success()) else {
        return sse(axum::body::Body::from("data: {\"error\": \"runner unreachable\"}\n\ndata: [DONE]\n\n"));
    };
    static NOISE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let noise = NOISE.get_or_init(|| regex::Regex::new(r#""GET /(?:v1/models|metrics|health\S*|version|ping)\b"#).expect("static regex"));
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<axum::body::Bytes, std::io::Error>>(64);
    tokio::spawn(async move {
        let mut buf: Vec<u8> = Vec::new();
        while let Ok(Some(chunk)) = up.chunk().await {
            buf.extend_from_slice(&chunk);
            while let Some(i) = buf.windows(2).position(|w| w == b"\n\n") {
                let evt = String::from_utf8_lossy(&buf[..i]).into_owned();
                buf.drain(..i + 2);
                let data_line = evt.split('\n').find(|l| l.starts_with("data:")).unwrap_or("");
                if noise.is_match(data_line) {
                    continue;
                }
                if tx.send(Ok(axum::body::Bytes::from(format!("{evt}\n\n")))).await.is_err() {
                    return; // the browser left
                }
            }
        }
    });
    sse(axum::body::Body::from_stream(tokio_stream::wrappers::ReceiverStream::new(rx)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_logos() {
        assert!(platform_name_ok("Plateforme IA — x".replace('—', "-").as_str()));
        assert!(!platform_name_ok("a/b"));
        assert!(logo_ok("data:image/png;base64,iVBORw0KGgo="));
        assert!(logo_ok("data:image/svg+xml;base64,PHN2Zz4="));
        assert!(!logo_ok("data:image/bmp;base64,AAAA"));
        assert!(!logo_ok("data:image/png;base64,<script>"));
    }
}
