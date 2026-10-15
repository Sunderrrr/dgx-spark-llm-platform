//! Admin writes that touch the database only (admin_routes.py,
//! admin_models.py, admin_users.py): announcements, the global key cap, the
//! OCR and voice catalogs, session revocation and unblocking. Compared to Flask
//! on the parity bench (both databases restored from one seed, then diffed).
use super::*;
use super::admin_routes::admin_guard;
use std::collections::HashMap;

/// `local_users.MAX_BUDGET`.
const MAX_BUDGET: f64 = 1e12;

/// `sidecars.VOICE_REPO_IDS`: the voice variants the catalog may name.
const VOICE_REPO_IDS: [&str; 6] = [
    "kyutai/tts-1.6b-en_fr",
    "Qwen3-TTS-12Hz-1.7B-Base",
    "Qwen3-TTS-12Hz-0.6B-Base",
    "chatterbox-multilingual",
    "chatterbox-turbo",
    "chatterbox",
];

/// CSRF first (Flask's `before_request`), then `admin_required`; the form.
pub(crate) async fn admin_form(state: &AppState, headers: &HeaderMap, body: &axum::body::Bytes, path: &str) -> Result<(auth::SessionData, HashMap<String, String>), Response> {
    let form = form_of(headers, body);
    match auth::csrf_ok(headers, form.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return Err(e.into_response()),
        Ok(false) => return Err(auth::csrf_refused()),
        Ok(true) => {}
    }
    let s = admin_guard(state, headers, path).await?;
    Ok((s, form))
}

/// `_json_ok(message, **extra)`.
pub(crate) fn json_ok(message: &str, extra: Value) -> Response {
    let mut body = json!({"ok": true, "message": message});
    if let (Some(b), Some(e)) = (body.as_object_mut(), extra.as_object()) {
        b.extend(e.clone());
    }
    Json(body).into_response()
}

/// `_json_error(message, code)`.
pub(crate) fn json_error(message: &str, code: StatusCode) -> Response {
    (code, Json(json!({"ok": false, "error": message}))).into_response()
}

/// `request.form.get(key, '').strip()`.
fn field(form: &HashMap<String, String>, key: &str) -> String {
    crate::memory::py_strip(form.get(key).map(String::as_str).unwrap_or("")).to_string()
}

/// `re.sub(r'[^a-zA-Z0-9_-]', '-', name)[:40]`: a catalog name.
fn catalog_name(raw: &str) -> String {
    raw.chars().map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '-' { c } else { '-' }).take(40).collect()
}

/// `datetime.now().isoformat()`.
fn local_now() -> String {
    keys::iso(&chrono::Local::now().naive_local())
}

/// Python's `float(s)`: surrounding whitespace allowed, `_` between digits,
/// `inf`/`nan` accepted (and refused later by the range check).
pub(crate) fn parse_py_float(s: &str) -> Option<f64> {
    let t = crate::memory::py_strip(s);
    if t.contains('_') {
        let b = t.as_bytes();
        let ok = b.iter().enumerate().all(|(i, &c)| c != b'_' || (i > 0 && i + 1 < b.len() && b[i - 1].is_ascii_digit() && b[i + 1].is_ascii_digit()));
        return if ok { t.replace('_', "").parse().ok() } else { None };
    }
    t.parse().ok()
}

/// `USERNAME_RE`.
pub(crate) fn username_ok(u: &str) -> bool {
    (1..=64).contains(&u.len()) && u.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
}

/// `_owner_protected(username)`: the owner is out of other admins' reach.
pub(crate) async fn owner_protected(state: &AppState, session: &auth::SessionData, username: &str) -> Option<Response> {
    let owner = state.db.get_setting_set("owner_username").await;
    (owner.as_deref() == Some(username) && session.username != username).then(|| {
        json_error("Ce compte est le propriétaire de l'instance : seul lui-même peut le modifier.", StatusCode::FORBIDDEN)
    })
}

