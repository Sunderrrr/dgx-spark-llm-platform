//! The « Clés API » tab's actions (app.py `POST /keys`): create, revoke and
//! rename the viewer's LiteLLM keys, each answering honestly. The budget
//! request (`request_budget`) notifies the admin by Discord and e-mail: it is
//! relayed to Flask as is, which keeps those notifications where they live.
#![allow(unused_imports)]
use super::*;

fn litellm() -> (String, String) {
    (
        std::env::var("LITELLM_URL").unwrap_or_else(|_| "http://litellm:4000".to_string()).trim_end_matches('/').to_string(),
        std::env::var("LITELLM_MASTER_KEY").unwrap_or_default(),
    )
}

async fn litellm_post(path: &str, body: Value, secs: u64) -> Option<reqwest::Response> {
    let (base, key) = litellm();
    let c = reqwest::Client::builder().timeout(std::time::Duration::from_secs(secs)).build().ok()?;
    c.post(format!("{base}{path}")).bearer_auth(key).json(&body).send().await.ok()
}

/// The alias rule: anything but `[A-Za-z0-9_-]` becomes `-`, 40 characters.
pub(crate) fn key_alias(raw: &str) -> String {
    raw.chars().map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '-' { c } else { '-' }).take(40).collect()
}

/// `_ensure_litellm_user`: the account envelope exists and carries a budget.
async fn ensure_user(username: &str, max_budget: f64, duration: &str) {
    let info = keys::account_info(username).await;
    if info.exists {
        if info.max_budget.is_none() {
            let _ = litellm_post("/user/update", json!({"user_id": username, "max_budget": max_budget, "budget_duration": duration}), 8).await;
        }
        return;
    }
    let _ = litellm_post("/user/new", json!({"user_id": username, "metadata": {"created_by": "dgx-portal"}, "max_budget": max_budget, "budget_duration": duration}), 8).await;
}

/// `create_litellm_key`: the new key, or None when LiteLLM did not answer.
async fn create_key(state: &AppState, alias: &str, username: &str, is_admin: bool) -> Option<String> {
    let mut payload = json!({"key_alias": alias, "metadata": {"user": username, "created_by": "dgx-portal"}});
    if !is_admin {
        // The budget is the ACCOUNT's, shared by all its keys through user_id.
        let key_budget: f64 = std::env::var("KEY_MAX_BUDGET").ok().and_then(|v| v.parse().ok()).unwrap_or(200_000_000.0);
        let budget = state.db.get_setting("default_key_budget").await.ok().flatten().and_then(|v| v.trim().parse().ok()).unwrap_or(key_budget);
        let duration = state.db.get_setting("default_key_duration").await.ok().flatten()
            .unwrap_or_else(|| std::env::var("KEY_BUDGET_DURATION").unwrap_or_else(|_| "7d".to_string()));
        ensure_user(username, budget, &duration).await;
        payload["user_id"] = json!(username);
    }
    let r = litellm_post("/key/generate", payload, 10).await?;
    if !r.status().is_success() {
        return None;
    }
    r.json::<Value>().await.ok()?.get("key")?.as_str().map(str::to_string)
}

