//! `/login/sso` and `/api/oauth2-redirect` (app.py): the SSO sign-in. The
//! state, nonce and return path live in the server session between the two;
//! every failure goes back to the login page.
use super::*;

fn to(location: &str) -> Response {
    (
        StatusCode::FOUND,
        [(axum::http::header::LOCATION, location.to_string()), (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8".to_string())],
        auth::werkzeug_redirect_body(location),
    )
        .into_response()
}

/// `url_for('oauth_callback', _external=True)` behind ProxyFix.
fn callback_url(headers: &HeaderMap) -> String {
    let h = |n: &str| headers.get(n).and_then(|v| v.to_str().ok()).map(|v| v.split(',').next().unwrap_or("").trim().to_string()).filter(|v| !v.is_empty());
    let scheme = h("x-forwarded-proto").unwrap_or_else(|| "http".into());
    let host = h("x-forwarded-host").or_else(|| h("host")).unwrap_or_else(|| "localhost".into());
    format!("{scheme}://{host}/api/oauth2-redirect")
}

/// The visitor's session data and token (a new anonymous session when none).
async fn session_data(state: &AppState, headers: &HeaderMap) -> (Option<String>, serde_json::Map<String, Value>) {
    let token = auth::get_session_token(headers).filter(|t| !t.contains('.'));
    let data = match &token {
        Some(t) => state.db.get_session(t).await.ok().flatten().and_then(|v| v.as_object().cloned()).unwrap_or_default(),
        None => Default::default(),
    };
    (token.filter(|_| !data.is_empty()), data)
}

/// GET /login/sso - to the provider, with a fresh state and nonce.
pub async fn login_sso(State(state): State<AppState>, headers: HeaderMap, Query(q): Query<HashMap<String, String>>) -> Response {
    let pool = state.db.pool();
    let Some(c) = crate::sso::config(pool).await else { return to("/login") };
    let md = crate::sso::metadata(&c.metadata_url).await;
    let redirect_uri = if c.redirect_uri.is_empty() { callback_url(&headers) } else { c.redirect_uri.clone() };
    let (Some(oauth_state), Some(nonce)) = (crate::sso::token(22), crate::sso::token(15)) else { return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
    let Some(url) = crate::sso::authorize_url(&md, &c, &redirect_uri, &oauth_state, &nonce) else {
        eprintln!("SSO: OIDC metadata unreachable ({})", c.metadata_url);
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    };
    let (token, mut data) = session_data(&state, &headers).await;
    data.insert("sso_next".into(), json!(crate::login::safe_next(q.get("next").map(String::as_str))));
    data.insert("sso_state".into(), json!(oauth_state));
    data.insert("sso_nonce".into(), json!(nonce));
    data.insert("sso_redirect_uri".into(), json!(redirect_uri));
    let new_token = token.is_none();
    let Some(token) = token.or_else(crate::session_store::new_token) else { return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
    if crate::session_store::save(pool, &token, &data).await.is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let mut r = to(&url);
    if new_token {
        if let Ok(v) = axum::http::HeaderValue::from_str(&crate::session_store::set_cookie(&token)) {
            r.headers_mut().insert(axum::http::header::SET_COOKIE, v);
        }
    }
    r
}

/// GET /api/oauth2-redirect - the provider's answer: the identity is the
/// directory's (by `sub`, else by a verified e-mail), never the token's own.
pub async fn oauth_callback(State(state): State<AppState>, connect: Option<axum::extract::ConnectInfo<std::net::SocketAddr>>, headers: HeaderMap, Query(q): Query<HashMap<String, String>>) -> Response {
    let pool = state.db.pool();
    let Some(c) = crate::sso::config(pool).await else { return to("/login") };
    let (token, mut data) = session_data(&state, &headers).await;
    // Single use: the state, nonce and return URI leave the session now.
    let expected = data.remove("sso_state");
    let nonce = data.remove("sso_nonce").and_then(|v| v.as_str().map(str::to_string)).unwrap_or_default();
    let redirect_uri = data.remove("sso_redirect_uri").and_then(|v| v.as_str().map(str::to_string)).unwrap_or_default();
    if let Some(t) = &token {
        let _ = crate::session_store::save(pool, t, &data).await;
    }
    let given = q.get("state").filter(|s| !s.is_empty());
    let code = q.get("code").filter(|s| !s.is_empty());
    let (Some(given), Some(code)) = (given, code) else { return to("/login") };
    if expected.as_ref().and_then(Value::as_str) != Some(given.as_str()) {
        return to("/login");
    }
    let md = crate::sso::metadata(&c.metadata_url).await;
    let Ok(userinfo) = crate::sso::exchange(&md, &c, code, &redirect_uri, &nonce).await else { return to("/login") };
    let text = |k: &str| userinfo.get(k).filter(|v| keys::truthy(v)).and_then(Value::as_str).map(|s| crate::memory::py_strip(s).to_string()).unwrap_or_default();
    let sub = text("sub");
    let mut email = text("email").to_lowercase();
    if userinfo.get("email_verified") == Some(&json!(false)) && !email.is_empty() {
        let short: String = sub.chars().take(64).collect();
        crate::sessions::log_audit(pool, "sso", "sso.email_non_verifie", &format!("repli sur l'adresse refusé pour sub={short} (email_verified=false)")).await;
        email.clear();
    }
    let Some((username, mut is_admin, fullname, ldap_groups)) = crate::ldap::resolve_sso_identity(pool, &sub, &email).await else { return to("/login") };
    let username = username.to_lowercase();
    if !crate::login::valid_username(&username) {
        return to("/login");
    }
    let fullname = if fullname.is_empty() { Some(text("name")).filter(|n| !n.is_empty()).unwrap_or_else(|| username.clone()) } else { fullname };
    let groups: Option<Vec<String>> = userinfo.get("groups").and_then(Value::as_array).map(|a| a.iter().map(|g| g.as_str().map(str::to_string).unwrap_or_else(|| g.to_string())).collect());
    if let Some(gs) = &groups {
        is_admin = is_admin || gs.iter().any(|g| *g == c.admin_group || crate::ldap::is_admin_group(g, &c.admin_group));
    }
    if sqlx::query_scalar::<_, i64>("SELECT 1 FROM blocked_users WHERE username=?").bind(&username).fetch_optional(pool).await.ok().flatten().is_some() {
        crate::sessions::log_audit(pool, &username, "login.refuse", "compte bloqué par un administrateur").await;
        return to("/login?refused=blocked");
    }
    let nxt = data.remove("sso_next").and_then(|v| v.as_str().map(str::to_string));
    if let Some(t) = &token {
        let _ = crate::session_store::save(pool, t, &data).await;
    }
    // `_import_directory_groups`: the directory's memberOf and the token's own claim.
    super::admin_groups::import_groups(pool, &username, "ldap", &ldap_groups).await;
    if let Some(gs) = &groups {
        super::admin_groups::import_groups(pool, &username, "sso", gs).await;
    }
    is_admin = is_admin || super::admin_groups::rights_from_groups(&state, &username).await.0;
    crate::login::record_source(pool, &username, "sso", &fullname, is_admin).await;
    super::admin_groups::apply_group_quota(&state, &username).await;
    let ip = auth::client_ip(&headers, connect.map(|c| c.0.ip()));
    let session_token = match crate::login::apply_session(&state, &headers, &ip, &username, &fullname, is_admin, true).await {
        Ok(t) => t,
        Err(r) => return r,
    };
    let mut r = to(&crate::login::safe_next(nxt.as_deref()));
    if let Ok(v) = axum::http::HeaderValue::from_str(&crate::session_store::set_cookie(&session_token)) {
        r.headers_mut().insert(axum::http::header::SET_COOKIE, v);
    }
    r
}