/// `announcements.add_announcement`: the card shown at the site's opening,
/// then the Discord DM to the linked accounts. Failures are swallowed.
pub(crate) async fn add_announcement(pool: &sqlx::SqlitePool, kind: &str, a: &str, b: &str) {
    let _ = sqlx::query("INSERT INTO announcements (kind, a, b, created_at) VALUES (?,?,?,?)")
        .bind(kind)
        .bind(a)
        .bind(b)
        .bind(local_now())
        .execute(pool)
        .await;
    crate::discord::announce(pool, kind, a, b).await;
}

/// POST /admin/announce
pub async fn admin_announce(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/announce").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let title: String = field(&form, "title").chars().take(120).collect();
    let text: String = field(&form, "body").chars().take(600).collect();
    if title.is_empty() {
        return json_error("Titre requis pour l'annonce.", StatusCode::BAD_REQUEST);
    }
    add_announcement(state.db.pool(), "site", &title, &text).await;
    crate::sessions::log_audit(state.db.pool(), &s.username, "announce", &format!("annonce : {title}")).await;
    json_ok("Annonce publiée — elle s'affichera à l'ouverture du site.", json!({}))
}

/// POST /admin/settings - the global key cap and its period.
pub async fn admin_settings(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/settings").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let budget = field(&form, "default_key_budget");
    let duration = field(&form, "default_key_duration");
    // `not (0 < v <= MAX_BUDGET)`: NaN fails every comparison.
    let Some(v) = parse_py_float(&budget).filter(|v| *v > 0.0 && *v <= MAX_BUDGET) else {
        return json_error("Le nombre de tokens par défaut doit être un nombre positif, au plus 1e+12.", StatusCode::BAD_REQUEST);
    };
    // `^\d+[smhd]$`
    let unit_ok = duration.strip_suffix(['s', 'm', 'h', 'd']).is_some_and(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()));
    if !unit_ok {
        return json_error("Durée invalide (ex: 1d, 7d, 30d, 12h).", StatusCode::BAD_REQUEST);
    }
    let old = state.db.get_setting("default_key_budget").await.ok().flatten();
    let pool = state.db.pool();
    for (k, val) in [("default_key_budget", super::support_routes::py_float(v)), ("default_key_duration", duration.clone())] {
        let _ = sqlx::query("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(k)
            .bind(val)
            .execute(pool)
            .await;
    }
    let old = old.unwrap_or_else(|| "None".into());
    crate::sessions::log_audit(pool, &s.username, "settings.budget", &format!("plafond global {old} → {v:.0} tokens / {duration}")).await;
    json_ok(&format!("Limite globale mise à jour : {} tokens / {duration}.", crate::chat::support_tools::grouped(v)), json!({}))
}

/// POST /admin/ocr/catalog/add
pub async fn admin_ocr_catalog_add(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/ocr/catalog/add").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let name = catalog_name(&field(&form, "name"));
    let hf_id = field(&form, "hf_model_id");
    let args = field(&form, "vllm_args");
    if name.is_empty() || hf_id.is_empty() {
        return json_error("Nom et HF model ID requis.", StatusCode::BAD_REQUEST);
    }
    let r = sqlx::query("INSERT INTO ocr_configs (name, hf_model_id, vllm_args, added_at) VALUES (?,?,?,?)")
        .bind(&name)
        .bind(&hf_id)
        .bind(&args)
        .bind(local_now())
        .execute(state.db.pool())
        .await;
    match r {
        Err(sqlx::Error::Database(e)) if e.is_unique_violation() => return json_error("Un modèle OCR avec ce nom existe déjà.", StatusCode::CONFLICT),
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
        Ok(_) => {}
    }
    crate::sessions::log_audit(state.db.pool(), &s.username, "ocr.catalog.add", &format!("{name} ({hf_id})")).await;
    json_ok(&format!("Modèle OCR {name} ajouté au catalogue."), json!({}))
}

