//! Account groups (admin_users.py, local_users.py, directory_groups.py): an
//! admin creates, edits or deletes a group, and its members follow at once —
//! the local members' LiteLLM cap, the directory members' admin right and
//! group quota. Compared on the parity bench, LiteLLM calls included.
use super::*;
use super::admin_writes::{admin_form, json_error, parse_py_float};
use sqlx::SqlitePool;

/// `local_users.MAX_BUDGET`.
const MAX_BUDGET: f64 = 1e12;

/// A `user_groups` row: (name, max_budget, is_admin).
type Group = (String, Option<f64>, bool);

/// `_local_group(name)`: the group of exactly that name.
async fn local_group(pool: &SqlitePool, name: Option<&str>) -> Option<Group> {
    let name = name.filter(|n| !n.is_empty())?;
    sqlx::query_as::<_, (String, Option<f64>, Option<i64>)>("SELECT name, CAST(max_budget AS REAL), is_admin FROM user_groups WHERE name=?")
        .bind(name)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .map(|(n, b, a)| (n, b, a.unwrap_or(0) != 0))
}

/// A `local_users` row as the quota and rights helpers read it.
#[derive(Clone)]
struct Local {
    username: String,
    is_admin: bool,
    group: Option<String>,
    max_budget: Option<f64>,
}

async fn locals(pool: &SqlitePool, filter: &str, arg: Option<&str>) -> Vec<Local> {
    let sql = format!("SELECT username, is_admin, group_name, CAST(max_budget AS REAL) FROM local_users {filter}");
    let mut q = sqlx::query_as::<_, (String, Option<i64>, Option<String>, Option<f64>)>(&sql);
    if let Some(a) = arg {
        q = q.bind(a);
    }
    q.fetch_all(pool)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(|(username, a, group, max_budget)| Local { username, is_admin: a.unwrap_or(0) != 0, group, max_budget })
        .collect()
}

/// `_local_user_is_admin(row)`.
async fn local_is_admin(pool: &SqlitePool, row: &Local) -> bool {
    row.is_admin || local_group(pool, row.group.as_deref()).await.is_some_and(|g| g.2)
}

/// `float(get_setting('default_key_budget', KEY_BUDGET))`.
async fn default_budget(state: &AppState) -> f64 {
    let fallback = super::admin_routes::key_budget_default().as_f64().unwrap_or(200_000_000.0);
    state.db.get_setting("default_key_budget").await.ok().flatten().and_then(|v| parse_py_float(&v)).unwrap_or(fallback)
}

/// `get_setting('default_key_duration', KEY_DURATION)`.
async fn default_duration(state: &AppState) -> String {
    match state.db.get_setting("default_key_duration").await.ok().flatten() {
        Some(v) => v,
        None => super::admin_routes::key_duration_default().as_str().unwrap_or("7d").to_string(),
    }
}

/// `litellm_update_user_budget(username, amount, budget_duration)`.
async fn update_envelope(username: &str, amount: f64, duration: &str) -> bool {
    let mut payload = json!({"user_id": username, "max_budget": amount});
    if !duration.is_empty() {
        payload["budget_duration"] = json!(duration);
    }
    super::key_routes::litellm_post("/user/update", payload, 5).await.is_some_and(|r| r.status().is_success())
}

/// `_sync_local_user_budget(username, row)`: the account's effective quota
/// (its own, else its group's, else the default) written at LiteLLM.
async fn sync_local_budget(state: &AppState, row: &Local) -> bool {
    let pool = state.db.pool();
    let eff = match row.max_budget {
        Some(b) => b,
        None => match local_group(pool, row.group.as_deref()).await.and_then(|g| g.1) {
            Some(b) => b,
            None => default_budget(state).await,
        },
    };
    let duration = default_duration(state).await;
    super::key_routes::ensure_user(&row.username, eff, &duration).await;
    update_envelope(&row.username, eff, &duration).await
}

/// `instance_config.setting('oidc_admin_group')`: stored, else « admins ».
async fn oidc_admin_group(state: &AppState) -> String {
    let v = state.db.get_setting("cfg_oidc_admin_group").await.ok().flatten().filter(|v| !v.is_empty());
    v.unwrap_or_else(|| "admins".into())
}

/// `rights_from_groups(username)`: (admin, most generous quota) the
/// account's directory groups grant.
async fn rights_from_groups(state: &AppState, username: &str) -> (bool, Option<f64>) {
    let groups: Vec<(String, Option<f64>, Option<i64>)> = sqlx::query_as(
        "SELECT g.name, CAST(g.max_budget AS REAL), g.is_admin FROM user_groups g \
         WHERE g.name IN (SELECT group_name FROM user_directory_groups WHERE username=?) ORDER BY g.name",
    )
    .bind(username)
    .fetch_all(state.db.pool())
    .await
    .unwrap_or_default();
    let admin_group = crate::memory::py_strip(&oidc_admin_group(state).await).to_lowercase();
    let is_admin = groups.iter().any(|(n, _, a)| a.unwrap_or(0) != 0 || (!admin_group.is_empty() && n.to_lowercase() == admin_group));
    let quota = groups.iter().filter_map(|g| g.1).fold(None, |m: Option<f64>, q| Some(m.map_or(q, |m| m.max(q))));
    (is_admin, quota)
}

