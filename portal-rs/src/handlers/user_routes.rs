//! User data routes: pending counts, notifications, announcements, skills list,
//! conversations, Discord status, API keys, settings, sessions, sharing, preferences.
#![allow(unused_imports)]
use super::*;

/// Answer of a user-data read: the value, or the unreadable-database refusal.
pub(crate) fn data_or_503(r: Result<Value, sqlx::Error>) -> Response {
    match r {
        Ok(v) => Json(v).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// GET /api/pending-count - the sidebar's badges
pub async fn api_pending_count(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(userdata::pending_count(state.db.pool(), &s.username, s.is_admin).await),
    }
}

/// GET /api/notifications - the bell
pub async fn api_notifications(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(userdata::notifications(state.db.pool(), &s.username).await),
    }
}

/// GET /api/announcements - announcements the viewer has not seen
pub async fn api_announcements(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(userdata::announcements(state.db.pool(), &s.username).await),
    }
}

/// GET /api/skills - the viewer's skills
pub async fn api_skills(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(userdata::skills(state.db.pool(), &s.username, 50).await),
    }
}

/// GET /api/conversations - the list, newest first, with its size budget
pub async fn api_conversations(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match viewer(&state, &headers).await {
        Err(r) => r,
        Ok(s) => data_or_503(conversations::list(state.db.pool(), &s.username).await),
    }
}

/// GET /api/conversations/:client_id - one whole conversation (Flask's JSON errors)
pub async fn api_conversation(State(state): State<AppState>, headers: HeaderMap, Path(client_id): Path<String>) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match conversations::one(state.db.pool(), &session.username, &client_id).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err("introuvable")) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "conversation introuvable"}))).into_response(),
        Ok(Err(_)) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"ok": false, "error": "conversation illisible"}))).into_response(),
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
    }
}

/// GET /api/discord/status - account linking and DMs, and this account's link
pub async fn api_discord_status(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let (client_id, client_secret, bot_token) = tokio::join!(
        state.db.get_config("discord_client_id"),
        state.db.get_config("discord_client_secret"),
        state.db.get_config("discord_bot_token"),
    );
    let link_enabled = client_id.is_some() && client_secret.is_some();
    Json(userdata::discord_status(state.db.pool(), &session.username, link_enabled, bot_token.is_some()).await).into_response()
}

/// GET /api/keys - the viewer's keys, the account envelope and the model limits
/// (app.api_keys). The keys are returned in clear, as Flask does.
pub async fn api_keys(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let db = &state.db;
    let key_budget: f64 = std::env::var("KEY_MAX_BUDGET").ok().and_then(|v| v.parse().ok()).unwrap_or(200_000_000.0);
    let default_budget: f64 = db
        .get_setting("default_key_budget")
        .await
        .ok()
        .flatten()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(key_budget);
    let duration = db
        .get_setting("default_key_duration")
        .await
        .ok()
        .flatten()
        .unwrap_or_else(|| std::env::var("KEY_BUDGET_DURATION").unwrap_or_else(|_| "7d".to_string()));

    let acct = keys::account_info(&session.username).await;
    let has_pending = sqlx::query_as::<_, (i64,)>("SELECT 1 FROM budget_requests WHERE username = ? AND status = 'pending'")
        .bind(&session.username)
        .fetch_optional(db.pool())
        .await
        .ok()
        .flatten()
        .is_some();
    let account = json!({
        "spend": acct.spend,
        "max_budget": if acct.exists { json!(acct.max_budget) } else { json!(default_budget) },
        "budget_reset_at": acct.budget_reset_at,
        "unlimited": session.is_admin,
        "has_pending": has_pending,
    });

    // Limits per catalog model, then `auto-model` inheriting the running one's.
    let mut model_limits = serde_json::Map::new();
    let catalog: Vec<(String, Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT name, vllm_args, engine FROM model_configs",
    )
    .fetch_all(db.pool())
    .await
    .unwrap_or_default();
    for (name, args, engine) in catalog {
        let engine = engine.filter(|e| !e.is_empty()).unwrap_or_else(|| "vllm".to_string());
        let (max_in, max_out) = health::ctx_split(args.as_deref().unwrap_or_default(), &engine);
        model_limits.insert(name, json!({"context": max_in, "output": max_out}));
    }
    let running = models::running_models().await;
    let auto_name = std::env::var("AUTO_MODEL_NAME").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "auto-model".to_string());
    let auto = running
        .first()
        .and_then(|m| model_limits.get(m).cloned())
        .unwrap_or_else(|| json!({"context": 262144, "output": 131072}));
    model_limits.insert(auto_name.clone(), auto);

    let (used, remaining) = home::budget_remaining(
        state.spend.as_ref(),
        &session.username,
        default_budget,
        home::budget_period_days(&duration),
    )
    .await;
    let user_keys = keys::user_keys(db.pool(), state.spend.as_ref(), &session.username).await;

    Json(json!({
        "user_keys": user_keys,
        "budget_tokens": home::thousands(default_budget),
        "budget_duration": duration,
        "budget_used": used,
        "budget_remaining": remaining,
        "account": account,
        "model_limits": model_limits,
        "running_models": running,
        "auto_model": auto_name,
        "public_api_url": std::env::var("PUBLIC_API_URL").unwrap_or_default(),
    }))
    .into_response()
}

