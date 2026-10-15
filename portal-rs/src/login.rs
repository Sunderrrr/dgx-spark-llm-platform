//! Signing a LOCAL account in (app.py `login`, local_users.py): the werkzeug
//! hash checked here, the lockout counters shared with Flask, the session
//! opened in the shared store. Every other case — directory accounts, a
//! failed local check, an account with a passkey — is handed to Flask as it
//! came, so LDAP and WebAuthn stay where they are verified.
use hmac::Hmac;
use sha2::Sha256;

/// werkzeug `check_password_hash` for the `scrypt:` and `pbkdf2:` methods.
pub fn check_password_hash(stored: &str, password: &str) -> bool {
    let mut parts = stored.splitn(3, '$');
    let (Some(method), Some(salt), Some(hash)) = (parts.next(), parts.next(), parts.next()) else { return false };
    let m: Vec<&str> = method.split(':').collect();
    let derived: Option<Vec<u8>> = match m.as_slice() {
        ["scrypt", n, r, p] => (|| {
            let n: u64 = n.parse().ok()?;
            if !n.is_power_of_two() {
                return None;
            }
            let params = scrypt::Params::new(n.trailing_zeros() as u8, r.parse().ok()?, p.parse().ok()?, 64).ok()?;
            let mut out = vec![0u8; 64];
            scrypt::scrypt(password.as_bytes(), salt.as_bytes(), &params, &mut out).ok()?;
            Some(out)
        })(),
        ["pbkdf2", "sha256", iterations] => (|| {
            let it: u32 = iterations.parse().ok()?;
            let mut out = vec![0u8; 32];
            pbkdf2::pbkdf2::<Hmac<Sha256>>(password.as_bytes(), salt.as_bytes(), it, &mut out).ok()?;
            Some(out)
        })(),
        _ => None,
    };
    derived.is_some_and(|d| crate::auth::constant_time_eq(hex::encode(d).as_bytes(), hash.as_bytes()))
}

/// The throwaway hash checked for an unknown account: the same KDF cost, no
/// enumeration oracle in the response time.
pub const DUMMY_HASH: &str = "scrypt:32768:8:1$bTh6vRk0aQ2c9LxW$2c1f8d2a3b4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f";

/// `USERNAME_RE`.
pub fn valid_username(u: &str) -> bool {
    (1..=64).contains(&u.len()) && u.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// `_safe_next`: only a local relative path, else the home page.
pub fn safe_next(target: Option<&str>) -> String {
    let Some(t) = target.filter(|t| !t.is_empty()) else { return "/".into() };
    if t.contains(['\\', '\t', '\n']) || !t.starts_with('/') || t.starts_with("//") || t.contains("://") {
        return "/".into();
    }
    let (scheme, host, _) = crate::skillstore::url_parts(t);
    if !scheme.is_empty() || !host.is_empty() {
        return "/".into();
    }
    t.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Hashes made by werkzeug's generate_password_hash for « correct horse ».
    #[test]
    fn werkzeug_hashes() {
        let fx: serde_json::Value = serde_json::from_str(include_str!("../tests/fixtures/werkzeug_hashes.json")).unwrap();
        for h in fx["hashes"].as_array().unwrap() {
            let h = h.as_str().unwrap();
            assert!(check_password_hash(h, "correct horse"), "{h}");
            assert!(!check_password_hash(h, "correct horsE"), "{h}");
        }
        assert!(!check_password_hash("plain", "plain"));
        assert!(!check_password_hash(DUMMY_HASH, "mot-de-passe-factice"));
    }

    #[test]
    fn redirect_targets() {
        assert_eq!(safe_next(Some("/playground?x=1")), "/playground?x=1");
        for bad in ["//evil.com", "/\\evil.com", "https://evil.com", "evil", "/a\tb"] {
            assert_eq!(safe_next(Some(bad)), "/", "{bad}");
        }
        assert_eq!(safe_next(None), "/");
    }
}

use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};

use crate::AppState;

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `_login_locked`: seconds of lock left on a key.
pub async fn locked(pool: &sqlx::SqlitePool, key: &str) -> i64 {
    let until: Option<Option<f64>> = sqlx::query_scalar("SELECT locked_until FROM login_attempts WHERE key=?").bind(key).fetch_optional(pool).await.ok().flatten();
    match until.flatten() {
        Some(u) if u != 0.0 => ((u - now()) as i64).max(0),
        _ => 0,
    }
}

