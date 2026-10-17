//! Account budgets set by an admin (admin_users.py): an account's exact cap,
//! and the approval or rejection of a budget request. Each one writes the
//! LiteLLM envelope first and the portal's records after, as Flask does.
//! Compared on the parity bench, LiteLLM calls included.
use super::*;
use super::admin_writes::{admin_form, json_error, json_ok, owner_protected, parse_py_float};
use crate::chat::support_tools::grouped;

/// Above this, an amount is a typo that makes the account unlimited in effect.
const MAX_AMOUNT: f64 = 1_000_000_000_000.0;

/// `get_setting('default_key_duration', KEY_DURATION)`.
pub(crate) async fn key_duration(state: &AppState) -> String {
    match state.db.get_setting("default_key_duration").await.ok().flatten() {
        Some(v) => v,
        None => super::admin_routes::key_duration_default().as_str().unwrap_or("7d").to_string(),
    }
}

/// `litellm_update_user_budget(username, amount, budget_duration)`.
pub(crate) async fn update_envelope(username: &str, amount: f64, duration: &str) -> bool {
    let mut payload = json!({"user_id": username, "max_budget": amount});
    if !duration.is_empty() {
        payload["budget_duration"] = json!(duration);
    }
    super::key_routes::litellm_post("/user/update", payload, 5).await.is_some_and(|r| r.status().is_success())
}

/// POST /admin/users/<username>/budget/set - the account's cap, exactly.
pub async fn admin_budget_set(State(state): State<AppState>, Path(username): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/users/x/budget/set").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    if let Some(r) = owner_protected(&state, &s, &username).await {
        return r;
    }
    let raw = form.get("budget").map(String::as_str).unwrap_or("");
    // `not v > 0` refuses NaN as well (it passed `v <= 0` before).
    let Some(value) = parse_py_float(raw).filter(|v| *v > 0.0) else {
        return json_error(
            "Budget : nombre strictement positif attendu (tokens). Pour couper l'accès d'un compte, utilise le blocage du compte — réversible et tracé ; un budget de 0 le plafonne réellement à zéro token.",
            StatusCode::BAD_REQUEST,
        );
    };
    if value > MAX_AMOUNT {
        return json_error("Montant irréaliste (> 1e12 tokens) — vérifie la saisie.", StatusCode::BAD_REQUEST);
    }
    let duration = key_duration(&state).await;
    if !update_envelope(&username, value, &duration).await {
        return json_error("Erreur lors de la mise à jour du budget sur LiteLLM.", StatusCode::SERVICE_UNAVAILABLE);
    }
    let pool = state.db.pool();
    let _ = sqlx::query("DELETE FROM budget_grants WHERE username=?").bind(&username).execute(pool).await;
    // A directory account's group quota no longer overwrites this cap at login.
    let _ = sqlx::query("UPDATE user_sources SET budget_manual=1, budget_auto=NULL WHERE username=?").bind(&username).execute(pool).await;
    let by = if s.username.is_empty() { "?" } else { s.username.as_str() };
    crate::sessions::log_audit(pool, by, "budget_set", &format!("{username}={value:.0}")).await;
    json_ok(&format!("Budget de {username} redéfini à {} tokens / {duration}.", grouped(value)), json!({}))
}

/// `<int:req_id>`: digits only, else werkzeug's 404 before any other check.
fn req_id(raw: &str) -> Option<i64> {
    (!raw.is_empty() && raw.bytes().all(|c| c.is_ascii_digit())).then(|| raw.parse().ok()).flatten()
}

fn utc_iso(dt: chrono::NaiveDateTime) -> String {
    keys::iso(&dt)
}