/// Shared shape of the viewer's POST writes that take no body: CSRF first (as
/// Flask's before_request does, 400 even without a session), then the session.
pub(crate) async fn csrf_then_viewer(state: &AppState, headers: &HeaderMap, body: &axum::body::Bytes) -> Result<auth::SessionData, Response> {
    // The token comes from the X-CSRFToken header or a form's csrf_token field, as in Flask.
    let form = form_of(headers, body);
    match auth::csrf_ok(headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return Err(e.into_response()),
        Ok(false) => return Err(auth::csrf_refused()),
        Ok(true) => {}
    }
    viewer(state, headers).await
}

/// POST /api/notifications/seen - mark the bell's notifications as read
pub async fn api_notifications_seen(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match userdata::notifications_seen(state.db.pool(), &session.username).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// POST /api/announcements/seen - mark the announcements as read up to the newest
pub async fn api_announcements_seen(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match userdata::announcements_seen(state.db.pool(), &session.username).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
}

/// GET /api/settings - the settings page's data (settings_routes.api_settings)
pub async fn api_settings(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let db = state.db.pool();
    let username = session.username.as_str();
    let servers: Vec<Value> = match sqlx::query(
        "SELECT id, name, url, description, allowed_tools, enabled, (auth_header IS NOT NULL) AS has_auth, created_at \
         FROM mcp_servers WHERE username = ? ORDER BY created_at DESC",
    )
    .bind(username)
    .fetch_all(db)
    .await
    {
        Ok(rows) => rows.iter().map(|r| json!({
            "id": r.get::<i64, _>("id"),
            "name": r.get::<Option<String>, _>("name"),
            "url": r.get::<Option<String>, _>("url"),
            "description": r.get::<Option<String>, _>("description"),
            "allowed_tools": r.get::<Option<String>, _>("allowed_tools"),
            "enabled": r.get::<Option<i64>, _>("enabled"),
            "has_auth": r.get::<Option<i64>, _>("has_auth"),
            "created_at": r.get::<Option<String>, _>("created_at"),
        })).collect(),
        Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
    };
    let skills: Vec<Value> = match sqlx::query(
        "SELECT id, name, description, instructions, created_at, source, version, enabled \
         FROM skills WHERE username = ? ORDER BY created_at DESC",
    )
    .bind(username)
    .fetch_all(db)
    .await
    {
        Ok(rows) => rows.iter().map(|r| json!({
            "id": r.get::<i64, _>("id"),
            "name": r.get::<Option<String>, _>("name"),
            "description": r.get::<Option<String>, _>("description"),
            "instructions": r.get::<Option<String>, _>("instructions"),
            "created_at": r.get::<Option<String>, _>("created_at"),
            "source": r.get::<Option<String>, _>("source"),
            "version": r.get::<Option<String>, _>("version"),
            "enabled": r.get::<Option<i64>, _>("enabled"),
        })).collect(),
        Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
    };
    let pref: Option<(Option<String>, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT avatar_id, theme_id, lang FROM user_prefs WHERE username = ?")
            .bind(username)
            .fetch_optional(db)
            .await
            .ok()
            .flatten();
    let (avatar_id, theme_id, lang) = match pref {
        Some((a, t, l)) => (a, t, l),
        None => (None, None, None),
    };

    let key_budget: f64 = std::env::var("KEY_MAX_BUDGET").ok().and_then(|v| v.parse().ok()).unwrap_or(200_000_000.0);
    let default_budget: f64 = state
        .db
        .get_setting("default_key_budget")
        .await
        .ok()
        .flatten()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(key_budget);
    let duration = state
        .db
        .get_setting("default_key_duration")
        .await
        .ok()
        .flatten()
        .unwrap_or_else(|| std::env::var("KEY_BUDGET_DURATION").unwrap_or_else(|_| "7d".to_string()));
    let acct = keys::account_info(username).await;
    let key_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM api_keys WHERE username = ?")
        .bind(username)
        .fetch_one(db)
        .await
        .unwrap_or(0);

    let running = models::running_models().await;
    let ctx = match running.first() {
        Some(model) => match state.db.model_config(model).await {
            Ok(Some((engine, args))) => {
                let engine = engine.filter(|e| !e.is_empty()).unwrap_or_else(|| "vllm".to_string());
                health::effective_ctx(args.as_deref().unwrap_or_default(), &engine)
            }
            _ => None,
        },
        None => None,
    };
    let activity = settings::activity(state.spend.as_ref(), username, 182).await;
    let limits = settings::limits(
        db,
        username,
        session.is_admin,
        default_budget,
        &acct,
        servers.len() as i64,
        skills.len() as i64,
        &running,
        ctx,
    )
    .await;

    Json(json!({
        "mcp_servers": servers,
        "skills": skills,
        "avatar_id": avatar_id,
        "theme_id": theme_id.unwrap_or_else(|| "neutral".to_string()),
        "lang": lang.unwrap_or_else(|| "en".to_string()),
        "theme_ids": settings::THEME_IDS,
        "langs": settings::LANGS,
        "avatars": settings::AVATAR_IDS.iter().map(|a| json!({"id": a, "label": settings::avatar_label(a)})).collect::<Vec<_>>(),
        "account": {
            "username": username,
            "fullname": session.fullname.clone().unwrap_or_else(|| username.to_string()),
            "is_admin": session.is_admin,
            "spend": acct.spend,
            "max_budget": acct.max_budget,
            "unlimited": session.is_admin,
            "budget_reset_at": Some(acct.budget_reset_at.clone()).filter(|s| !s.is_empty()),
            "budget_duration": duration,
            "key_count": key_count,
            "mcp_count": servers.len(),
            "skill_count": skills.len(),
        },
        "activity": activity,
        "limits": limits,
    }))
    .into_response()
}

/// GET /api/account/sessions - the viewer's active sessions and how the password is managed
pub async fn api_account_sessions(State(state): State<AppState>, headers: HeaderMap, connect: Option<axum::extract::ConnectInfo<std::net::SocketAddr>>) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    // complete_session_origin: fill in this browser's origin where missing.
    if let Some(sid) = session.sid.as_deref() {
        let ip = auth::client_ip(&headers, connect.map(|c| c.0.ip()));
        let ua = headers.get(axum::http::header::USER_AGENT).and_then(|v| v.to_str().ok()).unwrap_or_default();
        if let Err(e) = sessions::complete_origin(state.db.pool(), sid, &session.username, &ip, ua).await {
            let _ = e;
        }
    }
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    let listed = match sessions::list(state.db.pool(), &session.username, session.sid.as_deref(), now).await {
        Ok(v) => v,
        Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
    };
    let management = match state.db.get_password_management(&session.username).await {
        Ok(m) => m,
        Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
    };
    Json(json!({
        "sessions": listed,
        "local_account": management.local,
        "password_managed_by": management.management,
        "passkey_possible": management.passkey,
    }))
    .into_response()
}

/// POST /api/account/sessions/revoke - close one of the viewer's sessions (`id`), or all the others (`all`)
pub async fn api_account_sessions_revoke(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Response {
    // CSRF first, then the session; the body is JSON, or a form as the fallback.
    let form: std::collections::HashMap<String, String> = if headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/x-www-form-urlencoded"))
    {
        serde_urlencoded::from_bytes(&body).unwrap_or_default()
    } else {
        Default::default()
    };
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::csrf_refused(),
        Ok(true) => {}
    }
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    // Flask: `request.get_json(silent=True) or request.form`.
    let json_body: Option<Value> = serde_json::from_slice::<Value>(&body).ok().filter(|v| v.is_object());
    let field = |name: &str| -> Option<Value> {
        match &json_body {
            Some(v) if v.get(name).is_some() => v.get(name).cloned(),
            _ => form.get(name).map(|s| Value::String(s.clone())),
        }
    };
    let all = match field("all") {
        Some(Value::Bool(true)) => true,
        Some(Value::String(s)) => matches!(s.as_str(), "1" | "true" | "on"),
        _ => false,
    };
    let db = state.db.pool();
    if all {
        let n = match sessions::revoke_others(db, &session.username, session.sid.as_deref()).await {
            Ok(n) => n,
            Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
        };
        sessions::log_audit(db, &session.username, "account.sessions.revoke",
            &format!("{n} autre(s) session(s) fermée(s) par l'utilisateur")).await;
        return Json(json!({"ok": true, "revoked": n})).into_response();
    }
    let ident = match field("id") {
        Some(Value::String(s)) => s.trim().to_string(),
        _ => String::new(),
    };
    match sessions::revoke_one(db, &session.username, &ident).await {
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
        Ok(sessions::RevokeOne::TooShort) => (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Identifiant de session invalide."}))).into_response(),
        Ok(sessions::RevokeOne::NotFound) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "Session introuvable (déjà fermée ?)."}))).into_response(),
        Ok(sessions::RevokeOne::Ambiguous) => (StatusCode::CONFLICT, Json(json!({"ok": false, "error": "Identifiant de session ambigu, réessaie."}))).into_response(),
        Ok(sessions::RevokeOne::Revoked(target)) => {
            let current = session.sid.as_deref() == Some(target.as_str());
            let detail = if current { "fermeture de la session courante".to_string() } else { format!("fermeture de la session {ident}") };
            sessions::log_audit(db, &session.username, "account.sessions.revoke", &detail).await;
            Json(json!({"ok": true, "revoked": 1, "current": current})).into_response()
        }
    }
}