/// Relays the request to Flask unchanged (cookie, CSRF, body) and returns its answer.
async fn relay_to_flask(headers: &HeaderMap, body: axum::body::Bytes) -> Response {
    let base = std::env::var("FLASK_URL").unwrap_or_else(|_| "http://dgx-portal:5000".to_string());
    let Ok(c) = reqwest::Client::builder().timeout(std::time::Duration::from_secs(60)).redirect(reqwest::redirect::Policy::none()).build() else {
        return auth::html_status(StatusCode::SERVICE_UNAVAILABLE);
    };
    let mut req = c.post(format!("{}/keys", base.trim_end_matches('/'))).body(body);
    for name in [axum::http::header::COOKIE, axum::http::header::CONTENT_TYPE, axum::http::HeaderName::from_static("x-csrftoken"),
                 axum::http::HeaderName::from_static("x-forwarded-for"), axum::http::HeaderName::from_static("cf-connecting-ip")] {
        if let Some(v) = headers.get(&name) {
            req = req.header(name.as_str(), v.as_bytes());
        }
    }
    match req.send().await {
        Ok(r) => {
            let status = StatusCode::from_u16(r.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
            let ctype = r.headers().get(reqwest::header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("text/html; charset=utf-8").to_string();
            let bytes = r.bytes().await.unwrap_or_default();
            (status, [(axum::http::header::CONTENT_TYPE, ctype)], bytes).into_response()
        }
        Err(_) => auth::html_status(StatusCode::SERVICE_UNAVAILABLE),
    }
}

/// POST /keys - create, revoke or rename one of the viewer's keys (or request a budget)
pub async fn keys_form(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let form = form_of(&headers, &body);
    if form.get("action").map(String::as_str) == Some("request_budget") {
        return relay_to_flask(&headers, body).await;
    }
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::csrf_refused(),
        Ok(true) => {}
    }
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let user = session.username.as_str();
    let pool = state.db.pool();
    let raw = |k: &str| form.get(k).map(String::as_str).unwrap_or("");
    let answer = |status: StatusCode, v: Value| (status, Json(v)).into_response();
    let server_error = || auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    match form.get("action").map(String::as_str) {
        Some("create") => {
            let name = raw("key_name").trim_matches(memory::py_is_space);
            let alias = if name.is_empty() { format!("{user}-{}", chrono::Utc::now().timestamp()) } else { key_alias(name) };
            let Some(new_key) = create_key(&state, &alias, user, session.is_admin).await else {
                return answer(StatusCode::SERVICE_UNAVAILABLE, json!({"ok": false, "error": "La clé n'a pas pu être créée : le service de clés (LiteLLM) n'a pas répondu. Réessaie dans un instant."}));
            };
            match sqlx::query("INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) VALUES (?,?,?,?)")
                .bind(user).bind(&alias).bind(&new_key).bind(keys::iso(&chrono::Local::now().naive_local()))
                .execute(pool).await
            {
                Ok(_) => answer(StatusCode::OK, json!({"ok": true, "key_alias": alias, "key": new_key})),
                Err(_) => server_error(),
            }
        }
        Some("revoke") => {
            let k = raw("key");
            let owns = sqlx::query_scalar::<_, i64>("SELECT 1 FROM api_keys WHERE key_value=? AND username=?").bind(k).bind(user).fetch_optional(pool).await;
            match owns {
                Err(_) => return server_error(),
                Ok(None) => return answer(StatusCode::NOT_FOUND, json!({"ok": false, "error": "Clé introuvable sur ce compte."})),
                Ok(Some(_)) => {}
            }
            // The key travels in the body, never in a URL.
            let revoked = litellm_post("/key/delete", json!({"keys": [k]}), 5).await.is_some_and(|r| r.status().is_success());
            if !revoked {
                return answer(StatusCode::SERVICE_UNAVAILABLE, json!({"ok": false, "error": "La clé n'a PAS pu être révoquée : LiteLLM n'a pas répondu. Elle est encore valide, réessaie."}));
            }
            match sqlx::query("DELETE FROM api_keys WHERE key_value=? AND username=?").bind(k).bind(user).execute(pool).await {
                Ok(_) => answer(StatusCode::OK, json!({"ok": true})),
                Err(_) => server_error(),
            }
        }
        Some("rename") => {
            let k = raw("key");
            let owns = sqlx::query_scalar::<_, Option<String>>("SELECT key_alias FROM api_keys WHERE key_value=? AND username=?").bind(k).bind(user).fetch_optional(pool).await;
            match owns {
                Err(_) => return server_error(),
                Ok(None) => return answer(StatusCode::NOT_FOUND, json!({"ok": false, "error": "Clé introuvable sur ce compte."})),
                Ok(Some(_)) => {}
            }
            let alias = key_alias(raw("key_name").trim_matches(memory::py_is_space));
            if alias.is_empty() {
                return answer(StatusCode::BAD_REQUEST, json!({"ok": false, "error": "Le nom doit contenir au moins un caractère (lettres, chiffres, - ou _)."}));
            }
            let duplicate = sqlx::query_scalar::<_, i64>("SELECT 1 FROM api_keys WHERE username=? AND key_alias=? AND key_value<>?").bind(user).bind(&alias).bind(k).fetch_optional(pool).await;
            match duplicate {
                Err(_) => return server_error(),
                Ok(Some(_)) => return answer(StatusCode::CONFLICT, json!({"ok": false, "code": "alias_in_use", "error": "Tu as déjà une clé nommée ainsi."})),
                Ok(None) => {}
            }
            // LiteLLM first: its console must not keep the old name.
            let renamed = litellm_post("/key/update", json!({"key": k, "key_alias": alias}), 5).await.is_some_and(|r| r.status().is_success());
            if !renamed {
                return answer(StatusCode::SERVICE_UNAVAILABLE, json!({"ok": false, "error": "Le nom n'a PAS pu être changé : LiteLLM n'a pas répondu. La clé est intacte, réessaie."}));
            }
            match sqlx::query("UPDATE api_keys SET key_alias=? WHERE key_value=? AND username=?").bind(&alias).bind(k).bind(user).execute(pool).await {
                Ok(_) => answer(StatusCode::OK, json!({"ok": true, "key_alias": alias})),
                Err(_) => server_error(),
            }
        }
        _ => answer(StatusCode::BAD_REQUEST, json!({"ok": false, "error": "Action inconnue."})),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn aliases_follow_the_portal_rule() {
        assert_eq!(key_alias("mon portable ✓"), "mon-portable--");
        assert_eq!(key_alias(&"a".repeat(50)).len(), 40);
    }
}
