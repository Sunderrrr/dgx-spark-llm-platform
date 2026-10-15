//! Local accounts managed by an admin (admin_users.py, user_lifecycle.py):
//! create, update, delete, and the purge of a directory account's data.
//! An update that sets a password is handed to Flask, which e-mails the
//! account about it. Compared on the parity bench, LiteLLM calls included.
use super::*;
use super::admin_groups::{local_admins_without, local_group, local_is_admin, locals, parse_budget, sync_local_budget, Local};
use super::admin_writes::{admin_form, body_fields, field, json_error, owner_protected, revoke_account_keys, revoke_keys, username_ok};
use axum::extract::Request;
use sqlx::SqlitePool;

/// `user_lifecycle.PURGED_TABLES`: the tables attached to an account.
const PURGED_TABLES: [&str; 32] = [
    "announcement_state", "api_keys", "budget_grants", "budget_requests", "conversation_shares", "conversations",
    "discord_links", "image_jobs", "inflight_requests", "mcp_servers", "media_request_cooldown", "memory_aliases",
    "memory_edges", "memory_nodes", "model_requests", "notifications", "ocr_jobs", "pending_actions",
    "pending_webauthn", "previews", "skill_files", "skills", "support_feedback", "support_thread", "user_prefs",
    "user_security", "user_sessions", "user_sources", "user_directory_groups", "video_jobs", "voice_jobs",
    "webauthn_credentials",
];

/// `local_users._FORBIDDEN_PASSWORDS`.
const FORBIDDEN_PASSWORDS: [&str; 19] = [
    "password", "password1", "password123", "motdepasse", "motdepasse1", "12345678", "123456789", "1234567890",
    "azertyui", "azertyuiop", "qwertyui", "qwertyuiop", "admin123", "administrateur", "changeme", "dgxspark",
    "iloveyou", "soleil123", "00000000",
];

/// `password_policy_error(password, username)`.
pub(crate) fn password_policy_error(password: &str, username: Option<&str>) -> Option<&'static str> {
    if password.chars().count() < 8 {
        return Some("Mot de passe trop court (8 caractères minimum).");
    }
    let p = password.to_lowercase();
    if FORBIDDEN_PASSWORDS.contains(&p.as_str()) {
        return Some("Ce mot de passe est trop courant : choisis-en un autre.");
    }
    if let Some(u) = username.map(str::to_lowercase) {
        if u.chars().count() >= 4 && p.contains(&u) {
            return Some("Le mot de passe ne doit pas contenir l'identifiant.");
        }
    }
    if password.chars().collect::<std::collections::HashSet<_>>().len() < 4 {
        return Some("Mot de passe trop répétitif (trop peu de caractères différents).");
    }
    None
}

/// werkzeug's `generate_password_hash` (scrypt, its default parameters).
pub(crate) async fn password_hash(password: &str) -> Option<String> {
    const SALT_CHARS: &[u8] = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    let mut raw = [0u8; 16];
    getrandom::getrandom(&mut raw).ok()?;
    // 62 symbols: the modulo bias is far below what a salt cares about.
    let salt: String = raw.iter().map(|b| SALT_CHARS[*b as usize % SALT_CHARS.len()] as char).collect();
    let password = password.to_string();
    tokio::task::spawn_blocking(move || {
        let params = scrypt::Params::new(15, 8, 1, 64).ok()?;
        let mut out = [0u8; 64];
        scrypt::scrypt(password.as_bytes(), salt.as_bytes(), &params, &mut out).ok()?;
        Some(format!("scrypt:32768:8:1${salt}${}", hex::encode(out)))
    })
    .await
    .ok()
    .flatten()
}

fn bad(code: StatusCode, msg: &str) -> Response {
    (code, Json(json!({"ok": false, "error": msg}))).into_response()
}

fn truthy_flag(v: Option<&String>) -> bool {
    matches!(v.map(String::as_str), Some("1" | "true" | "on"))
}