/// GET /c/:token - a shared conversation, read-only, for signed-in viewers only
pub async fn share_view(State(state): State<AppState>, headers: HeaderMap, Path(token): Path<String>) -> Response {
    if let Err(r) = viewer(&state, &headers).await {
        return r;
    }
    match sharing::load_and_count(state.db.pool(), &token).await {
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
        Ok(None) => auth::html_status(StatusCode::NOT_FOUND),
        Ok(Some(share)) => {
            let platform = state.db.get_setting_set("platform_name").await.unwrap_or_else(|| "DGX".to_string());
            ([(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], sharing::page(&share, &platform)).into_response()
        }
    }
}

/// Form fields of a POST (request.form): an urlencoded body, nothing else.
pub(crate) fn form_of(headers: &HeaderMap, body: &axum::body::Bytes) -> std::collections::HashMap<String, String> {
    let is_form = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/x-www-form-urlencoded"));
    if is_form { serde_urlencoded::from_bytes(body).unwrap_or_default() } else { Default::default() }
}

/// POST /settings/appearance - theme and language (allow-listed)
pub async fn settings_appearance(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
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
    let theme = form.get("theme_id").map(String::as_str);
    let lang = form.get("lang").map(String::as_str);
    if let Some(t) = theme {
        if !settings::THEME_IDS.contains(&t) {
            return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Thème inconnu."}))).into_response();
        }
    }
    if let Some(l) = lang {
        if !settings::LANGS.contains(&l) {
            return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Langue inconnue."}))).into_response();
        }
    }
    match userdata::set_appearance(state.db.pool(), &session.username, theme, lang).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
    }
}

