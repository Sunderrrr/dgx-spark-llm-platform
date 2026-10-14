//! Session guard, parity with dgx-portal/auth.py `login_required`.
//!
//! A session holds only if, at THIS request:
//!   - it carries a username (no name = not signed in, never a default user);
//!   - it is younger than SESSION_MAX_AGE since `auth_at` (no `auth_at` =
//!     expired, not eternal);
//!   - its `sid`, when it has one, is in the registry, not revoked, not past
//!     its expiry (a session without sid predates the registry: age only);
//!   - the account is still valid: not blocked, not a disabled local account.
//! The role is re-read too: a local account's role is the portal's (own flag
//! or group); a directory account keeps the role recorded at its last login,
//! the owner is always an admin, and missing data never demotes anyone.
//!
//! Not ported: Flask also clears the cookie and writes a `session.invalidee`
//! audit line when it drops a session. Here the request is refused only.
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};

use crate::db::{AccountFacts, Database};

/// A signed-in user, with the role re-read at this request.
#[derive(Debug, Clone)]
pub struct SessionData {
    pub username: String,
    pub fullname: Option<String>,
    pub is_admin: bool,
    pub csrf: Option<String>,
    /// The session's registry identifier (its own sessions list marks it as current).
    pub sid: Option<String>,
}

#[derive(Debug)]
pub enum AuthError {
    /// Not signed in, expired, revoked or invalid account: 401.
    Unauthorized,
    /// The portal database could not be read: 503, never a forced logout.
    Unavailable,
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        match self {
            AuthError::Unauthorized => html_status(StatusCode::UNAUTHORIZED),
            AuthError::Unavailable => (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({"ok": false, "error": "Base du portail illisible."})),
            )
                .into_response(),
        }
    }
}