/// POST /admin/users/create
pub async fn admin_users_create(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/users/create").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let username = field(&form, "username").to_lowercase();
    let password = form.get("password").cloned().unwrap_or_default();
    if !username_ok(&username) {
        return bad(StatusCode::BAD_REQUEST, "Identifiant invalide (a-z, 0-9, . _ - , max 64).");
    }
    if let Some(e) = password_policy_error(&password, Some(&username)) {
        return bad(StatusCode::BAD_REQUEST, e);
    }
    let pool = state.db.pool();
    if sqlx::query_scalar::<_, i64>("SELECT 1 FROM local_users WHERE username=?").bind(&username).fetch_optional(pool).await.ok().flatten().is_some() {
        return bad(StatusCode::CONFLICT, "Cet utilisateur existe déjà.");
    }
    let group = Some(field(&form, "group")).filter(|g| !g.is_empty());
    if group.is_some() && local_group(pool, group.as_deref()).await.is_none() {
        return bad(StatusCode::BAD_REQUEST, "Groupe inconnu.");
    }
    let budget = match parse_budget(form.get("max_budget").map(String::as_str)) {
        Ok(b) => b,
        Err(e) => return bad(StatusCode::BAD_REQUEST, e),
    };
    let is_admin = truthy_flag(form.get("is_admin"));
    let fullname: String = field(&form, "fullname").chars().take(120).collect();
    let Some(hash) = password_hash(&password).await else {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    };
    let r = sqlx::query(
        "INSERT INTO local_users (username, password_hash, fullname, is_admin, group_name, max_budget, enabled, created_at) VALUES (?,?,?,?,?,?,1,?)",
    )
    .bind(&username)
    .bind(hash)
    .bind((!fullname.is_empty()).then_some(fullname))
    .bind(i64::from(is_admin))
    .bind(&group)
    .bind(budget.map(|b| b as i64))
    .bind(keys::iso(&chrono::Local::now().naive_local()))
    .execute(pool)
    .await;
    if r.is_err() {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let row = locals(pool, "WHERE username=?", Some(&username)).await;
    let quota_ok = match row.first() {
        Some(r) => sync_local_budget(&state, r).await,
        None => false,
    };
    let mut detail = format!("création de {username}");
    if let Some(g) = &group {
        detail += &format!(" (groupe {g})");
    }
    if !quota_ok {
        detail += " — QUOTA NON APPLIQUÉ";
    }
    crate::sessions::log_audit(pool, &s.username, "user.create", &detail).await;
    if quota_ok {
        Json(json!({"ok": true})).into_response()
    } else {
        Json(json!({"ok": true, "warning": "Compte créé, mais son quota n'a PAS pu être appliqué sur LiteLLM : le compte est sans plafond tant que le budget n'est pas redéfini."})).into_response()
    }
}

/// `_admin_removal_refusal(row, action)`: removing this account's admin
/// right would leave nobody to administer the portal locally.
async fn removal_refusal(pool: &SqlitePool, me: &str, row: &Local, action: &str) -> Option<String> {
    if !local_is_admin(pool, row).await {
        return None;
    }
    if row.username == me {
        return Some(format!(
            "Tu ne peux pas te {action} toi-même : tu perdrais immédiatement l'accès à l'administration. Demande-le à un autre administrateur."
        ));
    }
    if local_admins_without(pool, std::slice::from_ref(&row.username)).await == 0 {
        return Some(format!("{} est le dernier administrateur local actif : le portail n'aurait plus personne pour l'administrer en local.", row.username));
    }
    None
}

/// `<int:uid>`: digits only, else werkzeug's 404 before any other check.
fn int_id(raw: &str) -> Option<i64> {
    (!raw.is_empty() && raw.bytes().all(|c| c.is_ascii_digit())).then(|| raw.parse().ok()).flatten()
}

async fn local_by_id(pool: &SqlitePool, uid: i64) -> Option<(Local, bool)> {
    let r: Option<(String, Option<i64>, Option<String>, Option<f64>, Option<i64>)> =
        sqlx::query_as("SELECT username, is_admin, group_name, CAST(max_budget AS REAL), enabled FROM local_users WHERE id=?").bind(uid).fetch_optional(pool).await.ok().flatten();
    r.map(|(username, a, group, max_budget, en)| (Local { username, is_admin: a.unwrap_or(0) != 0, group, max_budget }, en.unwrap_or(0) != 0))
}

/// POST /admin/users/update/<int:uid> - group, quota, rights, state, name.
pub async fn admin_users_update(State(state): State<AppState>, Path(uid): Path<String>, req: Request) -> Response {
    let Some(uid) = int_id(&uid) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let (parts, body) = req.into_parts();
    let Ok(body) = axum::body::to_bytes(body, 4 * 1024 * 1024).await else { return auth::html_status(StatusCode::PAYLOAD_TOO_LARGE) };
    let headers = parts.headers.clone();
    // A new password e-mails the account (notify_password_changed): Flask does it.
    if form_of(&headers, &body).get("password").is_some_and(|p| !p.is_empty()) {
        return crate::relay::to_flask(Request::from_parts(parts, axum::body::Body::from(body))).await;
    }
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/users/update/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let Some((row, _)) = local_by_id(pool, uid).await else {
        return bad(StatusCode::NOT_FOUND, "Utilisateur introuvable.");
    };
    if let Some(r) = owner_protected(&state, &s, &row.username).await {
        return r;
    }
    let mut sets: Vec<&str> = Vec::new();
    let mut vals: Vec<Value> = Vec::new();
    if form.contains_key("group") {
        let group = Some(field(&form, "group")).filter(|g| !g.is_empty());
        let new_group = local_group(pool, group.as_deref()).await;
        if group.is_some() && new_group.is_none() {
            return bad(StatusCode::BAD_REQUEST, "Groupe inconnu.");
        }
        if !(row.is_admin || new_group.is_some_and(|g| g.2)) {
            if let Some(r) = removal_refusal(pool, &s.username, &row, "retirer du groupe administrateur").await {
                return json_error(&r, StatusCode::CONFLICT);
            }
        }
        sets.push("group_name=?");
        vals.push(json!(group));
    }
    if form.contains_key("max_budget") {
        match parse_budget(form.get("max_budget").map(String::as_str)) {
            Ok(b) => {
                sets.push("max_budget=?");
                vals.push(json!(b.map(|b| b as i64)));
            }
            Err(e) => return bad(StatusCode::BAD_REQUEST, e),
        }
    }
    if form.contains_key("is_admin") {
        let new_admin = truthy_flag(form.get("is_admin"));
        if !new_admin {
            if let Some(r) = removal_refusal(pool, &s.username, &row, "rétrograder").await {
                return json_error(&r, StatusCode::CONFLICT);
            }
        }
        sets.push("is_admin=?");
        vals.push(json!(i64::from(new_admin)));
    }
    if form.contains_key("enabled") {
        let active = truthy_flag(form.get("enabled"));
        if !active {
            if let Some(r) = removal_refusal(pool, &s.username, &row, "désactiver").await {
                return json_error(&r, StatusCode::CONFLICT);
            }
        }
        sets.push("enabled=?");
        vals.push(json!(i64::from(active)));
    }
    if form.contains_key("fullname") {
        let f: String = field(&form, "fullname").chars().take(120).collect();
        sets.push("fullname=?");
        vals.push(if f.is_empty() { Value::Null } else { json!(f) });
    }
    if !sets.is_empty() {
        let sql = format!("UPDATE local_users SET {} WHERE id=?", sets.join(", "));
        let mut q = sqlx::query(&sql);
        for v in &vals {
            q = match v {
                Value::Null => q.bind(None::<String>),
                Value::Number(n) => q.bind(n.as_i64()),
                other => q.bind(other.as_str().map(str::to_string)),
            };
        }
        if q.bind(uid).execute(pool).await.is_err() {
            return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
        }
    }
    let Some((updated, enabled)) = local_by_id(pool, uid).await else {
        return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR);
    };
    let disabled = form.contains_key("enabled") && !enabled;
    let mut revoked = 0;
    let mut keys_failed = 0;
    if disabled {
        revoked = sqlx::query("UPDATE user_sessions SET revoked=1 WHERE username=? AND revoked=0").bind(&updated.username).execute(pool).await.map(|r| r.rows_affected()).unwrap_or(0);
        keys_failed = revoke_account_keys(pool, &updated.username, &s.username, "désactivation du compte").await.1;
    }
    // The quota is rewritten only when it may have changed: a resync would erase a running grant.
    let quota_ok = if form.contains_key("group") || form.contains_key("max_budget") || truthy_flag(form.get("enabled")) {
        sync_local_budget(&state, &updated).await
    } else {
        true
    };
    let mut detail = format!("mise à jour de {}", updated.username);
    if disabled {
        detail += &format!(" — compte désactivé, {revoked} session(s) fermée(s)");
    }
    if keys_failed > 0 {
        detail += &format!(" — {keys_failed} clé(s) API NON révoquée(s)");
    }
    if !quota_ok {
        detail += " — QUOTA NON APPLIQUÉ";
    }
    crate::sessions::log_audit(pool, &s.username, "user.update", &detail).await;
    let mut warnings = Vec::new();
    if !quota_ok {
        warnings.push("le quota n'a PAS pu être appliqué sur LiteLLM : redéfinis le budget du compte".to_string());
    }
    if keys_failed > 0 {
        warnings.push(format!("{keys_failed} clé(s) API n'ont PAS pu être révoquées : vérifie côté LiteLLM avant de considérer ce compte comme parti"));
    }
    if warnings.is_empty() {
        Json(json!({"ok": true})).into_response()
    } else {
        Json(json!({"ok": true, "warning": format!("Modifications enregistrées, mais {}.", warnings.join(" ; "))})).into_response()
    }
}

