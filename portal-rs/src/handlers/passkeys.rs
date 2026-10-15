//! Passkeys (webauthn_routes.py): listing, adding (after the password), removing,
//! toggling the second factor, and the second factor of the sign-in. The
//! challenges are single use and short-lived, kept in pending_webauthn exactly
//! as Flask writes them, so either backend can finish the other's challenge.
use super::*;
use super::admin_accounts::{management, verify_password_locked};
use crate::webauthn as wa;
use sqlx::SqlitePool;

/// `config.WEBAUTHN_*`: origin (the public URL), RP ID (its host), RP name, UV.
pub fn settings() -> (String, String, String, bool) {
    let web = std::env::var("DGX_WEB").unwrap_or_else(|_| "https://example.org".into()).trim_end_matches('/').to_string();
    let origin = std::env::var("WEBAUTHN_ORIGIN").unwrap_or(web);
    let derived = origin.split_once("//").map_or(origin.as_str(), |(_, r)| r).split('/').next().unwrap_or("").split(':').next().unwrap_or("").to_string();
    let rp_id = std::env::var("WEBAUTHN_RP_ID").unwrap_or(derived);
    let rp_name = std::env::var("WEBAUTHN_RP_NAME").unwrap_or_else(|_| "DGX".into());
    let uv = std::env::var("WEBAUTHN_REQUIRE_UV").unwrap_or_else(|_| "0".into()) == "1";
    (origin, rp_id, rp_name, uv)
}

fn now_f() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn random(n: usize) -> Option<Vec<u8>> {
    let mut b = vec![0u8; n];
    getrandom::getrandom(&mut b).ok()?;
    Some(b)
}

/// `secrets.token_urlsafe(24)`.
fn nonce() -> Option<String> {
    random(24).map(|b| wa::b64url(&b))
}

/// `_stored_credentials`: the account's keys, newest first.
async fn stored(pool: &SqlitePool, username: &str) -> Vec<(i64, String, i64, Option<String>, Option<String>, Option<f64>)> {
    sqlx::query_as("SELECT id, credential_id, sign_count, transports, label, CAST(created_at AS REAL) FROM webauthn_credentials WHERE username=? ORDER BY created_at DESC")
        .bind(username)
        .fetch_all(pool)
        .await
        .unwrap_or_default()
}

/// `_set_enabled(username, enabled)`.
async fn set_enabled(pool: &SqlitePool, username: &str, on: bool) {
    let now = now_f();
    let _ = sqlx::query(
        "INSERT INTO user_security (username, enabled, created_at, updated_at) VALUES (?,?,?,?) ON CONFLICT(username) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
    )
    .bind(username)
    .bind(i64::from(on))
    .bind(now)
    .bind(now)
    .execute(pool)
    .await;
}

/// `_pending_insert`: one challenge in flight per account and kind.
async fn pending_insert(pool: &SqlitePool, nonce: &str, username: &str, kind: &str, challenge: &[u8], fullname: Option<&str>, is_admin: Option<i64>, source: Option<&str>) {
    let now = now_f();
    let _ = sqlx::query("DELETE FROM pending_webauthn WHERE username=? AND kind=?").bind(username).bind(kind).execute(pool).await;
    let _ = sqlx::query("INSERT INTO pending_webauthn (nonce, username, kind, challenge, fullname, is_admin, source, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)")
        .bind(nonce)
        .bind(username)
        .bind(kind)
        .bind(challenge.to_vec())
        .bind(fullname)
        .bind(is_admin)
        .bind(source)
        .bind(now)
        .bind(now + 300.0)
        .execute(pool)
        .await;
}

pub struct Pending {
    pub username: String,
    pub fullname: Option<String>,
    pub is_admin: Option<i64>,
    pub challenge: Vec<u8>,
}

/// `_pending_get(nonce, kind)`: the live challenge of that kind, or None.
async fn pending_get(pool: &SqlitePool, nonce: &str, kind: &str) -> Option<Pending> {
    if nonce.is_empty() {
        return None;
    }
    let row: Option<(String, String, Vec<u8>, Option<String>, Option<i64>, f64)> =
        sqlx::query_as("SELECT username, kind, challenge, fullname, is_admin, CAST(expires_at AS REAL) FROM pending_webauthn WHERE nonce=?")
            .bind(nonce)
            .fetch_optional(pool)
            .await
            .ok()
            .flatten();
    let (username, row_kind, challenge, fullname, is_admin, expires) = row?;
    if expires < now_f() {
        let _ = sqlx::query("DELETE FROM pending_webauthn WHERE nonce=?").bind(nonce).execute(pool).await;
        return None;
    }
    (row_kind == kind).then_some(Pending { username, fullname, is_admin, challenge })
}

