//! What a user asks the admins for (app.py): a model added to the platform
//! (`POST /request`), a media model launched (`/api/model/request`), more
//! tokens (`POST /keys`, `action=request_budget`). Each is recorded, then the
//! admins are told by the Discord webhook and/or e-mail.
use super::*;

/// `config.MEDIA_REQUEST_COOLDOWN_S`: one media request per account and category in this window.
fn media_cooldown_s() -> f64 {
    std::env::var("MEDIA_REQUEST_COOLDOWN_S").ok().and_then(|v| v.trim().parse::<i64>().ok()).unwrap_or(1800) as f64
}

/// A string field of a JSON body, else of the form (`get_json(silent=True) or request.form`).
fn json_or_form(headers: &HeaderMap, body: &axum::body::Bytes) -> std::collections::HashMap<String, Value> {
    let data = super::memory_routes::json_object(headers, body);
    if !data.is_empty() {
        return data.into_iter().collect();
    }
    form_of(headers, body).into_iter().map(|(k, v)| (k, Value::String(v))).collect()
}

/// `login_required` on a page path: signed out → the login page.
async fn page_viewer(state: &AppState, headers: &HeaderMap, path: &str) -> Result<auth::SessionData, Response> {
    match auth::require_session(headers, &state.db).await {
        Ok(s) => Ok(s),
        Err(auth::AuthError::Unauthorized) => Err(auth::login_redirect(path)),
        Err(e) => Err(e.into_response()),
    }
}

/// POST /request - ask for a model to be added to the platform.
pub async fn request_model(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let form = form_of(&headers, &body);
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::csrf_refused(),
        Ok(true) => {}
    }
    let s = match page_viewer(&state, &headers, "/request").await {
        Ok(s) => s,
        Err(r) => return r,
    };
    let data = json_or_form(&headers, &body);
    let text = |k: &str, n: usize| -> String { data.get(k).and_then(Value::as_str).map(|v| crate::memory::py_strip(v).chars().take(n).collect()).unwrap_or_default() };
    let model_id = text("model_id", 200);
    let reason = text("reason", 500);
    if model_id.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "code": "identifier_required", "error": "L'identifiant du modèle est requis."}))).into_response();
    }
    let pool = state.db.pool();
    let existing = sqlx::query_scalar::<_, i64>("SELECT id FROM model_requests WHERE username=? AND model_id=? AND status='pending'")
        .bind(&s.username)
        .bind(&model_id)
        .fetch_optional(pool)
        .await;
    match existing {
        Ok(Some(_)) => {
            return (
                StatusCode::CONFLICT,
                Json(json!({"ok": false, "code": "already_pending", "error": format!("Tu as déjà une demande en attente pour « {model_id} ».")})),
            )
                .into_response()
        }
        Ok(None) => {}
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
    let r = sqlx::query("INSERT INTO model_requests (username, fullname, model_id, reason, status, created_at) VALUES (?,?,?,?,?,?)")
        .bind(&s.username)
        .bind(&s.fullname)
        .bind(&model_id)
        .bind(&reason)
        .bind("pending")
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .execute(pool)
        .await;
    if r.is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let fullname = s.fullname.as_deref().unwrap_or("None");
    let discord_ok = crate::discord::model_request(pool, &model_id, &s.username, fullname, &reason).await;
    let email_ok = crate::mailer::model_request(pool, &model_id, &s.username, s.fullname.as_deref(), &reason).await;
    let mut out = json!({"ok": true, "message": format!("Demande envoyée pour « {model_id} » !"), "discord_sent": discord_ok, "email_sent": email_ok});
    if !(discord_ok || email_ok) {
        out["warning"] = json!("Demande enregistrée, mais l'admin n'a pu être prévenu (ni Discord ni email).");
    }
    Json(out).into_response()
}

/// `_media_category_running(category)`: a model of the category is loaded.
async fn media_category_running(category: &str) -> bool {
    match category {
        "image" => crate::sidecars::image_ready().await,
        "video" => crate::sidecars::comfyui_up().await,
        "ocr" => crate::sidecars::ocr_model().await.is_some(),
        "voice" => crate::sidecars::voice_model().await.is_some(),
        _ => false,
    }
}