async fn count(pool: &SqlitePool, table: &str, username: &str) -> i64 {
    sqlx::query_scalar(&format!("SELECT COUNT(*) FROM {table} WHERE username=?")).bind(username).fetch_one(pool).await.unwrap_or(0)
}

/// `count_data(username)`: what a deletion will take, shown before confirming.
async fn count_data(pool: &SqlitePool, username: &str) -> serde_json::Map<String, Value> {
    let mut media = 0;
    for t in ["image_jobs", "ocr_jobs", "video_jobs", "voice_jobs"] {
        media += count(pool, t, username).await;
    }
    let mut m = serde_json::Map::new();
    for (k, t) in [
        ("sessions", "user_sessions"),
        ("keys", "api_keys"),
        ("memory_facts", "memory_nodes"),
        ("memory_links", "memory_edges"),
        ("conversations", "conversations"),
        ("shares", "conversation_shares"),
        ("preferences", "user_prefs"),
        ("passkeys", "webauthn_credentials"),
    ] {
        m.insert(k.into(), json!(count(pool, t, username).await));
    }
    m.insert("media_jobs".into(), json!(media));
    m
}

/// `purge_data(username)`: every row attached to the account (not the audit
/// log, not the block), then its login attempts. The rows deleted.
async fn purge_data(pool: &SqlitePool, username: &str) -> u64 {
    let mut total = 0;
    for t in PURGED_TABLES {
        if let Ok(r) = sqlx::query(&format!("DELETE FROM {t} WHERE username=?")).bind(username).execute(pool).await {
            total += r.rows_affected();
        }
    }
    let like = username.replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_");
    if let Ok(r) = sqlx::query("DELETE FROM login_attempts WHERE key=? OR key LIKE ? ESCAPE '\\'")
        .bind(format!("user:{username}"))
        .bind(format!("%|{like}"))
        .execute(pool)
        .await
    {
        total += r.rows_affected();
    }
    total
}