/// `apply_group_quota(username)`: the group quota as this directory
/// account's cap, unless set by hand or a grant runs. Never fails.
async fn apply_group_quota(state: &AppState, username: &str) {
    let pool = state.db.pool();
    if sqlx::query_scalar::<_, i64>("SELECT 1 FROM local_users WHERE username=?").bind(username).fetch_optional(pool).await.ok().flatten().is_some() {
        return;
    }
    let rs: Option<(Option<i64>, Option<f64>)> =
        sqlx::query_as("SELECT budget_manual, CAST(budget_auto AS REAL) FROM user_sources WHERE username=?").bind(username).fetch_optional(pool).await.ok().flatten();
    let Some((manual, previous)) = rs else { return };
    if manual.unwrap_or(0) != 0 {
        return;
    }
    let now = keys::iso(&chrono::Utc::now().naive_utc());
    if sqlx::query_scalar::<_, i64>("SELECT 1 FROM budget_grants WHERE username=? AND expires_at > ?").bind(username).bind(now).fetch_optional(pool).await.ok().flatten().is_some() {
        return;
    }
    let (_, quota) = rights_from_groups(state, username).await;
    let target = match (quota, previous) {
        (Some(q), _) => q,
        (None, Some(_)) => default_budget(state).await, // left its quota group(s)
        (None, None) => return,
    };
    if previous == Some(target) {
        return;
    }
    let duration = default_duration(state).await;
    super::key_routes::ensure_user(username, target, &duration).await;
    if !update_envelope(username, target, &duration).await {
        return;
    }
    let _ = sqlx::query("UPDATE user_sources SET budget_auto=? WHERE username=?").bind(quota.map(|_| target)).bind(username).execute(pool).await;
}

/// `recompute_members(group)`: its directory members' right and quota follow now.
async fn recompute_members(state: &AppState, group: &str) {
    let pool = state.db.pool();
    let members: Vec<String> = sqlx::query_scalar(
        "SELECT DISTINCT d.username FROM user_directory_groups d LEFT JOIN local_users l ON l.username = d.username \
         WHERE lower(d.group_name)=lower(?) AND l.username IS NULL",
    )
    .bind(group)
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    for u in members {
        let (is_admin, _) = rights_from_groups(state, &u).await;
        let _ = sqlx::query("UPDATE user_sources SET last_is_admin=? WHERE username=?").bind(i64::from(is_admin)).bind(&u).execute(pool).await;
        apply_group_quota(state, &u).await;
    }
}

/// `_local_admins_after({member: (0, None)})`: the enabled, unblocked local
/// admins left once these accounts lose every admin right.
async fn local_admins_without(pool: &SqlitePool, removed: &[String]) -> usize {
    let mut n = 0;
    for r in locals(pool, "WHERE enabled=1 AND username NOT IN (SELECT username FROM blocked_users)", None).await {
        if !removed.contains(&r.username) && local_is_admin(pool, &r).await {
            n += 1;
        }
    }
    n
}

/// `_parse_budget(raw)`: '' → None (inherit), else a positive integer.
fn parse_budget(raw: Option<&str>) -> Result<Option<f64>, &'static str> {
    let raw = crate::memory::py_strip(raw.unwrap_or("")).replace(' ', "");
    if raw.is_empty() {
        return Ok(None);
    }
    // int(float(raw)): inf and nan are refused as invalid, not as out of range.
    let v = parse_py_float(&raw).filter(|v| v.is_finite()).ok_or("Quota invalide.")?.trunc();
    if !(v > 0.0 && v <= MAX_BUDGET) {
        return Err("Le quota doit être un entier positif, au plus 1e+12 tokens.");
    }
    Ok(Some(v))
}

/// `^[\w .-]{1,80}$` (Python's `\w` is Unicode-aware).
fn group_name_ok(name: &str) -> bool {
    (1..=80).contains(&name.chars().count()) && name.chars().all(|c| c.is_alphanumeric() || matches!(c, '_' | ' ' | '.' | '-'))
}

fn bad(msg: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": msg}))).into_response()
}