/// POST /admin/voice/catalog/add
pub async fn admin_voice_catalog_add(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/voice/catalog/add").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let name = catalog_name(&field(&form, "name"));
    let repo_id = field(&form, "repo_id");
    if name.is_empty() || !VOICE_REPO_IDS.contains(&repo_id.as_str()) {
        return json_error("Nom et variante requis (variante hors liste autorisée).", StatusCode::BAD_REQUEST);
    }
    let r = sqlx::query("INSERT INTO voice_configs (name, repo_id, added_at) VALUES (?,?,?)")
        .bind(&name)
        .bind(&repo_id)
        .bind(local_now())
        .execute(state.db.pool())
        .await;
    match r {
        Err(sqlx::Error::Database(e)) if e.is_unique_violation() => return json_error("Un modèle voix avec ce nom existe déjà.", StatusCode::CONFLICT),
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
        Ok(_) => {}
    }
    crate::sessions::log_audit(state.db.pool(), &s.username, "voice.catalog.add", &format!("{name} ({repo_id})")).await;
    json_ok(&format!("Modèle voix {name} ajouté au catalogue."), json!({}))
}

/// The OCR and voice catalog deletions: (table, audit action, not-found text, done text).
fn catalog_kind(kind: &str) -> Option<(&'static str, &'static str, &'static str, &'static str)> {
    match kind {
        "ocr" => Some(("ocr_configs", "ocr.catalog.delete", "Modèle OCR introuvable.", "Modèle OCR supprimé du catalogue.")),
        "voice" => Some(("voice_configs", "voice.catalog.delete", "Modèle voix introuvable.", "Modèle voix supprimé du catalogue.")),
        _ => None,
    }
}

/// POST /admin/{ocr,voice}/catalog/delete/<int:cid>
pub async fn admin_catalog_delete(State(state): State<AppState>, Path((kind, cid)): Path<(String, String)>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    // `<int:cid>`: anything but digits is not this route (werkzeug's 404, before any check).
    let digits = !cid.is_empty() && cid.bytes().all(|c| c.is_ascii_digit());
    let (Some((table, action, missing, done)), Some(cid)) = (catalog_kind(&kind), cid.parse::<i64>().ok().filter(|_| digits)) else {
        return auth::html_status(StatusCode::NOT_FOUND);
    };
    let (s, _) = match admin_form(&state, &headers, &body, &format!("/admin/{kind}/catalog/delete/{cid}")).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let name: Option<String> = match sqlx::query_scalar(&format!("SELECT name FROM {table} WHERE id=?")).bind(cid).fetch_optional(pool).await {
        Ok(n) => n,
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
    };
    let Some(name) = name else {
        return json_error(missing, StatusCode::NOT_FOUND);
    };
    let _ = sqlx::query(&format!("DELETE FROM {table} WHERE id=?")).bind(cid).execute(pool).await;
    crate::sessions::log_audit(pool, &s.username, action, &name).await;
    json_ok(done, json!({}))
}

/// POST /admin/users/<username>/revoke-sessions
pub async fn admin_revoke_sessions(State(state): State<AppState>, Path(username): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/users/x/revoke-sessions").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    if let Some(r) = owner_protected(&state, &s, &username).await {
        return r;
    }
    if !username_ok(&username) {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Nom d'utilisateur invalide."}))).into_response();
    }
    let pool = state.db.pool();
    let n = sqlx::query("UPDATE user_sessions SET revoked=1 WHERE username=? AND revoked=0").bind(&username).execute(pool).await.map(|r| r.rows_affected()).unwrap_or(0);
    crate::sessions::log_audit(pool, &s.username, "session.revoke", &format!("{username} — {n} session(s) révoquée(s)")).await;
    Json(json!({"ok": true, "revoked": n})).into_response()
}