/// `deprovision_account(username, by, action)`: keys, sessions, the LiteLLM
/// envelope, then the data — the report, audited.
async fn deprovision(pool: &SqlitePool, username: &str, by: &str, action: &str) -> Value {
    let mut report = count_data(pool, username).await;
    let (ok, failed, total) = revoke_keys(pool, username).await;
    let sessions = sqlx::query("UPDATE user_sessions SET revoked=1 WHERE username=? AND revoked=0").bind(username).execute(pool).await.map(|r| r.rows_affected()).unwrap_or(0);
    let envelope = super::key_routes::litellm_post("/user/delete", json!({"user_ids": [username]}), 8).await.is_some_and(|r| r.status().is_success());
    let purged = purge_data(pool, username).await;
    report.insert("keys_revoked".into(), json!(ok));
    report.insert("keys_failed".into(), json!(failed));
    report.insert("keys".into(), json!(total));
    report.insert("sessions".into(), json!(sessions));
    report.insert("litellm_user".into(), json!(envelope));
    report.insert("purged_rows".into(), json!(purged));
    let mut detail = format!("{username} — {ok}/{total} clé(s) révoquée(s)");
    if failed > 0 {
        detail += &format!(", {failed} ÉCHEC(S)");
    }
    detail += &format!(
        ", {sessions} session(s), enveloppe LiteLLM {}, {purged} ligne(s) de données purgée(s)",
        if envelope { "supprimée" } else { "NON supprimée" }
    );
    crate::sessions::log_audit(pool, if by.is_empty() { "système" } else { by }, action, &detail).await;
    Value::Object(report)
}

fn confirmed(headers: &HeaderMap, body: &axum::body::Bytes) -> bool {
    body_fields(headers, body).get("confirm").is_some_and(|c| crate::memory::py_strip(c).to_uppercase() == "DELETE")
}