/// POST /admin/budget/approve/<int:req_id> - add `amount` to the account's
/// envelope, for good or for `grant_days` days (a grant: the reaper brings the
/// account back to its base cap at the deadline).
pub async fn admin_budget_approve(State(state): State<AppState>, Path(id): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let Some(id) = req_id(&id) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/budget/approve/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let breq: Option<(Option<String>, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT username, fullname, status FROM budget_requests WHERE id=?").bind(id).fetch_optional(pool).await.ok().flatten();
    let Some((Some(user), fullname, Some(_))) = breq.filter(|b| b.2.as_deref() == Some("pending")) else {
        return json_error("Demande introuvable ou déjà traitée.", StatusCode::NOT_FOUND);
    };
    let fullname = fullname.unwrap_or_else(|| "None".into());
    let Some(amount) = form.get("amount").and_then(|a| parse_py_float(a)).filter(|v| *v > 0.0) else {
        return json_error("Le montant à ajouter doit être un nombre positif.", StatusCode::BAD_REQUEST);
    };
    if amount > MAX_AMOUNT {
        return json_error("Montant irréaliste (> 1e12 tokens) — vérifie la saisie.", StatusCode::BAD_REQUEST);
    }
    let current = keys::account_info(&user).await.max_budget.filter(|b| *b != 0.0).unwrap_or(0.0);
    let duration = key_duration(&state).await;
    let now = chrono::Utc::now().naive_utc();
    let grant: Option<(i64, f64, f64)> = sqlx::query_as("SELECT id, CAST(base_budget AS REAL), CAST(current_budget AS REAL) FROM budget_grants WHERE username=? AND expires_at > ?")
        .bind(&user)
        .bind(utc_iso(now))
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
    let days_raw = crate::memory::py_strip(form.get("grant_days").map(String::as_str).unwrap_or("")).to_string();
    let mut expires_at = None;
    if !days_raw.is_empty() {
        let Some(days) = super::support_routes::py_int(&Value::String(days_raw)).filter(|d| (1..=365).contains(d)) else {
            return json_error("Durée de la subvention : nombre de jours entre 1 et 365 (vide = permanent).", StatusCode::BAD_REQUEST);
        };
        expires_at = Some(utc_iso(now + chrono::Duration::days(days)));
    }
    let base = grant.map_or(current, |g| g.1);
    let new_budget;
    if let Some(exp) = &expires_at {
        // A temporary grant: the base cap is frozen at the first boost, a
        // second one stacks on top, the deadline returns to the same base.
        new_budget = grant.map_or(base, |g| g.2) + amount;
        if !update_envelope(&user, new_budget, &duration).await {
            return json_error("Erreur lors de la mise à jour du budget sur LiteLLM.", StatusCode::SERVICE_UNAVAILABLE);
        }
        let now_iso = utc_iso(chrono::Utc::now().naive_utc());
        let _ = match grant {
            Some(g) => sqlx::query("UPDATE budget_grants SET current_budget=?, expires_at=?, updated_at=? WHERE id=?").bind(new_budget).bind(exp).bind(&now_iso).bind(g.0).execute(pool).await,
            None => sqlx::query("INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?)")
                .bind(&user).bind(base).bind(new_budget).bind(exp).bind(&now_iso).bind(&now_iso).execute(pool).await,
        };
    } else {
        new_budget = current + amount;
        if !update_envelope(&user, new_budget, &duration).await {
            return json_error("Erreur lors de la mise à jour du budget sur LiteLLM.", StatusCode::SERVICE_UNAVAILABLE);
        }
        if let Some(g) = grant {
            // A permanent raise during a grant must survive the deadline: it raises the base too.
            let _ = sqlx::query("UPDATE budget_grants SET base_budget=?, current_budget=?, updated_at=? WHERE id=?")
                .bind(g.1 + amount).bind(g.2 + amount).bind(utc_iso(chrono::Utc::now().naive_utc())).bind(g.0).execute(pool).await;
        }
    }
    let _ = sqlx::query("UPDATE budget_requests SET status='approved', granted_amount=?, updated_at=? WHERE id=?")
        .bind(amount)
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .bind(id)
        .execute(pool)
        .await;
    super::image_routes::add_notification(pool, &user, "request", &format!("Budget accordé : +{} tokens.", grouped(amount))).await;
    let tail = match &expires_at {
        Some(exp) => format!(" (temporaire, retour à {base:.0} le {})", &exp[..16.min(exp.len())]),
        None => " (permanent)".to_string(),
    };
    crate::sessions::log_audit(pool, &s.username, "budget.approve", &format!("{user} +{amount:.0} tokens{tail}")).await;
    let msg = match &expires_at {
        Some(exp) => format!(
            "+{} tokens accordés à {fullname} (total temporaire : {}, retour à {} le {} UTC).",
            grouped(amount),
            grouped(new_budget),
            grouped(base),
            exp[..16.min(exp.len())].replace('T', " ")
        ),
        None => format!("+{} tokens accordés à {fullname} (nouveau total : {} / {duration}).", grouped(amount), grouped(new_budget)),
    };
    json_ok(&msg, json!({}))
}

/// POST /admin/budget/reject/<int:req_id>
pub async fn admin_budget_reject(State(state): State<AppState>, Path(id): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let Some(id) = req_id(&id) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/budget/reject/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let row: Option<(Option<String>,)> = sqlx::query_as("SELECT username FROM budget_requests WHERE id=?").bind(id).fetch_optional(pool).await.ok().flatten();
    let Some((user,)) = row else {
        return json_error("Demande introuvable.", StatusCode::NOT_FOUND);
    };
    let _ = sqlx::query("UPDATE budget_requests SET status='rejected', updated_at=? WHERE id=?")
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .bind(id)
        .execute(pool)
        .await;
    let user = user.unwrap_or_default();
    if !user.is_empty() {
        super::image_routes::add_notification(pool, &user, "request", "Ta demande de budget a été refusée.").await;
    }
    crate::sessions::log_audit(pool, &s.username, "budget.reject", if user.is_empty() { "?" } else { &user }).await;
    json_ok("Demande rejetée.", json!({}))
}