/// POST /settings/avatar - a listed avatar, or the generated one ('' or 'genere')
pub async fn settings_avatar(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
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
    let requested = form.get("avatar_id").map(String::as_str).unwrap_or_default();
    let avatar: Option<&str> = if requested.is_empty() || requested == "genere" {
        None
    } else if settings::AVATAR_IDS.contains(&requested) {
        Some(requested)
    } else {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Avatar inconnu."}))).into_response();
    };
    match userdata::set_avatar(state.db.pool(), &session.username, avatar).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
    }
}

/// POST /conversations - `action=save` (upsert, bounded, the 30 newest kept) or
/// `action=delete` (one of the viewer's; `deleted` counts the rows).
pub async fn conversations_post(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
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
    let bad = |msg: String| (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": msg}))).into_response();
    let action = form.get("action").map(String::as_str);
    match action {
        Some("save") => {
            let field = |k: &str| form.get(k).map(String::as_str).unwrap_or("").trim().to_string();
            let client_id: String = field("id").chars().take(64).collect();
            let title: String = field("title").chars().take(120).collect();
            let title = if title.is_empty() { "Conversation".to_string() } else { title };
            let model: String = field("model").chars().take(80).collect();
            if client_id.is_empty() {
                return bad("id manquant".to_string());
            }
            let raw = form.get("messages").map(String::as_str).unwrap_or("[]");
            let Ok(Value::Array(messages)) = serde_json::from_str::<Value>(raw) else {
                return bad("messages invalides".to_string());
            };
            match conversations::save(state.db.pool(), &session.username, &client_id, &title, &model, &messages).await {
                Ok(()) => Json(json!({"ok": true})).into_response(),
                Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
            }
        }
        Some("delete") => {
            let id = form.get("id").map(String::as_str).unwrap_or("");
            match conversations::delete(state.db.pool(), &session.username, id).await {
                Ok(n) => Json(json!({"ok": true, "deleted": n})).into_response(),
                Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
            }
        }
        other => bad(format!("Action inconnue : {}", conversations::py_repr(other))),
    }
}

/// POST /conversations/share - a read-only link to one of the viewer's conversations
pub async fn conversations_share(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
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
    // Flask: `request.get_json(silent=True) or request.form` (JSON only with its type).
    let is_json = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/json"));
    let json_body = if is_json { serde_json::from_slice::<Value>(&body).ok().filter(keys::truthy) } else { None };
    let raw_id: String = match &json_body {
        Some(v) => match v.get("client_id") {
            None => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(Value::Null) => "None".to_string(),
            Some(other) => other.to_string(),
        },
        None => form.get("client_id").cloned().unwrap_or_default(),
    };
    let client_id: String = raw_id.trim().chars().take(64).collect();
    if client_id.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "id manquant"}))).into_response();
    }
    match conversations::share(state.db.pool(), &session.username, &client_id).await {
        Ok(Some(token)) => Json(json!({"ok": true, "token": token})).into_response(),
        Ok(None) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "conversation introuvable"}))).into_response(),
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
    }
}