async fn pending_clear(pool: &SqlitePool, nonce: &str) {
    if !nonce.is_empty() {
        let _ = sqlx::query("DELETE FROM pending_webauthn WHERE nonce=?").bind(nonce).execute(pool).await;
    }
}

/// The user-visible descriptor list: the stored ids, as the browser wants them.
fn descriptors(creds: &[(i64, String, i64, Option<String>, Option<String>, Option<f64>)]) -> Vec<Value> {
    creds.iter().map(|c| json!({"id": c.1, "type": "public-key"})).collect()
}

/// `start_registration(username)`: the options the browser signs.
async fn start_registration(state: &AppState, username: &str) -> Option<Value> {
    let pool = state.db.pool();
    let (_, rp_id, rp_name, uv) = settings();
    let challenge = random(64)?;
    let nonce = nonce()?;
    let creds = stored(pool, username).await;
    let user_id = {
        use sha2::{Digest, Sha256};
        wa::b64url(&Sha256::digest(username.as_bytes()))
    };
    let uv_req = if uv { "required" } else { "preferred" };
    pending_insert(pool, &nonce, username, "register", &challenge, None, None, None).await;
    Some(json!({
        "publicKey": {
            "rp": {"name": rp_name, "id": rp_id},
            "user": {"id": user_id, "name": username, "displayName": username},
            "challenge": wa::b64url(&challenge),
            "pubKeyCredParams": [{"type": "public-key", "alg": wa::EDDSA}, {"type": "public-key", "alg": wa::ES256}, {"type": "public-key", "alg": wa::RS256}],
            "timeout": 60000,
            "excludeCredentials": descriptors(&creds),
            "authenticatorSelection": {"residentKey": "preferred", "requireResidentKey": false, "userVerification": uv_req},
            "attestation": "none",
        },
        "nonce": nonce,
    }))
}

/// `start_login(username, fullname, is_admin, source)`: the second factor's challenge.
pub async fn start_login(state: &AppState, username: &str, fullname: &str, is_admin: bool, source: &str) -> Option<Value> {
    let pool = state.db.pool();
    let (_, rp_id, _, uv) = settings();
    let challenge = random(64)?;
    let nonce = nonce()?;
    let creds = stored(pool, username).await;
    let uv_req = if uv { "required" } else { "preferred" };
    pending_insert(pool, &nonce, username, "login", &challenge, Some(fullname), Some(i64::from(is_admin)), Some(source)).await;
    Some(json!({
        "publicKey": {
            "challenge": wa::b64url(&challenge),
            "timeout": 60000,
            "rpId": rp_id,
            "allowCredentials": descriptors(&creds),
            "userVerification": uv_req,
        },
        "nonce": nonce,
    }))
}

/// `finish_registration`: the signed key checked, stored for this account.
async fn finish_registration(state: &AppState, username: &str, cred: &Value, nonce: &str, label: &str) -> Result<(), (StatusCode, Value)> {
    let pool = state.db.pool();
    let err = |code: StatusCode, msg: &str| (code, json!({"error": msg}));
    let Some(pend) = pending_get(pool, nonce, "register").await else {
        return Err(err(StatusCode::BAD_REQUEST, "Demande d'enregistrement expirée ou invalide."));
    };
    if pend.username != username {
        return Err(err(StatusCode::BAD_REQUEST, "Compte incohérent."));
    }
    let (origin, rp_id, _, uv) = settings();
    let ver = match wa::verify_registration(cred, &pend.challenge, &rp_id, &origin, uv) {
        Ok(v) => v,
        Err(_) => {
            pending_clear(pool, nonce).await;
            return Err(err(StatusCode::BAD_REQUEST, "Clé refusée : la vérification a échoué."));
        }
    };
    let cred_id = wa::b64url(&ver.cred_id);
    let transports = cred.get("response").and_then(|r| r.get("transports")).and_then(Value::as_array).cloned().unwrap_or_default();
    // json.dumps of a list: ", " between the items.
    let transports = format!("[{}]", transports.iter().map(|t| serde_json::to_string(&t.as_str().map_or_else(|| t.to_string(), str::to_string)).unwrap_or_default()).collect::<Vec<_>>().join(", "));
    // Scoped to the account: a key id is no more than a hint from the client.
    let _ = sqlx::query("DELETE FROM webauthn_credentials WHERE username=? AND credential_id=?").bind(username).bind(&cred_id).execute(pool).await;
    let label = if label.is_empty() { "Clé de sécurité".to_string() } else { label.to_string() };
    let _ = sqlx::query("INSERT INTO webauthn_credentials (username, credential_id, public_key, sign_count, transports, label, created_at) VALUES (?,?,?,?,?,?,?)")
        .bind(username)
        .bind(&cred_id)
        .bind(&ver.cose)
        .bind(i64::from(ver.sign_count))
        .bind(transports)
        .bind(label)
        .bind(now_f())
        .execute(pool)
        .await;
    set_enabled(pool, username, true).await;
    pending_clear(pool, nonce).await;
    Ok(())
}