/// POST /api/model/request - tell the admins a media model is wanted.
pub async fn api_model_request(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let form = form_of(&headers, &body);
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::csrf_refused(),
        Ok(true) => {}
    }
    let s = match viewer(&state, &headers).await {
        Ok(s) => s,
        Err(r) => return r,
    };
    let data = super::memory_routes::json_object(&headers, &body);
    let category = data.get("category").filter(|v| keys::truthy(v)).and_then(Value::as_str).map(|c| crate::memory::py_strip(c).to_lowercase()).unwrap_or_default();
    if !["image", "video", "ocr", "voice"].contains(&category.as_str()) {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": {"message": "Catégorie inconnue."}}))).into_response();
    }
    if media_category_running(&category).await {
        return (StatusCode::CONFLICT, Json(json!({"error": {"message": format!("Un modèle « {category} » est déjà chargé.")}}))).into_response();
    }
    let pool = state.db.pool();
    let now = chrono::Utc::now().timestamp_micros() as f64 / 1e6;
    let last: Option<f64> = sqlx::query_scalar("SELECT CAST(created_at AS REAL) FROM media_request_cooldown WHERE username=? AND category=?")
        .bind(&s.username)
        .bind(&category)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
    let window = media_cooldown_s();
    if let Some(t) = last.filter(|t| now - t < window) {
        let retry_after = (window - (now - t)) as i64;
        let remaining = retry_after / 60 + 1;
        return (
            StatusCode::TOO_MANY_REQUESTS,
            Json(json!({"error": {"message": format!("Déjà signalé. Nouvelle demande possible dans ≈ {remaining} min.")}, "cooldown": true, "retry_after": retry_after})),
        )
            .into_response();
    }
    let _ = sqlx::query("INSERT INTO media_request_cooldown (username, category, created_at) VALUES (?,?,?) ON CONFLICT(username, category) DO UPDATE SET created_at=excluded.created_at")
        .bind(&s.username)
        .bind(&category)
        .bind(now)
        .execute(pool)
        .await;
    let sent = crate::mailer::media_request(pool, &category, &s.username, s.fullname.as_deref().unwrap_or("")).await;
    Json(json!({"ok": true, "category": category, "email_sent": sent})).into_response()
}

/// `POST /keys` `action=request_budget`: more tokens for the account (one
/// pending request at a time), the admins told by Discord and e-mail.
pub(crate) async fn request_budget(state: &AppState, s: &auth::SessionData, form: &std::collections::HashMap<String, String>) -> Response {
    let reason: String = crate::memory::py_strip(form.get("reason").map(String::as_str).unwrap_or("")).chars().take(500).collect();
    let current = keys::account_info(&s.username).await.max_budget;
    let pool = state.db.pool();
    match sqlx::query_scalar::<_, i64>("SELECT id FROM budget_requests WHERE username=? AND status='pending'").bind(&s.username).fetch_optional(pool).await {
        Ok(Some(_)) => {
            return (StatusCode::CONFLICT, Json(json!({"ok": false, "code": "already_pending", "error": "Tu as déjà une demande en attente."}))).into_response();
        }
        Ok(None) => {}
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    }
    let r = sqlx::query("INSERT INTO budget_requests (username, fullname, key_alias, current_budget, reason, status, created_at) VALUES (?,?,?,?,?,?,?)")
        .bind(&s.username)
        .bind(&s.fullname)
        .bind("(compte)")
        .bind(current)
        .bind(&reason)
        .bind("pending")
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .execute(pool)
        .await;
    if r.is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let fullname = s.fullname.as_deref().unwrap_or("None");
    crate::discord::budget_request(pool, &s.username, fullname, "(compte)", current, &reason).await;
    crate::mailer::budget_request(pool, &s.username, fullname, "(compte)", current, &reason).await;
    Json(json!({"ok": true})).into_response()
}
