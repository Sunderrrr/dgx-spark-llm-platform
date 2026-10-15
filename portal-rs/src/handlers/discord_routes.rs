//! Linking a Discord account (discord_routes.py, OAuth2 « identify »): the
//! link sends the browser to Discord with a state kept in the session; the
//! callback checks it, exchanges the code and records the account. Every
//! outcome is a redirect to the home page with `?discord=…`.
use super::*;
use crate::discord::{api, setting};

fn home(outcome: &str) -> Response {
    let to = format!("/?discord={outcome}");
    (StatusCode::FOUND, [(axum::http::header::LOCATION, to.clone()), (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8".to_string())], auth::werkzeug_redirect_body(&to)).into_response()
}

/// (client_id, client_secret, redirect_uri), when linking is configured.
async fn link_config(state: &AppState) -> Option<(String, String, String)> {
    let pool = state.db.pool();
    let id = setting(pool, "cfg_discord_client_id").await;
    let secret = setting(pool, "cfg_discord_client_secret").await;
    (!id.is_empty() && !secret.is_empty()).then_some((id, secret, setting(pool, "cfg_discord_redirect_uri").await))
}

/// `url_for('discord.discord_callback', _external=True)` behind ProxyFix.
fn callback_url(headers: &HeaderMap) -> String {
    let h = |n: &str| headers.get(n).and_then(|v| v.to_str().ok()).map(|v| v.split(',').next().unwrap_or("").trim().to_string()).filter(|v| !v.is_empty());
    let scheme = h("x-forwarded-proto").unwrap_or_else(|| "http".into());
    let host = h("x-forwarded-host").or_else(|| h("host")).unwrap_or_else(|| "localhost".into());
    format!("{scheme}://{host}/discord/callback")
}

/// The signed-in session: its data and token, or the login page.
async fn session_of(state: &AppState, headers: &HeaderMap, path: &str) -> Result<(auth::SessionData, String, serde_json::Map<String, Value>), Response> {
    let s = match auth::require_session(headers, &state.db).await {
        Ok(s) => s,
        Err(auth::AuthError::Unauthorized) => return Err(auth::login_redirect(path)),
        Err(e) => return Err(e.into_response()),
    };
    let token = auth::get_session_token(headers).unwrap_or_default();
    let data = state.db.get_session(&token).await.ok().flatten().and_then(|v| v.as_object().cloned()).unwrap_or_default();
    Ok((s, token, data))
}

/// GET /discord/link
pub async fn discord_link(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let (_, token, mut data) = match session_of(&state, &headers, "/discord/link").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let Some((client_id, _, redirect_uri)) = link_config(&state).await else { return home("unavailable") };
    let mut raw = [0u8; 24];
    if getrandom::getrandom(&mut raw).is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    use base64::Engine;
    let oauth_state = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw);
    data.insert("discord_oauth_state".into(), json!(oauth_state));
    if crate::session_store::save(state.db.pool(), &token, &data).await.is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let redirect_uri = if redirect_uri.is_empty() { callback_url(&headers) } else { redirect_uri };
    let q = serde_urlencoded::to_string([
        ("client_id", client_id.as_str()),
        ("redirect_uri", redirect_uri.as_str()),
        ("response_type", "code"),
        ("scope", "identify"),
        ("state", oauth_state.as_str()),
        ("prompt", "consent"),
    ])
    .unwrap_or_default();
    let to = format!("https://discord.com/api/oauth2/authorize?{q}");
    (StatusCode::FOUND, [(axum::http::header::LOCATION, to.clone()), (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8".to_string())], auth::werkzeug_redirect_body(&to)).into_response()
}

/// The token exchange then `/users/@me`: (id, displayed name).
async fn identify(client_id: &str, secret: &str, code: &str, redirect_uri: &str) -> Option<(String, String)> {
    let c = crate::sidecars::http();
    let tok = c
        .post(format!("{}/oauth2/token", api()))
        .form(&[("client_id", client_id), ("client_secret", secret), ("grant_type", "authorization_code"), ("code", code), ("redirect_uri", redirect_uri)])
        .timeout(std::time::Duration::from_secs(8))
        .send()
        .await
        .ok()?;
    if !tok.status().is_success() {
        return None;
    }
    let access = tok.json::<Value>().await.ok()?.get("access_token").filter(|v| keys::truthy(v))?.as_str()?.to_string();
    let me = c.get(format!("{}/users/@me", api())).bearer_auth(access).timeout(std::time::Duration::from_secs(8)).send().await.ok()?;
    let info: Value = if me.status().is_success() { me.json().await.unwrap_or_else(|_| json!({})) } else { json!({}) };
    let id = info.get("id").filter(|v| keys::truthy(v)).map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string()))?;
    let text = |k: &str| info.get(k).filter(|v| keys::truthy(v)).and_then(Value::as_str).map(str::to_string);
    let mut name = text("global_name").or_else(|| text("username")).unwrap_or_default();
    let disc = info.get("discriminator").filter(|v| keys::truthy(v));
    if let Some(d) = disc.filter(|d| d.as_str() != Some("0") && d.as_i64() != Some(0)) {
        let d = d.as_str().map(str::to_string).unwrap_or_else(|| d.to_string());
        name = format!("{}#{d}", info.get("username").and_then(Value::as_str).map(str::to_string).unwrap_or(name.clone()));
    }
    Some((id, name))
}

/// GET /discord/callback
pub async fn discord_callback(State(state): State<AppState>, headers: HeaderMap, Query(q): Query<std::collections::HashMap<String, String>>) -> Response {
    let (s, token, mut data) = match session_of(&state, &headers, "/discord/callback").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let Some((client_id, secret, redirect_uri)) = link_config(&state).await else { return home("unavailable") };
    // `session.pop`: the state is single use, whatever the outcome.
    let expected = data.remove("discord_oauth_state");
    if expected.is_some() {
        let _ = crate::session_store::save(state.db.pool(), &token, &data).await;
    }
    let given = q.get("state").filter(|v| !v.is_empty());
    if given.is_none() || expected.as_ref().and_then(Value::as_str) != given.map(String::as_str) {
        return home("error");
    }
    let Some(code) = q.get("code").filter(|v| !v.is_empty()) else { return home("error") };
    let redirect_uri = if redirect_uri.is_empty() { callback_url(&headers) } else { redirect_uri };
    let Some((id, name)) = identify(&client_id, &secret, code, &redirect_uri).await else { return home("error") };
    let r = sqlx::query("INSERT OR REPLACE INTO discord_links (username, discord_id, discord_name, linked_at) VALUES (?,?,?,?)")
        .bind(&s.username)
        .bind(id)
        .bind(name)
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .execute(state.db.pool())
        .await;
    if r.is_err() {
        return home("error");
    }
    home("linked")
}