/// `_record_user_source`: the cumulative sign-in sources of an account.
async fn record_source(pool: &sqlx::SqlitePool, username: &str, source: &str, fullname: &str, is_admin: bool) {
    let row: Option<Option<String>> = sqlx::query_scalar("SELECT sources FROM user_sources WHERE username=?").bind(username).fetch_optional(pool).await.ok().flatten();
    let mut srcs: std::collections::BTreeSet<String> = row.flatten().unwrap_or_default().split(',').filter(|s| !s.is_empty()).map(str::to_string).collect();
    srcs.insert(source.to_string());
    let _ = sqlx::query(
        "INSERT INTO user_sources (username, sources, fullname, last_source, last_seen, last_is_admin) VALUES (?,?,?,?,?,?) \
         ON CONFLICT(username) DO UPDATE SET sources=excluded.sources, fullname=COALESCE(excluded.fullname, user_sources.fullname), \
         last_source=excluded.last_source, last_seen=excluded.last_seen, last_is_admin=excluded.last_is_admin",
    )
    .bind(username).bind(srcs.into_iter().collect::<Vec<_>>().join(",")).bind(fullname).bind(source)
    .bind(crate::keys::iso(&chrono::Local::now().naive_local())).bind(i64::from(is_admin))
    .execute(pool).await;
}

