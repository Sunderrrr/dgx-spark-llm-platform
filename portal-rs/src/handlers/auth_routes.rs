//! Session routes that need no credential check (auth.py / app.py): the CSRF
//! token and the logout. The login itself stays on Flask (LDAP, SSO, WebAuthn).
#![allow(unused_imports)]
use super::*;
use crate::session_store;

/// GET /api/csrf - the session's CSRF token, created (with its session) on first ask
pub async fn api_csrf(State(state): State<AppState>, headers: HeaderMap) -> Response {
    // A legacy signed cookie (contains '.') is not read here: it opens a new session.
    let raw = auth::get_session_token(&headers).filter(|t| !t.is_empty() && !t.contains('.'));
    let existing = match &raw {
        Some(t) => match state.db.get_session(t).await {
            Ok(d) => d.and_then(|v| v.as_object().cloned()),
            Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
        },
        None => None,
    };
    if let Some(data) = &existing {
        if let Some(token) = data.get("csrf") {
            // Untouched session: no write, no cookie (Flask's lazy creation).
            return Json(json!({"token": token})).into_response();
        }
    }
    let (token, mut data) = match (raw, existing) {
        (Some(t), Some(d)) => (t, d),
        _ => match session_store::new_token() {
            Some(t) => (t, serde_json::Map::new()),
            None => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
        },
    };
    let Some(csrf) = session_store::new_token() else { return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
    data.insert("csrf".into(), json!(csrf));
    if session_store::save(state.db.pool(), &token, &data).await.is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    ([(axum::http::header::SET_COOKIE, session_store::set_cookie(&token))], Json(json!({"token": csrf}))).into_response()
}

/// werkzeug's `redirect(location)`: 302 and its HTML body.
pub(crate) fn redirect_to(location: &str) -> Response {
    let escaped = location.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&#34;").replace('\'', "&#39;");
    let body = format!(
        "<!doctype html>\n<html lang=en>\n<title>Redirecting...</title>\n<h1>Redirecting...</h1>\n<p>You should be redirected automatically to the target URL: <a href=\"{escaped}\">{escaped}</a>. If not, click the link.\n"
    );
    (StatusCode::FOUND, [(axum::http::header::LOCATION, location.to_string()), (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8".to_string())], body).into_response()
}

/// POST /logout - revoke the session server-side, drop it, back to the login page
/// (or to the identity provider's end-session after an SSO login)
pub async fn logout(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let form = form_of(&headers, &body);
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::csrf_refused(),
        Ok(true) => {}
    }
    // csrf_ok held, so the cookie names a live session.
    let Some(token) = auth::get_session_token(&headers) else { return auth::csrf_refused() };
    let data = state.db.get_session(&token).await.ok().flatten().unwrap_or(Value::Null);
    if let Some(sid) = data.get("sid").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        if sqlx::query("UPDATE user_sessions SET revoked=1 WHERE sid=?").bind(sid).execute(state.db.pool()).await.is_err() {
            return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
        }
    }
    if session_store::delete(state.db.pool(), &token).await.is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let was_sso = data.get("sso").is_some_and(keys::truthy);
    let logout_url = if was_sso { state.db.get_config("oidc_logout_url").await.unwrap_or_default() } else { String::new() };
    let mut resp = redirect_to(if logout_url.is_empty() { "/login" } else { &logout_url });
    if let Ok(v) = axum::http::HeaderValue::from_str(&session_store::delete_cookie()) {
        resp.headers_mut().insert(axum::http::header::SET_COOKIE, v);
    }
    resp
}
