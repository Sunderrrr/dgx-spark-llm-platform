//! Writing server-side sessions, parity with dgx-portal session_store.py: the
//! cookie is an opaque token (`secrets.token_urlsafe(32)`), the row is keyed by
//! its SHA-256 and holds the data as Flask's tagged JSON (sorted keys, compact
//! separators — plain JSON for the values the portal stores).
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;

/// A visitor who never logs in gets a short-lived row (the login page fetches a token).
pub const ANONYMOUS_LIFETIME_S: f64 = 2.0 * 3600.0;

pub fn token_hash(token: &str) -> String {
    hex::encode(Sha256::digest(token.as_bytes()))
}

/// `secrets.token_urlsafe(32)`: 43 URL-safe characters.
pub fn new_token() -> Option<String> {
    use base64::Engine;
    let mut b = [0u8; 32];
    getrandom::getrandom(&mut b).ok()?;
    Some(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b))
}

fn lifetime() -> f64 {
    std::env::var("SESSION_MAX_AGE").ok().and_then(|v| v.parse().ok()).unwrap_or(12.0 * 3600.0)
}

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// Flask's `TaggedJSONSerializer.dumps` for plain values: keys sorted at every level.
pub fn dumps(v: &Value) -> String {
    fn sorted(v: &Value) -> Value {
        match v {
            Value::Object(m) => {
                let mut keys: Vec<&String> = m.keys().collect();
                keys.sort();
                Value::Object(keys.into_iter().map(|k| (k.clone(), sorted(&m[k]))).collect::<Map<String, Value>>())
            }
            Value::Array(a) => Value::Array(a.iter().map(sorted).collect()),
            other => other.clone(),
        }
    }
    serde_json::to_string(&sorted(v)).unwrap_or_else(|_| "{}".to_string())
}

/// `save_session` for a non-empty session: upsert the row (12 h with an
/// account, 2 h without), sweep expired rows now and then.
pub async fn save(pool: &SqlitePool, token: &str, data: &Map<String, Value>) -> Result<(), sqlx::Error> {
    let now = now();
    let ttl = if data.contains_key("username") { lifetime() } else { ANONYMOUS_LIFETIME_S };
    sqlx::query(
        "INSERT INTO web_sessions (token_hash, data, expires_at, updated_at) VALUES (?,?,?,?) \
         ON CONFLICT(token_hash) DO UPDATE SET data=excluded.data, expires_at=excluded.expires_at, updated_at=excluded.updated_at",
    )
    .bind(token_hash(token))
    .bind(dumps(&Value::Object(data.clone())))
    .bind(now + ttl)
    .bind(now)
    .execute(pool)
    .await?;
    let mut roll = [0u8; 1];
    if getrandom::getrandom(&mut roll).is_ok() && roll[0] < 5 {
        // ~2 %: no thread, a write is the cue.
        sqlx::query("DELETE FROM web_sessions WHERE expires_at<=?").bind(now).execute(pool).await?;
    }
    Ok(())
}

/// The emptied session (logout): no row.
pub async fn delete(pool: &SqlitePool, token: &str) -> Result<(), sqlx::Error> {
    sqlx::query("DELETE FROM web_sessions WHERE token_hash=?").bind(token_hash(token)).execute(pool).await.map(|_| ())
}

fn secure() -> bool {
    std::env::var("SESSION_COOKIE_SECURE").map_or(true, |v| v != "0")
}

/// Flask's `set_cookie` for a non-permanent session (no Expires).
pub fn set_cookie(token: &str) -> String {
    let secure = if secure() { " Secure;" } else { "" };
    format!("session={token};{secure} HttpOnly; Path=/; SameSite=Lax")
}

/// Flask's `delete_cookie`.
pub fn delete_cookie() -> String {
    let secure = if secure() { " Secure;" } else { "" };
    format!("session=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0;{secure} HttpOnly; Path=/; SameSite=Lax")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn dumps_sorts_keys_compactly_like_flask() {
        assert_eq!(dumps(&json!({"username": "a", "csrf": "x", "auth_at": 1.5, "n": {"b": 1, "a": true}})),
            r#"{"auth_at":1.5,"csrf":"x","n":{"a":true,"b":1},"username":"a"}"#);
    }

    #[test]
    fn tokens_look_like_token_urlsafe_32() {
        let t = new_token().unwrap();
        assert_eq!(t.len(), 43);
        assert_eq!(token_hash("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }
}