/// POST /login - a local account's sign-in; anything else is Flask's.
pub async fn login(state: AppState, headers: HeaderMap, connect: Option<std::net::SocketAddr>, uri: axum::http::Uri, body: axum::body::Bytes) -> Response {
    let form = crate::handlers::form_of(&headers, &body);
    match crate::auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return crate::auth::csrf_refused(),
        Ok(true) => {}
    }
    let relay = |headers: HeaderMap, body: axum::body::Bytes, uri: axum::http::Uri| async move {
        let mut req = axum::http::Request::builder().method("POST").uri(uri);
        for (k, v) in &headers {
            req = req.header(k, v);
        }
        crate::relay::to_flask(req.body(axum::body::Body::from(body)).unwrap_or_default()).await
    };
    if crate::auth::require_session(&headers, &state.db).await.is_ok() {
        return (StatusCode::FOUND, [(axum::http::header::LOCATION, "/")], crate::auth::werkzeug_redirect_body("/")).into_response();
    }
    let username = crate::memory::py_strip(form.get("username").map_or("", String::as_str)).to_lowercase();
    let password = form.get("password").cloned().unwrap_or_default();
    if !valid_username(&username) {
        return (StatusCode::UNAUTHORIZED, [(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], "").into_response();
    }
    let pool = state.db.pool();
    let ip = crate::auth::client_ip(&headers, connect.map(|c| c.ip()));
    let (key, ukey) = (format!("{ip}|{username}"), format!("user:{username}"));
    if locked(pool, &key).await > 0 || locked(pool, &ukey).await > 0 {
        return (StatusCode::UNAUTHORIZED, [(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], "").into_response();
    }
    let row: Option<(String, Option<String>)> = sqlx::query_as("SELECT password_hash, fullname FROM local_users WHERE username=? AND enabled=1").bind(&username).fetch_optional(pool).await.ok().flatten();
    let hash = row.as_ref().map_or(DUMMY_HASH.to_string(), |r| r.0.clone());
    let pw = password.clone();
    let ok = tokio::task::spawn_blocking(move || check_password_hash(&hash, &pw)).await.unwrap_or(false);
    let Some((_, fullname)) = row.filter(|_| ok) else {
        // Not a local account, or not its password: Flask tries the directory and counts the failure.
        return relay(headers, body, uri).await;
    };
    let facts = match state.db.get_account_facts(&username).await {
        Ok(f) => f,
        Err(_) => return crate::auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    let passkey: Option<Option<i64>> = sqlx::query_scalar("SELECT enabled FROM user_security WHERE username=?").bind(&username).fetch_optional(pool).await.ok().flatten();
    if facts.blocked || passkey.flatten().unwrap_or(0) != 0 {
        // The refusal (and its audit line) and the second factor are Flask's.
        return relay(headers, body, uri).await;
    }
    let is_admin = facts.local.is_some_and(|l| l.is_admin);
    let fullname = fullname.filter(|f| !f.is_empty()).unwrap_or_else(|| username.clone());
    for k in [&key, &ukey] {
        let _ = sqlx::query("DELETE FROM login_attempts WHERE key=?").bind(k).execute(pool).await;
    }
    record_source(pool, &username, "local", &fullname, is_admin).await;
    // `_apply_session`: a NEW session token, the client's CSRF token kept.
    let old = crate::auth::get_session_token(&headers).filter(|t| !t.contains('.'));
    let csrf = match &old {
        Some(t) => state.db.get_session(t).await.ok().flatten().and_then(|d| d.get("csrf").and_then(serde_json::Value::as_str).map(str::to_string)),
        None => None,
    };
    if let Some(t) = &old {
        let _ = crate::session_store::delete(pool, t).await;
    }
    let (Some(token), Some(sid)) = (crate::session_store::new_token(), crate::session_store::new_token()) else {
        return crate::auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    };
    let csrf = match csrf.or_else(crate::session_store::new_token) {
        Some(c) => c,
        None => return crate::auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    let auth_at = now() as i64;
    let max_age: i64 = std::env::var("SESSION_MAX_AGE").ok().and_then(|v| v.parse().ok()).unwrap_or(12 * 3600);
    let ua: String = headers.get(axum::http::header::USER_AGENT).and_then(|v| v.to_str().ok()).unwrap_or("").chars().take(400).collect();
    if sqlx::query("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, created_at, ip, user_agent) VALUES (?, ?, ?, ?, 0, ?, ?, ?)")
        .bind(&sid).bind(&username).bind(auth_at).bind(auth_at + max_age).bind(now()).bind(&ip).bind(&ua)
        .execute(pool).await.is_err()
    {
        return crate::auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let mut data = serde_json::Map::new();
    data.insert("csrf".into(), serde_json::json!(csrf));
    data.insert("username".into(), serde_json::json!(username));
    data.insert("fullname".into(), serde_json::json!(fullname));
    data.insert("is_admin".into(), serde_json::json!(is_admin));
    data.insert("sso".into(), serde_json::json!(false));
    data.insert("auth_at".into(), serde_json::json!(auth_at));
    data.insert("sid".into(), serde_json::json!(sid));
    if crate::session_store::save(pool, &token, &data).await.is_err() {
        return crate::auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let next = safe_next(uri.query().and_then(|q| serde_urlencoded::from_str::<Vec<(String, String)>>(q).ok()).and_then(|v| v.into_iter().find(|(k, _)| k == "next").map(|(_, v)| v)).as_deref());
    (
        StatusCode::FOUND,
        [
            (axum::http::header::LOCATION, next.clone()),
            (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8".into()),
            (axum::http::header::SET_COOKIE, crate::session_store::set_cookie(&token)),
        ],
        crate::auth::werkzeug_redirect_body(&next),
    )
        .into_response()
}

/// `LOGIN_MAX_FAILS` / `LOGIN_WINDOW` / `LOGIN_LOCK`.
const LOGIN_MAX_FAILS: i64 = 6;
const LOGIN_WINDOW: f64 = 900.0;
const LOGIN_LOCK: f64 = 900.0;

/// `_login_fail(key)`: one more failure in the window; the sixth locks the
/// key for 15 min, and the lock is audited.
pub async fn fail(pool: &sqlx::SqlitePool, key: &str) {
    let t = now();
    let row: Option<(i64, f64)> = sqlx::query_as("SELECT fails, CAST(first_at AS REAL) FROM login_attempts WHERE key=?").bind(key).fetch_optional(pool).await.ok().flatten();
    let (fails, first_at) = match row {
        Some((f, first)) if t - first <= LOGIN_WINDOW => (f + 1, first),
        _ => (1, t),
    };
    let locked_until = if fails >= LOGIN_MAX_FAILS { t + LOGIN_LOCK } else { 0.0 };
    if locked_until > 0.0 {
        let who = key.rsplit('|').next().unwrap_or(key);
        crate::sessions::log_audit(pool, who, "login.verrouillage", &format!("{key} — {fails} échecs, verrouillé {} min", (LOGIN_LOCK / 60.0) as i64)).await;
    }
    let _ = sqlx::query(
        "INSERT INTO login_attempts (key, fails, first_at, locked_until) VALUES (?,?,?,?) \
         ON CONFLICT(key) DO UPDATE SET fails=excluded.fails, first_at=excluded.first_at, locked_until=excluded.locked_until",
    )
    .bind(key)
    .bind(fails)
    .bind(first_at)
    .bind(locked_until)
    .execute(pool)
    .await;
}

/// `_login_reset(key)`.
pub async fn reset(pool: &sqlx::SqlitePool, key: &str) {
    let _ = sqlx::query("DELETE FROM login_attempts WHERE key=?").bind(key).execute(pool).await;
}