/// POST /admin/users/delete/<int:uid> - access first, then the data.
pub async fn admin_users_delete(State(state): State<AppState>, Path(uid): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let Some(uid) = int_id(&uid) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/users/delete/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let Some((row, _)) = local_by_id(pool, uid).await else {
        return bad(StatusCode::NOT_FOUND, "Utilisateur introuvable.");
    };
    if row.username == s.username {
        return bad(StatusCode::BAD_REQUEST, "Tu ne peux pas supprimer ton propre compte.");
    }
    if let Some(r) = owner_protected(&state, &s, &row.username).await {
        return r;
    }
    if local_is_admin(pool, &row).await && super::admin_writes::last_local_admin(pool, &row.username).await {
        return bad(StatusCode::BAD_REQUEST, "Dernier administrateur local : nomme un autre administrateur avant de supprimer celui-ci.");
    }
    if !confirmed(&headers, &body) {
        let data = count_data(pool, &row.username).await;
        return (
            StatusCode::CONFLICT,
            Json(json!({"ok": false, "needs_confirm": true, "data": data, "error": "Confirmation requise : cette suppression emporte les accès ET les données du compte."})),
        )
            .into_response();
    }
    let _ = sqlx::query("DELETE FROM local_users WHERE id=?").bind(uid).execute(pool).await;
    let report = deprovision(pool, &row.username, &s.username, "user.delete").await;
    let mut out = json!({"ok": true, "purged": report});
    if report["keys_failed"].as_u64().unwrap_or(0) > 0 || report["litellm_user"] != json!(true) {
        out["warning"] = json!(format!(
            "{} clé(s) n'ont pas pu être révoquées et/ou l'enveloppe LiteLLM n'a pas pu être supprimée (service injoignable) : vérifie les clés de ce compte dans LiteLLM avant de le considérer comme parti.",
            report["keys_failed"]
        ));
    }
    Json(out).into_response()
}

/// POST /admin/users/<username>/purge - a directory account's data, its access untouched.
pub async fn admin_user_purge(State(state): State<AppState>, Path(username): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/users/x/purge").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    if let Some(r) = owner_protected(&state, &s, &username).await {
        return r;
    }
    if !username_ok(&username) {
        return bad(StatusCode::BAD_REQUEST, "Nom d'utilisateur invalide.");
    }
    if username == s.username {
        return bad(StatusCode::BAD_REQUEST, "Tu ne peux pas purger ton propre compte.");
    }
    let pool = state.db.pool();
    if sqlx::query_scalar::<_, i64>("SELECT 1 FROM local_users WHERE username=?").bind(&username).fetch_optional(pool).await.ok().flatten().is_some() {
        return bad(StatusCode::BAD_REQUEST, "Ce compte est local : utilise la suppression, qui retire aussi l'accès.");
    }
    if !confirmed(&headers, &body) {
        let data = count_data(pool, &username).await;
        return (
            StatusCode::CONFLICT,
            Json(json!({"ok": false, "needs_confirm": true, "data": data, "error": "Confirmation requise : cette opération efface définitivement les données du compte."})),
        )
            .into_response();
    }
    let report = deprovision(pool, &username, &s.username, "user.purge").await;
    Json(json!({"ok": true, "purged": report})).into_response()
}

/// `password_management(username)['management']`: who holds the password.
async fn management(pool: &SqlitePool, username: &str) -> (&'static str, bool) {
    let local = sqlx::query_scalar::<_, i64>("SELECT 1 FROM local_users WHERE username=?").bind(username).fetch_optional(pool).await.ok().flatten().is_some();
    let sources: Option<Option<String>> = sqlx::query_scalar("SELECT sources FROM user_sources WHERE username=?").bind(username).fetch_optional(pool).await.ok().flatten();
    let sources = sources.flatten().unwrap_or_default();
    let has = |s: &str| sources.split(',').any(|x| x == s);
    let m = if local {
        "portal"
    } else if has("ldap") {
        "ldap-directory"
    } else if has("sso") {
        "sso-provider"
    } else {
        "unknown"
    };
    (m, local)
}