/// POST /admin/users/<username>/unblock
pub async fn admin_unblock(State(state): State<AppState>, Path(username): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/users/x/unblock").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    if !username_ok(&username) {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Nom d'utilisateur invalide."}))).into_response();
    }
    let pool = state.db.pool();
    let n = sqlx::query("DELETE FROM blocked_users WHERE username=?").bind(&username).execute(pool).await.map(|r| r.rows_affected()).unwrap_or(0);
    if n > 0 {
        crate::sessions::log_audit(pool, &s.username, "user.unblock", &username).await;
    }
    Json(json!({"ok": true, "unblocked": n > 0})).into_response()
}

/// `_local_user_is_admin(row)`: its own flag, or its group's.
async fn local_is_admin(pool: &sqlx::SqlitePool, own: bool, group: Option<&str>) -> bool {
    if own {
        return true;
    }
    let Some(g) = group.filter(|g| !g.is_empty()) else { return false };
    sqlx::query_scalar::<_, Option<i64>>("SELECT is_admin FROM user_groups WHERE name=?").bind(g).fetch_optional(pool).await.ok().flatten().flatten().unwrap_or(0) != 0
}

/// `_last_local_admin(username)`: no other enabled, unblocked local admin.
async fn last_local_admin(pool: &sqlx::SqlitePool, username: &str) -> bool {
    let rows: Vec<(String, Option<i64>, Option<String>)> =
        sqlx::query_as("SELECT username, is_admin, group_name FROM local_users WHERE enabled=1 AND username NOT IN (SELECT username FROM blocked_users)")
            .fetch_all(pool)
            .await
            .unwrap_or_default();
    for (u, own, group) in rows {
        if u != username && local_is_admin(pool, own.unwrap_or(0) != 0, group.as_deref()).await {
            return false;
        }
    }
    true
}

/// `_body()`: the JSON object of a JSON request, else the form.
fn body_fields(headers: &HeaderMap, body: &axum::body::Bytes) -> HashMap<String, String> {
    let is_json = headers.get(axum::http::header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).is_some_and(|v| v.starts_with("application/json") || v.contains("+json"));
    if is_json {
        if let Ok(Value::Object(m)) = serde_json::from_slice::<Value>(body) {
            return m.into_iter().filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string()))).collect();
        }
    }
    form_of(headers, body)
}

/// `user_lifecycle.revoke_account_keys`: every key revoked at LiteLLM, the
/// rows forgotten whatever happened, the result audited. (ok, failed, total).
pub(crate) async fn revoke_account_keys(pool: &sqlx::SqlitePool, username: &str, by: &str, reason: &str) -> (usize, usize, usize) {
    let values: Vec<String> = sqlx::query_scalar("SELECT key_value FROM api_keys WHERE username=?").bind(username).fetch_all(pool).await.unwrap_or_default();
    let mut ok = 0;
    for v in &values {
        if super::key_routes::revoke_key(v).await {
            ok += 1;
        }
    }
    let failed = values.len() - ok;
    let _ = sqlx::query("DELETE FROM api_keys WHERE username=?").bind(username).execute(pool).await;
    let by = if by.is_empty() { "?" } else { by };
    let mut detail = format!("{username} — {ok}/{} clé(s) révoquée(s)", values.len());
    if failed > 0 {
        detail += &format!(", {failed} ÉCHEC(S) À RÉVOQUER");
    }
    if !reason.is_empty() {
        detail += &format!(" — {reason}");
    }
    crate::sessions::log_audit(pool, by, "user.keys_revoked", &detail).await;
    (ok, failed, values.len())
}

