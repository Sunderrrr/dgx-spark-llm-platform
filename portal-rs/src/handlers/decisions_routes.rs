//! The « Décisions » page's routes: the decision model's state, the run button,
//! and the admin start/stop.
//!
//! The page talks to the portal (session + CSRF), never to the engine: the
//! portal is the authority for authentication, and it is what holds the user's
//! API key that makes the call billable. The engine itself listens on its own
//! port, reachable only from the docker network and the host.
use super::*;

/// `GET /api/decisions` — the model's state and the limits the page enforces
/// client-side. A read: no CSRF, but a session.
pub async fn api_decisions(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match super::viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let mut status = crate::decisions::status().await;
    // The admin controls are rendered from this flag: the server stays the
    // authority on who may start or stop the engine, the page only hides them.
    status["admin"] = serde_json::Value::Bool(session.is_admin);
    Json(status).into_response()
}

/// `POST /api/decisions/run` — one System One request, relayed to LiteLLM with
/// the user's key so budgets and usage apply exactly as on chat.
pub async fn api_decisions_run(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match csrf_then_viewer(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match guards::rate_limited(state.db.pool(), &session.username, "rl-decisions", guards::MEDIA_RATE_MAX, guards::MEDIA_RATE_WINDOW_S).await {
        Ok(0) => {}
        Ok(wait) => {
            return (StatusCode::TOO_MANY_REQUESTS, Json(json!({"ok": false, "error": format!("Trop de requêtes. Réessaie dans {wait} s."), "wait": wait}))).into_response();
        }
        Err(_) => return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response(),
    }
    let data: Value = match serde_json::from_slice::<Value>(&body) {
        Ok(v) => v,
        Err(_) => return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Corps JSON invalide."}))).into_response(),
    };
    match crate::decisions::run(state.db.pool(), &session.username, &data).await {
        Ok(v) => Json(v).into_response(),
        Err((code, message)) => (StatusCode::from_u16(code).unwrap_or(StatusCode::BAD_REQUEST), Json(json!({"ok": false, "error": message}))).into_response(),
    }
}

/// The two admin routes share this: CSRF, then the admin flag. A viewer who is
/// not an admin gets 403 — never a diagnostic that reveals the engine's state.
async fn admin_guard(state: &AppState, headers: &HeaderMap, body: &axum::body::Bytes) -> Result<auth::SessionData, Response> {
    let session = csrf_then_viewer(state, headers, body).await?;
    if !session.is_admin {
        return Err((StatusCode::FORBIDDEN, Json(json!({"ok": false, "error": "Réservé aux administrateurs."}))).into_response());
    }
    Ok(session)
}

/// `POST /api/decisions/start` — admin. The runner's memory refusal is relayed
/// as it is: starting the decision model must never mean stopping the chat one.
pub async fn api_decisions_start(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match admin_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let (code, v) = crate::decisions::start().await;
    // The LiteLLM route follows a real start: `mode: decisions` is what makes
    // `/v1/systemone` answer for this model. A refused start registers nothing.
    if code == 200 && v.get("error").is_none() {
        let name = crate::decisions::model_name();
        if !crate::litellm_models::register_decisions(&name).await {
            crate::sessions::log_audit(state.db.pool(), &session.username, "decisions.register", "LiteLLM a refusé l'enregistrement").await;
        }
    }
    state_audit(&state, &session.username, "decisions.start", code).await;
    (StatusCode::from_u16(code).unwrap_or(StatusCode::OK), Json(v)).into_response()
}

/// `POST /api/decisions/stop` — admin, our engine only.
pub async fn api_decisions_stop(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match admin_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let (code, v) = crate::decisions::stop().await;
    state_audit(&state, &session.username, "decisions.stop", code).await;
    (StatusCode::from_u16(code).unwrap_or(StatusCode::OK), Json(v)).into_response()
}

/// The admin trace: who started or stopped the decision engine, and whether it
/// was accepted. The reason stays in the audit line, not in the UI's answer.
async fn state_audit(state: &AppState, username: &str, action: &str, code: u16) {
    crate::sessions::log_audit(state.db.pool(), username, action, &format!("HTTP {code}")).await;
}
