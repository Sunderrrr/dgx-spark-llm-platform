//! Signing in with the SSO provider (OIDC, sso_client.py and app.py's
//! /login/sso and /api/oauth2-redirect): the authorization-code flow, the
//! id_token checked (signature by the provider's JWKS, issuer, audience,
//! expiry, nonce), then the identity resolved by the DIRECTORY, never taken
//! from the token's editable claims.
use serde_json::{json, Value};
use sqlx::SqlitePool;
use std::time::Duration;

/// `sso_client._UA`: some IdPs behind Cloudflare refuse non-browser agents.
const UA: &str = "Mozilla/5.0 (DGX portal)";

/// `oidc_config()`, when SSO is configured (metadata URL, client ID, secret).
pub struct Oidc {
    pub metadata_url: String,
    pub client_id: String,
    pub client_secret: String,
    pub admin_group: String,
    pub redirect_uri: String,
}

async fn setting(pool: &SqlitePool, name: &str, default: &str) -> String {
    let v: Option<String> = sqlx::query_scalar("SELECT value FROM settings WHERE key=?").bind(format!("cfg_{name}")).fetch_optional(pool).await.ok().flatten();
    v.filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

pub async fn config(pool: &SqlitePool) -> Option<Oidc> {
    let c = Oidc {
        metadata_url: setting(pool, "oidc_metadata_url", "").await,
        client_id: setting(pool, "oidc_client_id", "").await,
        client_secret: setting(pool, "oidc_client_secret", "").await,
        admin_group: setting(pool, "oidc_admin_group", "admins").await,
        redirect_uri: setting(pool, "oidc_redirect_uri", "").await,
    };
    (!c.metadata_url.is_empty() && !c.client_id.is_empty() && !c.client_secret.is_empty()).then_some(c)
}

/// The provider's metadata (`_metadata`), {} when unreachable.
pub async fn metadata(url: &str) -> Value {
    match crate::sidecars::http().get(url).header(reqwest::header::USER_AGENT, UA).timeout(Duration::from_secs(15)).send().await {
        Ok(r) => r.json().await.unwrap_or_else(|_| json!({})),
        Err(_) => json!({}),
    }
}

/// `secrets.token_urlsafe(n)`.
pub fn token(n: usize) -> Option<String> {
    use base64::Engine;
    let mut raw = vec![0u8; n];
    getrandom::getrandom(&mut raw).ok()?;
    Some(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw))
}

/// The authorization URL for this state and nonce.
pub fn authorize_url(md: &Value, c: &Oidc, redirect_uri: &str, state: &str, nonce: &str) -> Option<String> {
    let endpoint = md.get("authorization_endpoint")?.as_str()?;
    let q = serde_urlencoded::to_string([
        ("response_type", "code"),
        ("client_id", c.client_id.as_str()),
        ("redirect_uri", redirect_uri),
        ("scope", "openid profile email"),
        ("state", state),
        ("nonce", nonce),
    ])
    .ok()?;
    Some(format!("{endpoint}{}{q}", if endpoint.contains('?') { "&" } else { "?" }))
}

/// The code exchanged (client_secret_basic), the id_token verified; its
/// claims, else the userinfo endpoint's answer.
pub async fn exchange(md: &Value, c: &Oidc, code: &str, redirect_uri: &str, nonce: &str) -> Result<Value, String> {
    let http = crate::sidecars::http();
    let token_endpoint = md.get("token_endpoint").and_then(Value::as_str).ok_or("no token endpoint")?;
    let r = http
        .post(token_endpoint)
        .basic_auth(&c.client_id, Some(&c.client_secret))
        .header(reqwest::header::USER_AGENT, UA)
        .form(&[("grant_type", "authorization_code"), ("code", code), ("redirect_uri", redirect_uri)])
        .timeout(Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !r.status().is_success() {
        return Err(format!("token endpoint HTTP {}", r.status().as_u16()));
    }
    let tok: Value = r.json().await.map_err(|e| e.to_string())?;
    let mut claims = json!({});
    if let Some(id_token) = tok.get("id_token").and_then(Value::as_str) {
        claims = verify_id_token(md, c, id_token, nonce).await?;
    }
    if claims.as_object().is_some_and(|o| !o.is_empty()) {
        return Ok(claims);
    }
    // `client.userinfo(token=token)`
    let access = tok.get("access_token").and_then(Value::as_str).ok_or("no access token")?;
    let endpoint = md.get("userinfo_endpoint").and_then(Value::as_str).ok_or("no userinfo endpoint")?;
    let r = http.get(endpoint).bearer_auth(access).header(reqwest::header::USER_AGENT, UA).timeout(Duration::from_secs(15)).send().await.map_err(|e| e.to_string())?;
    Ok(r.json().await.unwrap_or_else(|_| json!({})))
}

async fn verify_id_token(md: &Value, c: &Oidc, id_token: &str, nonce: &str) -> Result<Value, String> {
    use jsonwebtoken::{decode, decode_header, jwk::JwkSet, Algorithm, DecodingKey, Validation};
    let header = decode_header(id_token).map_err(|e| e.to_string())?;
    let jwks_uri = md.get("jwks_uri").and_then(Value::as_str).ok_or("no jwks_uri")?;
    let set: JwkSet = crate::sidecars::http().get(jwks_uri).header(reqwest::header::USER_AGENT, UA).timeout(Duration::from_secs(15)).send().await.map_err(|e| e.to_string())?.json().await.map_err(|e| e.to_string())?;
    let jwk = match &header.kid {
        Some(kid) => set.find(kid),
        None => set.keys.first(),
    }
    .ok_or("no matching key")?;
    let key = DecodingKey::from_jwk(jwk).map_err(|e| e.to_string())?;
    if !matches!(header.alg, Algorithm::RS256 | Algorithm::RS384 | Algorithm::RS512 | Algorithm::ES256 | Algorithm::ES384 | Algorithm::PS256) {
        return Err("unexpected id_token algorithm".into());
    }
    let mut v = Validation::new(header.alg);
    v.set_audience(&[&c.client_id]);
    if let Some(iss) = md.get("issuer").and_then(Value::as_str) {
        v.set_issuer(&[iss]);
    }
    v.leeway = 120;
    let data = decode::<Value>(id_token, &key, &v).map_err(|e| e.to_string())?;
    if data.claims.get("nonce").and_then(Value::as_str) != Some(nonce) {
        return Err("nonce mismatch".into());
    }
    Ok(data.claims)
}
