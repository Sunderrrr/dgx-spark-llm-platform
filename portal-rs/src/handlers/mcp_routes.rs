//! The viewer's MCP servers (settings_routes.py `/mcp`): register one (checked
//! live: initialize + tools/list), update, toggle, delete.
#![allow(unused_imports)]
use super::*;
use crate::mcp;

const MAX_MCP_SERVERS: i64 = 16;

fn valid_name(name: &str) -> bool {
    (1..=60).contains(&name.chars().count()) && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// A live check of the server: the number of tools it offers, or the refusal.
async fn probe(url: &str, auth: Option<&str>, ldap: &str) -> Result<usize, String> {
    let mut c = mcp::Client::new(url, auth, ldap);
    let checked = async {
        c.initialize().await?;
        c.list_tools().await
    };
    match checked.await {
        Ok(tools) => Ok(tools.len()),
        Err(mcp::McpError::Said(e)) => Err(format!("Connexion au serveur MCP impossible : {e}")),
        Err(mcp::McpError::Transport) => Err("Connexion au serveur MCP impossible.".into()),
    }
}

/// POST /mcp - create, update, toggle or delete one of the viewer's MCP servers
pub async fn mcp_form(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let form = form_of(&headers, &body);
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
    let field = |k: &str, n: usize| -> String { raw(k).trim_matches(memory::py_is_space).chars().take(n).collect() };
    let refuse = |msg: &str| Json(json!({"ok": false, "error": msg})).into_response();
    let unique = |e: &sqlx::Error| matches!(e, sqlx::Error::Database(d) if d.is_unique_violation());
    let server_error = || auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    let action = form.get("action").map(String::as_str);
    if matches!(action, Some("create" | "update")) {
        // A live outbound connection to a user-supplied URL: bounded.
        match guards::rate_limited(pool, user, "rl-mcp", guards::MEDIA_RATE_MAX, guards::MEDIA_RATE_WINDOW_S).await {
            Ok(0) => {}
            Ok(wait) => return (StatusCode::TOO_MANY_REQUESTS, Json(json!({"ok": false, "error": format!("Trop de tentatives, réessaie dans {wait} s.")}))).into_response(),
            Err(_) => return server_error(),
        }
    }
    let ldap = ldap_host(&state).await;
    match action {
        Some("create") => {
            let count: i64 = match sqlx::query_scalar("SELECT COUNT(*) FROM mcp_servers WHERE username=?").bind(user).fetch_one(pool).await {
                Ok(c) => c,
                Err(_) => return server_error(),
            };
            if count >= MAX_MCP_SERVERS {
                return refuse(&format!("Maximum {MAX_MCP_SERVERS} serveurs MCP par compte."));
            }
            let (name, url) = (field("name", 60), field("url", usize::MAX));
            let auth_header = Some(field("auth_header", usize::MAX)).filter(|a| !a.is_empty());
            let (description, allowed) = (field("description", 300), field("allowed_tools", 500));
            if name.is_empty() || url.is_empty() {
                return refuse("Nom et URL requis.");
            }
            if !valid_name(&name) {
                return refuse("Lettres, chiffres, underscores et tirets uniquement.");
            }
            if let Err(e) = skillstore::validated_target(&url, &ldap).await {
                return refuse(&e);
            }
            let tools = match probe(&url, auth_header.as_deref(), &ldap).await {
                Ok(n) => n,
                Err(e) => return refuse(&e),
            };
            match sqlx::query("INSERT INTO mcp_servers (username, name, url, auth_header, description, allowed_tools, enabled, created_at) VALUES (?,?,?,?,?,?,1,?)")
                .bind(user).bind(&name).bind(&url).bind(&auth_header).bind(&description).bind(&allowed)
                .bind(keys::iso(&chrono::Local::now().naive_local()))
                .execute(pool).await
            {
                Ok(_) => Json(json!({"ok": true, "tool_count": tools})).into_response(),
                Err(e) if unique(&e) => refuse("Tu as déjà un serveur MCP avec ce nom."),
                Err(_) => server_error(),
            }
        }
        Some("update") => {
            let id = raw("id").to_string();
            let row: Option<Option<String>> = match sqlx::query_scalar("SELECT auth_header FROM mcp_servers WHERE id=? AND username=?").bind(&id).bind(user).fetch_optional(pool).await {
                Ok(r) => r,
                Err(_) => return server_error(),
            };
            let Some(stored_auth) = row else { return refuse("Serveur introuvable.") };
            let (name, url) = (field("name", 60), field("url", usize::MAX));
            let (description, allowed) = (field("description", 300), field("allowed_tools", 500));
            // Left empty on redisplay = unchanged (the secret is never sent back); « - » clears it.
            let raw_auth = raw("auth_header");
            let trimmed = raw_auth.trim_matches(memory::py_is_space);
            let auth_header = if trimmed == "-" {
                None
            } else if raw_auth.is_empty() {
                stored_auth
            } else {
                Some(trimmed.to_string()).filter(|a| !a.is_empty())
            };
            if name.is_empty() || url.is_empty() {
                return refuse("Nom et URL requis.");
            }
            if !valid_name(&name) {
                return refuse("Lettres, chiffres, underscores et tirets uniquement.");
            }
            if let Err(e) = skillstore::validated_target(&url, &ldap).await {
                return refuse(&e);
            }
            let tools = match probe(&url, auth_header.as_deref(), &ldap).await {
                Ok(n) => n,
                Err(e) => return refuse(&e),
            };
            // Writing a key is what brings a server waiting for it to life; nothing
            // is ever disabled behind the user's back.
            match sqlx::query("UPDATE mcp_servers SET name=?, url=?, auth_header=?, description=?, allowed_tools=?, enabled=CASE WHEN ? THEN 1 ELSE enabled END WHERE id=? AND username=?")
                .bind(&name).bind(&url).bind(&auth_header).bind(&description).bind(&allowed)
                .bind(i64::from(auth_header.is_some())).bind(&id).bind(user)
                .execute(pool).await
            {
                // Flask also forgets its own per-process tools cache here (TTL 120 s).
                Ok(_) => Json(json!({"ok": true, "tool_count": tools})).into_response(),
                Err(e) if unique(&e) => refuse("Tu as déjà un serveur MCP avec ce nom."),
                Err(_) => server_error(),
            }
        }
        Some("toggle") => {
            let enabled = i64::from(raw("enabled") == "1");
            match sqlx::query("UPDATE mcp_servers SET enabled=? WHERE id=? AND username=?").bind(enabled).bind(raw("id")).bind(user).execute(pool).await {
                Ok(_) => Json(json!({"ok": true})).into_response(),
                Err(_) => server_error(),
            }
        }
        other => {
            if other == Some("delete") {
                if sqlx::query("DELETE FROM mcp_servers WHERE id=? AND username=?").bind(raw("id")).bind(user).execute(pool).await.is_err() {
                    return server_error();
                }
            }
            (StatusCode::NO_CONTENT, [(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")]).into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_names_are_ascii_words() {
        assert!(valid_name("context7_docs-2"));
        assert!(!valid_name("é"));
        assert!(!valid_name(""));
        assert!(!valid_name(&"a".repeat(61)));
    }
}