/// POST /admin/users/<username>/block - refused at login whatever its source;
/// its sessions and API keys are cut.
pub async fn admin_block(State(state): State<AppState>, Path(username): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/users/x/block").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let bad = |msg: &str| (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": msg}))).into_response();
    if !username_ok(&username) {
        return bad("Nom d'utilisateur invalide.");
    }
    if let Some(r) = owner_protected(&state, &s, &username).await {
        return r;
    }
    if username == s.username {
        return bad("Tu ne peux pas bloquer ton propre compte.");
    }
    let pool = state.db.pool();
    let blocked = sqlx::query_scalar::<_, i64>("SELECT 1 FROM blocked_users WHERE username=?").bind(&username).fetch_optional(pool).await.ok().flatten().is_some();
    if blocked {
        return Json(json!({"ok": true, "revoked_sessions": 0, "already_blocked": true})).into_response();
    }
    let row: Option<(Option<i64>, Option<String>)> = sqlx::query_as("SELECT is_admin, group_name FROM local_users WHERE username=?").bind(&username).fetch_optional(pool).await.ok().flatten();
    if let Some((own, group)) = row {
        if local_is_admin(pool, own.unwrap_or(0) != 0, group.as_deref()).await && last_local_admin(pool, &username).await {
            return bad("Dernier administrateur local : nomme un autre administrateur avant de bloquer celui-ci.");
        }
    }
    let fields = body_fields(&headers, &body);
    let reason: String = crate::memory::py_strip(fields.get("reason").map(String::as_str).unwrap_or("")).chars().take(200).collect();
    // auth.block_account
    let by = if s.username.is_empty() { "?" } else { s.username.as_str() };
    let _ = sqlx::query(
        "INSERT INTO blocked_users (username, reason, blocked_by, blocked_at) VALUES (?,?,?,?) \
         ON CONFLICT(username) DO UPDATE SET reason=excluded.reason, blocked_by=excluded.blocked_by, blocked_at=excluded.blocked_at",
    )
    .bind(&username)
    .bind((!reason.is_empty()).then_some(reason.as_str()))
    .bind(by)
    .bind(local_now())
    .execute(pool)
    .await;
    let revoked = sqlx::query("UPDATE user_sessions SET revoked=1 WHERE username=? AND revoked=0").bind(&username).execute(pool).await.map(|r| r.rows_affected()).unwrap_or(0);
    let (keys_ok, keys_failed, keys_total) = revoke_account_keys(pool, &username, &s.username, "blocage du compte").await;
    let mut detail = username.clone();
    if !reason.is_empty() {
        detail += &format!(" — motif : {reason}");
    }
    detail += &format!(" — {revoked} session(s) révoquée(s), {keys_ok}/{keys_total} clé(s) API révoquée(s)");
    if keys_failed > 0 {
        detail += &format!(", {keys_failed} ÉCHEC(S) À RÉVOQUER");
    }
    crate::sessions::log_audit(pool, by, "user.block", &detail).await;
    let mut out = json!({"ok": true, "revoked_sessions": revoked, "revoked_keys": keys_ok, "keys_total": keys_total});
    if keys_failed > 0 {
        out["warning"] = json!(format!(
            "{keys_failed} clé(s) API n'ont PAS pu être révoquées — vérifie côté LiteLLM avant de considérer ce compte comme parti."
        ));
    }
    Json(out).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_float_parsing() {
        assert_eq!(parse_py_float(" 1e6 "), Some(1e6));
        assert_eq!(parse_py_float("1_000"), Some(1000.0));
        assert_eq!(parse_py_float("_1"), None);
        assert_eq!(parse_py_float("1__0"), None);
        assert!(parse_py_float("nan").is_some_and(f64::is_nan));
        assert_eq!(parse_py_float("abc"), None);
    }

    #[test]
    fn catalog_names_are_sanitized() {
        assert_eq!(catalog_name("my model/é"), "my-model--");
        assert_eq!(catalog_name(&"a".repeat(50)).len(), 40);
    }

    #[test]
    fn usernames() {
        assert!(username_ok("alice.b-c_d"));
        assert!(!username_ok("a b"));
        assert!(!username_ok(""));
        assert!(!username_ok(&"a".repeat(65)));
    }
}