/// A bare error status typed like Flask's `abort()` pages (text/html): the
/// clients read only the status, the type keeps both backends alike.
pub fn html_status(status: StatusCode) -> Response {
    (status, [(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], "").into_response()
}

/// Absolute session lifetime in seconds (SESSION_MAX_AGE, default 12 h).
fn max_age() -> f64 {
    std::env::var("SESSION_MAX_AGE")
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(12.0 * 3600.0)
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// The `session` cookie's value.
pub fn get_session_token(headers: &HeaderMap) -> Option<String> {
    headers
        .get_all("cookie")
        .iter()
        .filter_map(|h| h.to_str().ok())
        .flat_map(|c| c.split(';'))
        .find_map(|c| c.trim().strip_prefix("session=").map(str::to_string))
        .filter(|v| !v.is_empty())
}

/// Too old since `auth_at` (missing = expired)? Pure.
pub fn too_old(auth_at: Option<f64>, now: f64, max_age: f64) -> bool {
    now - auth_at.unwrap_or(0.0) > max_age
}

/// Registry verdict for a session with a sid. Pure.
pub fn registry_holds(row: Option<(bool, f64)>, now: f64) -> bool {
    matches!(row, Some((false, expires_at)) if expires_at >= now)
}

/// auth.account_state: Some(role) when the account holds (None role = no
/// opinion, keep the session's), None when the session must be dropped. Pure.
pub fn account_state(facts: &AccountFacts) -> Option<Option<bool>> {
    if facts.blocked {
        return None;
    }
    if let Some(local) = facts.local {
        return if local.enabled { Some(Some(local.is_admin)) } else { None };
    }
    if facts.is_owner {
        return Some(Some(true));
    }
    Some(facts.last_is_admin)
}

/// The CSRF check every POST/PUT/PATCH/DELETE passes BEFORE login_required
/// (app.before_request `_csrf_protect`): the token from the `csrf_token` form
/// field or the `X-CSRFToken` header must equal the session's, compared in
/// constant time. No session, no token: refused (400) as in Flask.
pub async fn csrf_ok(headers: &HeaderMap, form_token: Option<&str>, db: &Database) -> Result<bool, AuthError> {
    let sent = form_token
        .filter(|t| !t.is_empty())
        .map(str::to_string)
        .or_else(|| headers.get("x-csrftoken").and_then(|v| v.to_str().ok()).map(str::to_string))
        .unwrap_or_default();
    let Some(token) = get_session_token(headers) else { return Ok(false) };
    let data = db.get_session(&token).await.map_err(|_| AuthError::Unavailable)?;
    let expected = data
        .as_ref()
        .and_then(|d| d.get("csrf"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    Ok(!expected.is_empty() && constant_time_eq(expected.as_bytes(), sent.as_bytes()))
}

/// hmac.compare_digest on bytes: no early exit on the first difference.
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Require a valid session (login_required).
pub async fn require_session(headers: &HeaderMap, db: &Database) -> Result<SessionData, AuthError> {
    let token = get_session_token(headers).ok_or(AuthError::Unauthorized)?;
    let data: Value = db
        .get_session(&token)
        .await
        .map_err(|_| AuthError::Unavailable)?
        .ok_or(AuthError::Unauthorized)?;

    let username = data
        .get("username")
        .and_then(Value::as_str)
        .filter(|u| !u.is_empty())
        .ok_or(AuthError::Unauthorized)?
        .to_string();
    let t = now();
    if too_old(data.get("auth_at").and_then(Value::as_f64), t, max_age()) {
        return Err(AuthError::Unauthorized);
    }
    if let Some(sid) = data.get("sid").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        let row = db.get_session_registry(sid).await.map_err(|_| AuthError::Unavailable)?;
        if !registry_holds(row, t) {
            return Err(AuthError::Unauthorized);
        }
    }
    let facts = db.get_account_facts(&username).await.map_err(|_| AuthError::Unavailable)?;
    let role = account_state(&facts).ok_or(AuthError::Unauthorized)?;
    let session_admin = data.get("is_admin").and_then(Value::as_bool).unwrap_or(false);

    Ok(SessionData {
        username,
        fullname: data.get("fullname").and_then(Value::as_str).map(str::to_string),
        is_admin: role.unwrap_or(session_admin),
        csrf: data.get("csrf").and_then(Value::as_str).map(str::to_string),
        sid: data.get("sid").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::LocalAccount;

    #[test]
    fn age_is_counted_from_auth_at_and_missing_means_expired() {
        assert!(!too_old(Some(1000.0), 1000.0 + 3600.0, 43200.0));
        assert!(too_old(Some(1000.0), 1000.0 + 43201.0, 43200.0));
        assert!(too_old(None, 1_800_000_000.0, 43200.0));
    }

    #[test]
    fn registry_refuses_missing_revoked_or_expired() {
        assert!(registry_holds(Some((false, 200.0)), 100.0));
        assert!(!registry_holds(None, 100.0));
        assert!(!registry_holds(Some((true, 200.0)), 100.0));
        assert!(!registry_holds(Some((false, 50.0)), 100.0));
    }

    #[test]
    fn blocked_wins_over_everything() {
        let f = AccountFacts { blocked: true, is_owner: true, ..Default::default() };
        assert_eq!(account_state(&f), None);
    }

    #[test]
    fn a_disabled_local_account_is_dropped_and_an_enabled_one_has_the_portal_role() {
        let off = AccountFacts { local: Some(LocalAccount { enabled: false, is_admin: true }), ..Default::default() };
        assert_eq!(account_state(&off), None);
        let on = AccountFacts { local: Some(LocalAccount { enabled: true, is_admin: false }), ..Default::default() };
        assert_eq!(account_state(&on), Some(Some(false)));
    }

    #[test]
    fn the_owner_is_always_admin() {
        let f = AccountFacts { is_owner: true, last_is_admin: Some(false), ..Default::default() };
        assert_eq!(account_state(&f), Some(Some(true)));
    }

    #[test]
    fn missing_data_never_demotes_a_directory_account() {
        assert_eq!(account_state(&AccountFacts::default()), Some(None));
        let f = AccountFacts { last_is_admin: Some(true), ..Default::default() };
        assert_eq!(account_state(&f), Some(Some(true)));
    }

    #[test]
    fn csrf_comparison_is_exact() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"abc", b"ab"));
        assert!(!constant_time_eq(b"", b"x"));
    }

    #[test]
    fn client_ip_prefers_a_valid_cloudflare_header_then_the_last_forwarded_hop() {
        use axum::http::HeaderValue;
        let mut h = HeaderMap::new();
        h.insert("cf-connecting-ip", HeaderValue::from_static("203.0.113.9"));
        assert_eq!(client_ip(&h, None), "203.0.113.9");
        let mut h = HeaderMap::new();
        h.insert("cf-connecting-ip", HeaderValue::from_static("not-an-ip"));
        h.insert("x-forwarded-for", HeaderValue::from_static("fake, 198.51.100.7"));
        assert_eq!(client_ip(&h, None), "198.51.100.7");
        let h = HeaderMap::new();
        assert_eq!(client_ip(&h, None), "unknown");
    }
}

/// `_client_ip`: the visitor's real address. A Cloudflare header counts only
/// when it is a valid IP; of X-Forwarded-For only the LAST hop is trusted (the
/// first is client-controlled). Falls back on the connection's own address.
pub fn client_ip(headers: &HeaderMap, remote: Option<std::net::IpAddr>) -> String {
    let valid = |v: &str| v.parse::<std::net::IpAddr>().ok().map(|ip| ip.to_string());
    if let Some(ip) = headers.get("cf-connecting-ip").and_then(|v| v.to_str().ok()).map(str::trim).and_then(valid) {
        return ip;
    }
    let last_hop = headers
        .get("x-forwarded-for")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.split(',').last())
        .map(str::trim)
        .unwrap_or_default();
    if let Some(ip) = valid(last_hop) {
        return ip;
    }
    remote.map(|ip| ip.to_string()).unwrap_or_else(|| "unknown".to_string())
}
