//! The playground's HTML preview (preview_routes.py): a generated page served
//! under its OWN sandboxed policy, so it can run its scripts with an opaque
//! origin — never the viewer's cookies or the API with their rights.
#![allow(unused_imports)]
use super::*;
use axum::http::Uri;

const PREVIEW_TTL_S: f64 = 3600.0;
/// Per-account cap (an account's burst only evicts its own previews).
const PREVIEW_MAX: i64 = 40;
/// Bound on the whole table (one page can weigh 400 kB).
const PREVIEW_MAX_TOTAL: i64 = 400;

/// The error spy grafted onto the page: reports its runtime errors to the playground.
const PREVIEW_REPORT: &str = r#"<script>
(function(){
  var sent = 0;
  // The sandbox itself raises errors that say NOTHING about the page: opaque
  // origin (so parent.document unreadable, localStorage forbidden) and blocked
  // external resources. Reporting them blamed correct code.
  var noise = /cross-origin|Blocked a frame|SecurityError|operation is insecure|localStorage|sessionStorage|indexedDB|Access is denied/i;
  function report(m){
    var s = String(m);
    if (noise.test(s)) return;
    if (sent++ > 3) return;                         // do not flood the parent
    try { parent.postMessage({ dgxPreviewError: s.slice(0, 400) }, '*'); } catch (e) {}
  }
  window.addEventListener('error', function(e){
    report(e && e.message ? e.message : 'resource failed to load');
  }, true);
  window.addEventListener('unhandledrejection', function(e){
    report((e && e.reason && e.reason.message) || 'unhandled promise rejection');
  });
})();
</script>"#;

/// The page's own policy: `sandbox` first (the security layer then leaves it alone).
const PREVIEW_CSP: &str = "sandbox allow-scripts allow-forms allow-modals allow-popups; \
default-src 'none'; script-src 'unsafe-inline'; \
img-src 'self' data: blob:; style-src 'unsafe-inline'; \
connect-src 'self'; font-src 'self' data:; \
form-action 'none'";

/// `_preview_with_report`: the spy as early as possible in the document.
pub(crate) fn with_report(html: &str) -> String {
    for tag in ["<head>", "<HEAD>", "<html>", "<HTML>"] {
        if let Some(i) = html.find(tag) {
            let at = i + tag.len();
            return format!("{}{PREVIEW_REPORT}{}", &html[..at], &html[at..]);
        }
    }
    format!("{PREVIEW_REPORT}{html}")
}

/// Python's `str(v)` for a JSON value.
fn py_str(v: &Value) -> String {
    match v {
        Value::Null => "None".to_string(),
        Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        Value::String(s) => s.clone(),
        Value::Number(n) => n.as_i64().map(|i| i.to_string()).unwrap_or_else(|| n.to_string()),
        other => conversations::py_dumps(other),
    }
}

fn now_epoch() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `_preview_purge` then the insert, in one transaction.
async fn store(pool: &sqlx::SqlitePool, username: &str, pid: &str, html: &str) -> Result<(), sqlx::Error> {
    let now = now_epoch();
    let mut tx = pool.begin().await?;
    sqlx::query("DELETE FROM previews WHERE created_at < ?").bind(now - PREVIEW_TTL_S).execute(&mut *tx).await?;
    sqlx::query(
        "DELETE FROM previews WHERE username = ? AND id NOT IN (SELECT id FROM previews WHERE username = ? ORDER BY created_at DESC LIMIT ?)",
    )
    .bind(username)
    .bind(username)
    .bind(PREVIEW_MAX)
    .execute(&mut *tx)
    .await?;
    sqlx::query("DELETE FROM previews WHERE id NOT IN (SELECT id FROM previews ORDER BY created_at DESC LIMIT ?)")
        .bind(PREVIEW_MAX_TOTAL)
        .execute(&mut *tx)
        .await?;
    sqlx::query("INSERT INTO previews (id, username, html, created_at) VALUES (?,?,?,?)")
        .bind(pid)
        .bind(username)
        .bind(html)
        .bind(now_epoch())
        .execute(&mut *tx)
        .await?;
    tx.commit().await
}

/// POST /playground/preview - store a generated page for 1 h, answer its id
pub async fn playground_preview_create(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    let html: String = data.get("html").map(py_str).unwrap_or_default().chars().take(conversations::MSG_MAX_CHARS).collect();
    if html.trim().is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "vide"}))).into_response();
    }
    let Some(pid) = preview_id() else { return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
    match store(state.db.pool(), &session.username, &pid, &html).await {
        Ok(()) => Json(json!({"ok": true, "id": pid})).into_response(),
        Err(_) => auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
}

/// `secrets.token_urlsafe(18)`.
fn preview_id() -> Option<String> {
    use base64::Engine;
    let mut bytes = [0u8; 18];
    getrandom::getrandom(&mut bytes).ok()?;
    Some(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes))
}

/// GET /playground/preview/:pid - one of the viewer's previews, sandboxed
pub async fn playground_preview_show(State(state): State<AppState>, uri: Uri, headers: HeaderMap, Path(pid): Path<String>) -> Response {
    if pid.contains('/') {
        return auth::html_status(StatusCode::NOT_FOUND);
    }
    let session = match page_viewer(&state, &headers, uri.path()).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let html: Option<Option<String>> = match sqlx::query_scalar("SELECT html FROM previews WHERE id = ? AND username = ?")
        .bind(&pid)
        .bind(&session.username)
        .fetch_optional(state.db.pool())
        .await
    {
        Ok(h) => h,
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    let Some(html) = html else { return auth::html_status(StatusCode::NOT_FOUND) };
    (
        [
            (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8"),
            (axum::http::header::CONTENT_SECURITY_POLICY, PREVIEW_CSP),
            (axum::http::header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
            (axum::http::header::CACHE_CONTROL, "no-store"),
        ],
        with_report(html.as_deref().unwrap_or("")),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_spy_goes_right_after_head_else_first() {
        assert!(with_report("<html><head><title>x</title>").starts_with("<html><head><script>"));
        assert!(with_report("<p>hi</p>").starts_with("<script>"));
        assert!(with_report("<HTML><p>").starts_with("<HTML><script>"));
    }

    #[test]
    fn the_policy_starts_with_sandbox() {
        assert!(PREVIEW_CSP.starts_with("sandbox allow-scripts"));
        assert!(PREVIEW_CSP.contains("default-src 'none'") && PREVIEW_CSP.contains("form-action 'none'"));
    }

    #[test]
    fn preview_ids_look_like_token_urlsafe_18() {
        let id = preview_id().unwrap();
        assert_eq!(id.len(), 24);
        assert!(id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'));
    }
}