/// POST /admin/groups/create - create or edit (by name, any case) a group.
pub async fn admin_groups_create(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/groups/create").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let mut name = crate::memory::py_strip(form.get("name").map(String::as_str).unwrap_or("")).to_string();
    if !group_name_ok(&name) {
        return bad("Nom de groupe invalide (max 80).");
    }
    let budget = match parse_budget(form.get("max_budget").map(String::as_str)) {
        Ok(b) => b,
        Err(e) => return bad(e),
    };
    let is_admin = matches!(form.get("is_admin").map(String::as_str), Some("1" | "true" | "on"));
    let pool = state.db.pool();
    // Groups merge by name whatever the case: editing goes to the existing row.
    if let Some(existing) = sqlx::query_scalar::<_, String>("SELECT name FROM user_groups WHERE lower(name)=lower(?)").bind(&name).fetch_optional(pool).await.ok().flatten() {
        name = existing;
    }
    // Withdrawing the group's admin right must not leave the portal without a local admin.
    if local_group(pool, Some(&name)).await.is_some_and(|g| g.2) && !is_admin {
        let members = locals(pool, "WHERE group_name=?", Some(&name)).await;
        let mut any_admin = false;
        for m in &members {
            any_admin |= local_is_admin(pool, m).await;
        }
        let names: Vec<String> = members.iter().map(|m| m.username.clone()).collect();
        if any_admin && local_admins_without(pool, &names).await == 0 {
            return json_error(
                &format!("Retirer le droit d'administration du groupe {name} laisserait le portail sans aucun administrateur local. Ajoute un autre administrateur local d'abord."),
                StatusCode::CONFLICT,
            );
        }
    }
    let _ = sqlx::query(
        "INSERT INTO user_groups (name, max_budget, is_admin, created_at) VALUES (?,?,?,?) \
         ON CONFLICT(name) DO UPDATE SET max_budget=excluded.max_budget, is_admin=excluded.is_admin",
    )
    .bind(&name)
    .bind(budget.map(|b| b as i64))
    .bind(i64::from(is_admin))
    .bind(keys::iso(&chrono::Local::now().naive_local()))
    .execute(pool)
    .await;
    // The group's new quota reaches its local members without an override…
    for u in locals(pool, "WHERE group_name=? AND max_budget IS NULL", Some(&name)).await {
        sync_local_budget(&state, &u).await;
    }
    // …and its directory members, admin right included.
    recompute_members(&state, &name).await;
    crate::sessions::log_audit(pool, &s.username, "group.create", &format!("groupe {name}")).await;
    Json(json!({"ok": true})).into_response()
}

/// POST /admin/groups/delete/<name> - its members fall back on the default.
pub async fn admin_groups_delete(State(state): State<AppState>, Path(name): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/groups/delete/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let members = locals(pool, "WHERE group_name=?", Some(&name)).await;
    if local_group(pool, Some(&name)).await.is_some_and(|g| g.2) {
        let mut any_admin = false;
        for m in &members {
            any_admin |= local_is_admin(pool, m).await;
        }
        let names: Vec<String> = members.iter().map(|m| m.username.clone()).collect();
        if any_admin && local_admins_without(pool, &names).await == 0 {
            return json_error(
                &format!("Le groupe {name} porte les droits d'administration de ses membres et aucun administrateur local ne subsisterait après sa suppression. Ajoute un autre administrateur local d'abord."),
                StatusCode::CONFLICT,
            );
        }
    }
    let _ = sqlx::query("UPDATE local_users SET group_name=NULL WHERE group_name=?").bind(&name).execute(pool).await;
    let _ = sqlx::query("DELETE FROM user_groups WHERE name=?").bind(&name).execute(pool).await;
    recompute_members(&state, &name).await;
    // The members lose the group's quota: their new effective cap goes to LiteLLM.
    let mut failures = Vec::new();
    for m in &members {
        if m.max_budget.is_some() {
            continue;
        }
        let fresh = locals(pool, "WHERE username=?", Some(&m.username)).await;
        if let Some(f) = fresh.first() {
            if !sync_local_budget(&state, f).await {
                failures.push(m.username.clone());
            }
        }
    }
    let mut detail = format!("suppression du groupe {name} — {} membre(s) rebasculé(s) sur le défaut", members.len());
    if !failures.is_empty() {
        detail += &format!(", QUOTA NON APPLIQUÉ pour {}", failures.join(", "));
    }
    crate::sessions::log_audit(pool, &s.username, "group.delete", &detail).await;
    Json(json!({"ok": true, "members": members.len(), "quota_failures": failures})).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn budgets_parse_like_python() {
        assert_eq!(parse_budget(Some(" ")), Ok(None));
        assert_eq!(parse_budget(Some("1 000 000")), Ok(Some(1_000_000.0)));
        assert_eq!(parse_budget(Some("1.9")), Ok(Some(1.0)));
        assert!(parse_budget(Some("0.5")).unwrap_err().starts_with("Le quota"));
        assert_eq!(parse_budget(Some("inf")), Err("Quota invalide."));
        assert_eq!(parse_budget(Some("nan")), Err("Quota invalide."));
        assert!(parse_budget(Some("2e12")).is_err());
    }

    #[test]
    fn group_names() {
        assert!(group_name_ok("authentik Admins"));
        assert!(group_name_ok("équipe-1.b"));
        assert!(!group_name_ok("a/b"));
        assert!(!group_name_ok(""));
    }
}