/// `finish_login`: the second factor's answer; the session opens on success.
async fn finish_login(state: &AppState, headers: &HeaderMap, connect: Option<std::net::SocketAddr>, nonce: &str, cred: &Value) -> Result<Response, (StatusCode, Value)> {
    let pool = state.db.pool();
    let err = |code: StatusCode, msg: &str| (code, json!({"error": msg}));
    let Some(pend) = pending_get(pool, nonce, "login").await else {
        return Err(err(StatusCode::BAD_REQUEST, "Demande de connexion expirée ou invalide."));
    };
    let username = pend.username.clone();
    let Some(id) = cred.get("id").and_then(Value::as_str).and_then(wa::unb64url) else {
        return Err(err(StatusCode::BAD_REQUEST, "Clé invalide."));
    };
    let cred_id = wa::b64url(&id);
    let row: Option<(Vec<u8>, i64)> = sqlx::query_as("SELECT public_key, sign_count FROM webauthn_credentials WHERE username=? AND credential_id=?")
        .bind(&username)
        .bind(&cred_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
    let Some((public_key, stored_count)) = row else {
        pending_clear(pool, nonce).await;
        return Err(err(StatusCode::UNAUTHORIZED, "Clé inconnue pour ce compte."));
    };
    let (origin, rp_id, _, uv) = settings();
    let new_count = match wa::verify_assertion(cred, &pend.challenge, &rp_id, &origin, &public_key, stored_count, uv) {
        Ok(c) => c,
        Err(_) => {
            pending_clear(pool, nonce).await;
            return Err(err(StatusCode::UNAUTHORIZED, "Vérification de la clé échouée."));
        }
    };
    // A block placed during the challenge still refuses: the key proves identity, not access.
    if sqlx::query_scalar::<_, i64>("SELECT 1 FROM blocked_users WHERE username=?").bind(&username).fetch_optional(pool).await.ok().flatten().is_some() {
        pending_clear(pool, nonce).await;
        crate::sessions::log_audit(pool, &username, "login.refuse", "compte bloqué pendant le défi passkey").await;
        return Err(err(StatusCode::FORBIDDEN, "Accès révoqué pour ce compte."));
    }
    let _ = sqlx::query("UPDATE webauthn_credentials SET sign_count=? WHERE username=? AND credential_id=?")
        .bind(i64::from(new_count))
        .bind(&username)
        .bind(&cred_id)
        .execute(pool)
        .await;
    pending_clear(pool, nonce).await;
    let fullname = pend.fullname.filter(|f| !f.is_empty()).unwrap_or_else(|| username.clone());
    let ip = crate::auth::client_ip(headers, connect.map(|c| c.ip()));
    let token = match crate::login::apply_session(state, headers, &ip, &username, &fullname, pend.is_admin.unwrap_or(0) != 0, false).await {
        Ok(t) => t,
        Err(r) => return Ok(r),
    };
    Ok(([(axum::http::header::SET_COOKIE, crate::session_store::set_cookie(&token))], Json(json!({"ok": true}))).into_response())
}

fn bad(code: StatusCode, msg: &str) -> Response {
    (code, Json(json!({"error": msg}))).into_response()
}

fn answer(r: Result<Value, (StatusCode, Value)>) -> Response {
    match r {
        Ok(v) => Json(v).into_response(),
        Err((code, v)) => (code, Json(v)).into_response(),
    }
}

/// Session and CSRF for a JSON POST; the session's username.
async fn signed_in(state: &AppState, headers: &HeaderMap) -> Result<auth::SessionData, Response> {
    match auth::csrf_ok(headers, None, &state.db).await {
        Err(e) => return Err(e.into_response()),
        Ok(false) => return Err(auth::csrf_refused()),
        Ok(true) => {}
    }
    auth::require_session(headers, &state.db).await.map_err(|e| e.into_response())
}

/// `request.get_json(silent=True) or {}`: a JSON object, or nothing.
fn json_body(headers: &HeaderMap, body: &axum::body::Bytes) -> serde_json::Map<String, Value> {
    super::memory_routes::json_object(headers, body)
}

fn text(v: Option<&Value>) -> String {
    v.and_then(Value::as_str).unwrap_or("").to_string()
}

/// POST /api/security/register/begin - the password first, then the options.
pub async fn register_begin(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let s = match signed_in(&state, &headers).await {
        Ok(s) => s,
        Err(r) => return r,
    };
    let data = json_body(&headers, &body);
    let password = text(data.get("password"));
    if password.is_empty() {
        return bad(StatusCode::BAD_REQUEST, "Mot de passe requis pour ajouter une clé.");
    }
    let pool = state.db.pool();
    let (how, _) = management(pool, &s.username).await;
    if let Err(r) = verify_password_locked(pool, &s.username, how, &password).await {
        return r;
    }
    match start_registration(&state, &s.username).await {
        Some(v) => Json(v).into_response(),
        None => auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
}

/// POST /api/security/register/finish - the signed key, with the challenge's nonce.
pub async fn register_finish(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let s = match signed_in(&state, &headers).await {
        Ok(s) => s,
        Err(r) => return r,
    };
    let data = json_body(&headers, &body);
    let cred = data.get("credential").filter(|v| crate::keys::truthy(v)).cloned();
    let nonce = text(data.get("nonce"));
    let label = text(data.get("label"));
    let (Some(cred), false) = (cred, nonce.is_empty()) else {
        return bad(StatusCode::BAD_REQUEST, "Réponse de clé manquante.");
    };
    answer(finish_registration(&state, &s.username, &cred, &nonce, &label).await.map(|_| json!({"ok": true})))
}

/// POST /api/security/remove - one key, on the password; no key left, no second factor.
pub async fn remove(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let s = match signed_in(&state, &headers).await {
        Ok(s) => s,
        Err(r) => return r,
    };
    let data = json_body(&headers, &body);
    let cred_id = text(data.get("credential_id"));
    let password = text(data.get("password"));
    if cred_id.is_empty() || password.is_empty() {
        return bad(StatusCode::BAD_REQUEST, "Champs manquants.");
    }
    let pool = state.db.pool();
    let (how, _) = management(pool, &s.username).await;
    if let Err(r) = verify_password_locked(pool, &s.username, how, &password).await {
        return r;
    }
    let _ = sqlx::query("DELETE FROM webauthn_credentials WHERE username=? AND credential_id=?").bind(&s.username).bind(&cred_id).execute(pool).await;
    let remaining: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM webauthn_credentials WHERE username=?").bind(&s.username).fetch_one(pool).await.unwrap_or(0);
    if remaining == 0 {
        let _ = sqlx::query("UPDATE user_security SET enabled=0, updated_at=? WHERE username=?").bind(now_f()).bind(&s.username).execute(pool).await;
    }
    Json(json!({"ok": true})).into_response()
}

/// POST /api/security/toggle - the second factor on or off, on the password.
pub async fn toggle(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let s = match signed_in(&state, &headers).await {
        Ok(s) => s,
        Err(r) => return r,
    };
    let data = json_body(&headers, &body);
    let on = data.get("enabled").is_some_and(crate::keys::truthy);
    let password = text(data.get("password"));
    if password.is_empty() {
        return bad(StatusCode::BAD_REQUEST, "Mot de passe requis.");
    }
    let pool = state.db.pool();
    let (how, _) = management(pool, &s.username).await;
    if let Err(r) = verify_password_locked(pool, &s.username, how, &password).await {
        return r;
    }
    if on && stored(pool, &s.username).await.is_empty() {
        return bad(StatusCode::BAD_REQUEST, "Enregistre d'abord une clé de sécurité.");
    }
    set_enabled(pool, &s.username, on).await;
    Json(json!({"ok": true})).into_response()
}

/// POST /api/security/verify-login - the second factor of the sign-in (no session yet).
pub async fn verify_login(State(state): State<AppState>, connect: Option<axum::extract::ConnectInfo<std::net::SocketAddr>>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    match auth::csrf_ok(&headers, None, &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::csrf_refused(),
        Ok(true) => {}
    }
    let data = json_body(&headers, &body);
    let nonce = text(data.get("nonce"));
    let cred = data.get("credential").filter(|v| crate::keys::truthy(v)).cloned();
    let (Some(cred), false) = (cred, nonce.is_empty()) else {
        return bad(StatusCode::BAD_REQUEST, "Réponse de clé manquante.");
    };
    match finish_login(&state, &headers, connect.map(|c| c.0), &nonce, &cred).await {
        Ok(r) => r,
        Err((code, v)) => (code, Json(v)).into_response(),
    }
}