/// POST /api/account/delete - one's own account. A local account goes
/// entirely (keys, envelope, sessions, data), on its password; a directory
/// account's data is purged (its access lives in the directory). An LDAP
/// account's password can only be checked by Flask: it is handed over.
pub async fn api_account_delete(State(state): State<AppState>, req: Request) -> Response {
    let (parts, body) = req.into_parts();
    let Ok(body) = axum::body::to_bytes(body, 4 * 1024 * 1024).await else { return auth::html_status(StatusCode::PAYLOAD_TOO_LARGE) };
    let headers = parts.headers.clone();
    let form = form_of(&headers, &body);
    match auth::csrf_ok(&headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::csrf_refused(),
        Ok(true) => {}
    }
    let s = match auth::require_session(&headers, &state.db).await {
        Ok(s) => s,
        Err(e) => return e.into_response(),
    };
    let pool = state.db.pool();
    let (how, local) = management(pool, &s.username).await;
    if how == "ldap-directory" {
        return crate::relay::to_flask(Request::from_parts(parts, axum::body::Body::from(body))).await;
    }
    let data = body_fields(&headers, &body);
    if !data.get("confirm").is_some_and(|c| crate::memory::py_strip(c).to_uppercase() == "DELETE") {
        let counts = count_data(pool, &s.username).await;
        return (
            StatusCode::CONFLICT,
            Json(json!({"ok": false, "needs_confirm": true, "data": counts, "error": "Confirmation requise : cette suppression est définitive."})),
        )
            .into_response();
    }
    if local && s.is_admin && super::admin_writes::last_local_admin(pool, &s.username).await {
        return bad(StatusCode::CONFLICT, "Tu es le dernier administrateur local : nomme un autre administrateur avant de supprimer ton compte.");
    }
    if how == "portal" {
        let password = data.get("password").cloned().unwrap_or_default();
        if password.is_empty() {
            return bad(StatusCode::BAD_REQUEST, "Mot de passe requis pour supprimer ton compte.");
        }
        // `_verify_password_locked`: the account's counter, shared with /login.
        let ukey = format!("user:{}", s.username);
        let wait = crate::login::locked(pool, &ukey).await;
        if wait > 0 {
            return (StatusCode::TOO_MANY_REQUESTS, Json(json!({"error": format!("Trop de tentatives. Réessaie dans {} min.", wait / 60 + 1)}))).into_response();
        }
        let hash: Option<String> = sqlx::query_scalar("SELECT password_hash FROM local_users WHERE username=? AND enabled=1").bind(&s.username).fetch_optional(pool).await.ok().flatten();
        let ok = match hash {
            Some(h) => tokio::task::spawn_blocking(move || crate::login::check_password_hash(&h, &password)).await.unwrap_or(false),
            None => false,
        };
        if !ok {
            crate::login::fail(pool, &ukey).await;
            return (StatusCode::BAD_REQUEST, Json(json!({"error": "Mot de passe incorrect."}))).into_response();
        }
        crate::login::reset(pool, &ukey).await;
    }
    if local {
        let _ = sqlx::query("DELETE FROM local_users WHERE username=?").bind(&s.username).execute(pool).await;
        let report = deprovision(pool, &s.username, &s.username, "account.self_delete").await;
        let mut out = json!({"ok": true, "deleted": true, "purged": report});
        if report["keys_failed"].as_u64().unwrap_or(0) > 0 || report["litellm_user"] != json!(true) {
            out["warning"] = json!(format!(
                "{} clé(s) n'ont pas pu être révoquées et/ou l'enveloppe LiteLLM n'a pas pu être supprimée (service injoignable) : préviens un administrateur, ces clés peuvent encore fonctionner.",
                report["keys_failed"]
            ));
        }
        return Json(out).into_response();
    }
    let report = deprovision(pool, &s.username, &s.username, "account.self_purge").await;
    Json(json!({"ok": true, "deleted": false, "purged": report})).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn password_policy_follows_flask() {
        assert!(password_policy_error("short", None).unwrap().contains("court"));
        assert!(password_policy_error("Password1", None).unwrap().contains("courant"));
        assert!(password_policy_error("xxalicexx9", Some("alice")).unwrap().contains("identifiant"));
        assert!(password_policy_error("aaaaabbbbb", None).unwrap().contains("répétitif"));
        assert_eq!(password_policy_error("corr3ct-h0rse", Some("bob")), None);
    }

    #[tokio::test]
    async fn hashes_verify_with_the_login_check() {
        let h = password_hash("corr3ct-h0rse").await.unwrap();
        assert!(h.starts_with("scrypt:32768:8:1$"));
        assert!(crate::login::check_password_hash(&h, "corr3ct-h0rse"));
        assert!(!crate::login::check_password_hash(&h, "wrong"));
    }
}