/// POST /api/onboarding/done - the onboarding is seen, once and for all
pub async fn api_onboarding_done(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match sqlx::query("INSERT INTO user_prefs (username, onboarded) VALUES (?, 1) ON CONFLICT(username) DO UPDATE SET onboarded = 1")
        .bind(&session.username)
        .execute(state.db.pool())
        .await
    {
        Ok(_) => Json(json!({"ok": true})).into_response(),
        Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
    }
}

/// POST /discord/unlink - forget this account's Discord link, back to the home page
pub async fn discord_unlink(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
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
    // A failure is swallowed, as in Flask: the redirect says « unlinked » either way.
    let _ = sqlx::query("DELETE FROM discord_links WHERE username = ?").bind(&session.username).execute(state.db.pool()).await;
    (
        StatusCode::FOUND,
        [
            (axum::http::header::LOCATION, "/?discord=unlinked"),
            (axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8"),
        ],
    )
        .into_response()
}

/// POST /skills - a skill written in the interface: create, update, delete.
/// Refusals answer 200 with `ok: false`, as Flask does; delete answers 204.
pub async fn skills_form(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
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
    let field = |k: &str, n: usize| -> String {
        form.get(k).map(String::as_str).unwrap_or("").trim_matches(memory::py_is_space).chars().take(n).collect()
    };
    let refuse = |msg: &str| Json(json!({"ok": false, "error": msg})).into_response();
    let no_content = || (StatusCode::NO_CONTENT, [(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")]).into_response();
    let unique = |e: &sqlx::Error| matches!(e, sqlx::Error::Database(d) if d.is_unique_violation());
    match form.get("action").map(String::as_str) {
        Some("create") => {
            let count: i64 = match sqlx::query_scalar("SELECT COUNT(*) FROM skills WHERE username = ?").bind(user).fetch_one(pool).await {
                Ok(c) => c,
                Err(_) => return data_or_503(Err(sqlx::Error::PoolTimedOut)),
            };
            if count >= skillstore::MAX_SKILLS {
                return refuse(&format!("Maximum {} compétences par compte.", skillstore::MAX_SKILLS));
            }
            let (name, description, instructions) = (field("name", 60), field("description", 300), field("instructions", 20000));
            if name.is_empty() || description.is_empty() || instructions.is_empty() {
                return refuse("Nom, description et instructions requis.");
            }
            match sqlx::query("INSERT INTO skills (username, name, description, instructions, created_at) VALUES (?, ?, ?, ?, ?)")
                .bind(user).bind(&name).bind(&description).bind(&instructions).bind(keys::iso(&chrono::Utc::now().naive_utc()))
                .execute(pool).await
            {
                Ok(_) => Json(json!({"ok": true})).into_response(),
                Err(e) if unique(&e) => refuse("Tu as déjà une compétence avec ce nom."),
                Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
            }
        }
        Some("update") => {
            let id = form.get("id").cloned().unwrap_or_default();
            let (name, description, instructions) = (field("name", 60), field("description", 300), field("instructions", 20000));
            if name.is_empty() || description.is_empty() || instructions.is_empty() {
                return refuse("Nom, description et instructions requis.");
            }
            match sqlx::query("UPDATE skills SET name = ?, description = ?, instructions = ? WHERE id = ? AND username = ?")
                .bind(&name).bind(&description).bind(&instructions).bind(&id).bind(user)
                .execute(pool).await
            {
                Err(e) if unique(&e) => refuse("Tu as déjà une compétence avec ce nom."),
                Err(_) => data_or_503(Err(sqlx::Error::PoolTimedOut)),
                Ok(r) if r.rows_affected() == 0 => refuse("Compétence introuvable."),
                Ok(_) => Json(json!({"ok": true})).into_response(),
            }
        }
        Some("delete") => {
            let id = form.get("id").cloned().unwrap_or_default();
            let deleted = sqlx::query("DELETE FROM skills WHERE id = ? AND username = ?").bind(&id).bind(user).execute(pool).await;
            if deleted.as_ref().is_ok_and(|r| r.rows_affected() > 0) {
                let _ = sqlx::query("DELETE FROM skill_files WHERE skill_id = ? AND username = ?").bind(&id).bind(user).execute(pool).await;
            }
            no_content()
        }
        _ => no_content(),
    }
}

/// GET /api/security - the passkey state of the viewer's account and its keys
pub async fn api_security(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let pool = state.db.pool();
    let user = &session.username;
    let read = async {
        let creds: Vec<(i64, Option<String>, Option<String>, Option<f64>)> = sqlx::query_as(
            "SELECT id, credential_id, label, created_at FROM webauthn_credentials WHERE username=? ORDER BY created_at DESC",
        )
        .bind(user)
        .fetch_all(pool)
        .await?;
        let enabled: Option<Option<i64>> = sqlx::query_scalar("SELECT enabled FROM user_security WHERE username=?").bind(user).fetch_optional(pool).await?;
        Ok::<_, sqlx::Error>((creds, enabled.flatten().unwrap_or(0) != 0))
    };
    let Ok((creds, enabled)) = read.await else { return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR) };
    let management = match state.db.get_password_management(user).await {
        Ok(m) => m,
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    let credentials: Vec<Value> = creds
        .into_iter()
        .map(|(id, cid, label, created)| json!({"id": id, "credential_id": cid, "label": label, "created_at": created}))
        .collect();
    Json(json!({
        "enabled": enabled,
        "password_managed_by": management.management,
        "passkey_possible": management.passkey,
        "credentials": credentials,
    }))
    .into_response()
}
