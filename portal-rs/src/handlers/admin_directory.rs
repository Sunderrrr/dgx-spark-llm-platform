//! The admin's views of accounts and of the platform (admin_users.py,
//! admin_routes.py, admin_models.py): every known account with its sources
//! and directory state, one account in detail, the platform's state, and the
//! decision on a model request (the requester is e-mailed at the address the
//! directory knows).
use super::*;
use super::admin_routes::admin_guard;
use super::admin_writes::{add_announcement, admin_form, json_error, json_ok, username_ok};
use std::collections::{BTreeSet, HashMap};

/// A `local_users` row as these views read it.
struct LocalRow {
    id: i64,
    fullname: Option<String>,
    is_admin: i64,
    group: Option<String>,
    max_budget: Option<f64>,
    enabled: i64,
}

type GroupRow = (String, Option<f64>, i64, Option<String>);

async fn groups(pool: &sqlx::SqlitePool) -> Vec<GroupRow> {
    sqlx::query_as("SELECT name, CAST(max_budget AS REAL), COALESCE(is_admin, 0), sources FROM user_groups ORDER BY name").fetch_all(pool).await.unwrap_or_default()
}

/// `_local_user_effective_budget(row)`: its own, its group's, or the default.
fn effective_budget(row: &LocalRow, groups: &HashMap<String, GroupRow>, default: f64) -> Value {
    if let Some(b) = row.max_budget {
        return num(b);
    }
    match row.group.as_ref().and_then(|g| groups.get(g)).and_then(|g| g.1) {
        Some(b) => num(b),
        None => json!(default),
    }
}

/// An integer column read as REAL goes back out as the integer Python returns.
fn num(v: f64) -> Value {
    if v.fract() == 0.0 && v.abs() < 9.0e15 { json!(v as i64) } else { json!(v) }
}

fn local_admin(row: &LocalRow, groups: &HashMap<String, GroupRow>) -> bool {
    row.is_admin != 0 || row.group.as_ref().and_then(|g| groups.get(g)).is_some_and(|g| g.2 != 0)
}

async fn locals(pool: &sqlx::SqlitePool, filter: &str, arg: Option<&str>) -> Vec<(String, LocalRow)> {
    let sql = format!("SELECT id, username, fullname, COALESCE(is_admin, 0), group_name, CAST(max_budget AS REAL), COALESCE(enabled, 0) FROM local_users {filter}");
    let mut q = sqlx::query_as::<_, (i64, String, Option<String>, i64, Option<String>, Option<f64>, i64)>(&sql);
    if let Some(a) = arg {
        q = q.bind(a);
    }
    q.fetch_all(pool)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(|(id, u, fullname, is_admin, group, max_budget, enabled)| (u, LocalRow { id, fullname, is_admin, group, max_budget, enabled }))
        .collect()
}

async fn owner(state: &AppState) -> String {
    state.db.get_setting("owner_username").await.ok().flatten().unwrap_or_default()
}

fn epoch_now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// GET /api/admin/users - every known account, its sources and state.
pub async fn api_admin_users(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, "/api/admin/users").await {
        return r;
    }
    let pool = state.db.pool();
    let managed: HashMap<String, LocalRow> = locals(pool, "", None).await.into_iter().collect();
    type Source = (String, Option<String>, Option<String>, Option<String>, Option<String>, Option<i64>);
    let recorded: HashMap<String, Source> = sqlx::query_as::<_, Source>("SELECT username, sources, fullname, last_source, last_seen, last_is_admin FROM user_sources")
        .fetch_all(pool)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(|r| (r.0.clone(), r))
        .collect();
    let spend: HashMap<String, Value> = super::admin_routes::user_consumption(&state)
        .await
        .into_iter()
        .filter_map(|v| v.get("username").and_then(Value::as_str).map(str::to_string).map(|u| (u, v)))
        .collect();
    let blocked: HashMap<String, (Option<String>, Option<String>)> = sqlx::query_as::<_, (String, Option<String>, Option<String>)>("SELECT username, reason, blocked_at FROM blocked_users")
        .fetch_all(pool)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(|(u, r, a)| (u, (r, a)))
        .collect();
    let avatars: HashMap<String, Option<String>> = sqlx::query_as::<_, (String, Option<String>)>("SELECT username, avatar_id FROM user_prefs").fetch_all(pool).await.unwrap_or_default().into_iter().collect();
    let now = chrono::Local::now().timestamp_micros() as f64 / 1e6;
    let mut locked: HashMap<String, i64> = HashMap::new();
    for (key, until) in sqlx::query_as::<_, (String, f64)>("SELECT key, CAST(locked_until AS REAL) FROM login_attempts WHERE locked_until > ?").bind(now).fetch_all(pool).await.unwrap_or_default() {
        let tail = key.rsplit('|').next().unwrap_or(&key);
        let account = tail.strip_prefix("user:").unwrap_or(tail).to_string();
        let mins = ((until - now) / 60.0).floor() as i64 + 1;
        let e = locked.entry(account).or_insert(0);
        *e = (*e).max(mins);
    }
    let group_rows = groups(pool).await;
    let by_name: HashMap<String, GroupRow> = group_rows.iter().map(|g| (g.0.clone(), g.clone())).collect();
    let mut dir_groups: HashMap<String, Vec<String>> = HashMap::new();
    for (u, g) in sqlx::query_as::<_, (String, String)>(
        "SELECT DISTINCT d.username, d.group_name FROM user_directory_groups d JOIN user_groups g ON g.name = d.group_name ORDER BY d.group_name",
    )
    .fetch_all(pool)
    .await
    .unwrap_or_default()
    {
        dir_groups.entry(u).or_default().push(g);
    }
    let names: BTreeSet<String> = managed.keys().chain(recorded.keys()).chain(spend.keys()).cloned().collect();
    let external: Vec<String> = names.iter().filter(|n| !managed.contains_key(*n)).cloned().collect();
    let states = crate::ldap::states_for(&state, &external);
    let owner_name = owner(&state).await;
    let default = super::admin_groups::default_budget(&state).await;
    let mut out = Vec::new();
    for name in &names {
        let mut srcs: BTreeSet<String> = BTreeSet::new();
        if managed.contains_key(name) {
            srcs.insert("local".into());
        }
        if let Some(r) = recorded.get(name) {
            srcs.extend(r.1.clone().unwrap_or_default().split(',').filter(|s| !s.is_empty()).map(str::to_string));
        }
        if srcs.is_empty() && spend.contains_key(name) {
            srcs.insert("external".into());
        }
        let local = managed.get(name);
        let rs = recorded.get(name);
        let sp = spend.get(name);
        let fullname = local.and_then(|l| l.fullname.clone()).filter(|f| !f.is_empty()).or_else(|| rs.and_then(|r| r.2.clone()));
        let last_source = rs.and_then(|r| r.3.clone());
        let last_is_admin = rs.and_then(|r| r.5);
        let (mut effective_admin, role_source) = match local {
            Some(l) => (json!(local_admin(l, &by_name)), "local".to_string()),
            None => (
                last_is_admin.map_or(Value::Null, |a| json!(a != 0)),
                match last_source.as_deref() {
                    Some(s @ ("sso" | "ldap")) => s.to_string(),
                    _ => "external".into(),
                },
            ),
        };
        let managed_by = if local.is_none() || matches!(last_source.as_deref(), Some("sso" | "ldap")) { "repertoire" } else { "local" };
        if *name == owner_name {
            effective_admin = json!(true);
        }
        let bl = blocked.get(name);
        out.push(json!({
            "username": name,
            "fullname": fullname,
            "avatar_id": avatars.get(name).cloned().flatten(),
            "sources": srcs,
            "managed": local.is_some(),
            "managed_by": managed_by,
            "id": local.map(|l| l.id),
            "group_name": local.and_then(|l| l.group.clone()),
            "enabled": local.map_or(1, |l| l.enabled),
            "is_admin": local.map(|l| l.is_admin),
            "effective_admin": effective_admin,
            "role_source": role_source,
            "last_source": last_source,
            "effective_budget": match local {
                Some(l) => effective_budget(l, &by_name, default),
                None => sp.and_then(|s| s.get("max_budget")).cloned().unwrap_or(Value::Null),
            },
            "unlimited": sp.and_then(|s| s.get("unlimited")).cloned().unwrap_or(json!(false)),
            "spend": sp.and_then(|s| s.get("spend")).cloned().unwrap_or(json!(0)),
            "key_count": sp.and_then(|s| s.get("key_count")).cloned().unwrap_or(json!(0)),
            "last_seen": rs.and_then(|r| r.4.clone()),
            "blocked": bl.is_some(),
            "block_reason": bl.and_then(|b| b.0.clone()),
            "blocked_at": bl.and_then(|b| b.1.clone()),
            "locked_minutes": locked.get(name).copied().unwrap_or(0),
            "directory_status": if local.is_none() { states.as_ref().and_then(|s| s.get(name).cloned()) } else { None },
            "directory_groups": dir_groups.get(name).cloned().unwrap_or_default(),
            "owner": *name == owner_name,
        }));
    }
    let mut members: HashMap<String, i64> = HashMap::new();
    for u in &out {
        let mut gs: BTreeSet<String> = u["directory_groups"].as_array().into_iter().flatten().filter_map(|g| g.as_str().map(str::to_string)).collect();
        if let Some(g) = u["group_name"].as_str() {
            gs.insert(g.to_string());
        }
        for g in gs {
            *members.entry(g).or_insert(0) += 1;
        }
    }
    let groups_out: Vec<Value> = group_rows
        .iter()
        .map(|(n, b, a, s)| {
            json!({"name": n, "max_budget": b.map(num), "is_admin": a, "sources": s.clone().unwrap_or_default().split(',').filter(|x| !x.is_empty()).collect::<Vec<_>>(), "members": members.get(n).copied().unwrap_or(0)})
        })
        .collect();
    Json(json!({"users": out, "groups": groups_out, "default_budget": default, "budget_duration": super::admin_groups::default_duration(&state).await})).into_response()
}

/// GET /admin/users/<username>/detail - everything known about one account
/// (key values never leave: aliases only).
pub async fn admin_user_detail(State(state): State<AppState>, Path(username): Path<String>, headers: HeaderMap) -> Response {
    let s = match admin_guard(&state, &headers, "/admin/users/x/detail").await {
        Ok(s) => s,
        Err(r) => return r,
    };
    if !username_ok(&username) {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Nom d'utilisateur invalide."}))).into_response();
    }
    let pool = state.db.pool();
    let local = locals(pool, "WHERE username=?", Some(&username)).await.into_iter().next().map(|(_, r)| r);
    type Source = (Option<String>, Option<String>, Option<String>, Option<String>, Option<i64>);
    let rs: Option<Source> = sqlx::query_as("SELECT sources, fullname, last_source, last_seen, last_is_admin FROM user_sources WHERE username=?").bind(&username).fetch_optional(pool).await.ok().flatten();
    let bl: Option<(Option<String>, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT reason, blocked_at, blocked_by FROM blocked_users WHERE username=?").bind(&username).fetch_optional(pool).await.ok().flatten();
    let litellm = keys::account_info(&username).await;
    let data = super::admin_accounts::count_data(pool, &username).await;
    let keys_list: Vec<Value> = keys::user_keys(pool, state.spend.as_ref(), &username)
        .await
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .map(|k| json!({"alias": k.get("key_alias"), "created_at": k.get("created_at"), "spend": k.get("spend").cloned().unwrap_or(json!(0))}))
        .collect();
    // complete_session_origin: the caller's own session row, when it has no origin yet.
    if let Some(sid) = &s.sid {
        let ua: String = headers.get(axum::http::header::USER_AGENT).and_then(|v| v.to_str().ok()).unwrap_or("").chars().take(400).collect();
        let _ = sqlx::query("UPDATE user_sessions SET ip=?, user_agent=? WHERE sid=? AND username=? AND (ip IS NULL OR user_agent IS NULL)")
            .bind(auth::client_ip(&headers, None))
            .bind(ua)
            .bind(sid)
            .bind(&s.username)
            .execute(pool)
            .await;
    }
    let now = chrono::Local::now().timestamp_micros() as f64 / 1e6;
    let sessions: Vec<Value> = sqlx::query(
        "SELECT sid, created_at, expires_at, ip, user_agent FROM user_sessions WHERE username=? AND revoked=0 AND expires_at > ? ORDER BY created_at DESC LIMIT 20",
    )
    .bind(&username)
    .bind(now)
    .fetch_all(pool)
    .await
    .unwrap_or_default()
    .iter()
    .map(|r| {
        let sid: Option<String> = r.get("sid");
        let mut row = crate::rowjson::row_dict(r).as_object().cloned().unwrap_or_default();
        let sid = sid.unwrap_or_default();
        row.remove("sid");
        let mut out = serde_json::Map::new();
        out.insert("id".into(), json!(sid.chars().take(12).collect::<String>()));
        for k in ["created_at", "expires_at", "ip", "user_agent"] {
            out.insert(k.into(), row.remove(k).unwrap_or(Value::Null));
        }
        out.insert("current".into(), json!(s.sid.as_deref().is_some_and(|c| !c.is_empty() && c == sid)));
        Value::Object(out)
    })
    .collect();
    let audit: Vec<Value> = sqlx::query_as::<_, (Option<String>, Option<String>, Option<String>)>("SELECT action, detail, created_at FROM audit_log WHERE username=? ORDER BY id DESC LIMIT 20")
        .bind(&username)
        .fetch_all(pool)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(|(a, d, t)| json!({"action": a, "detail": d, "at": t}))
        .collect();
    let groups_map: HashMap<String, GroupRow> = groups(pool).await.into_iter().map(|g| (g.0.clone(), g)).collect();
    let role = match &local {
        Some(l) => if local_admin(l, &groups_map) { "admin" } else { "user" },
        None => if rs.as_ref().and_then(|r| r.4).unwrap_or(0) != 0 { "admin" } else { "user" },
    };
    let default = super::admin_groups::default_budget(&state).await;
    let mut media = serde_json::Map::new();
    for (k, t) in [("image", "image_jobs"), ("video", "video_jobs"), ("voice", "voice_jobs"), ("ocr", "ocr_jobs")] {
        let (count, last): (i64, Option<String>) = sqlx::query_as(&format!("SELECT COUNT(*), MAX(created_at) FROM {t} WHERE username=?")).bind(&username).fetch_one(pool).await.unwrap_or((0, None));
        media.insert(k.into(), json!({"count": count, "last": last}));
    }
    let requests: Vec<Value> = sqlx::query("SELECT model_id, status, created_at FROM model_requests WHERE username=? ORDER BY created_at DESC LIMIT 10")
        .bind(&username)
        .fetch_all(pool)
        .await
        .unwrap_or_default()
        .iter()
        .map(crate::rowjson::row_dict)
        .collect();
    let directory_status = if local.is_none() { crate::ldap::states_for(&state, std::slice::from_ref(&username)).and_then(|m| m.get(&username).cloned()) } else { None };
    Json(json!({
        "ok": true,
        "username": username,
        "fullname": local.as_ref().and_then(|l| l.fullname.clone()).filter(|f| !f.is_empty()).or_else(|| rs.as_ref().and_then(|r| r.1.clone())),
        "sources": rs.as_ref().and_then(|r| r.0.clone()).unwrap_or_default(),
        "last_source": rs.as_ref().and_then(|r| r.2.clone()),
        "last_seen": rs.as_ref().and_then(|r| r.3.clone()),
        "local": local.is_some(),
        "enabled": local.as_ref().map_or(true, |l| l.enabled != 0),
        "group": local.as_ref().and_then(|l| l.group.clone()),
        "max_budget": local.as_ref().and_then(|l| l.max_budget).map(num),
        "effective_budget": match &local { Some(l) => effective_budget(l, &groups_map, default), None => litellm.max_budget.map_or(Value::Null, |b| json!(b)) },
        "role": role,
        "blocked": {"blocked": bl.is_some(), "reason": bl.as_ref().and_then(|b| b.0.clone()), "at": bl.as_ref().and_then(|b| b.1.clone()), "by": bl.as_ref().and_then(|b| b.2.clone())},
        "litellm": {"exists": litellm.exists, "max_budget": litellm.max_budget, "spend": litellm.spend, "budget_duration": (!litellm.budget_reset_at.is_empty()).then_some(litellm.budget_reset_at.clone())},
        "keys": keys_list,
        "memory_facts": data["memory_facts"],
        "conversations": data["conversations"],
        "sessions": sessions,
        "audit": audit,
        "activity": crate::settings::activity(state.spend.as_ref(), &username, 182).await,
        "media": media,
        "model_requests": requests,
        "directory_status": directory_status,
        "data": data,
    }))
    .into_response()
}

/// Python's `round(x, 1)`.
fn round1(x: f64) -> f64 {
    format!("{x:.1}").parse().unwrap_or(x)
}

fn disk_state() -> Value {
    let path = std::ffi::CString::new("/app/data").expect("static path");
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: a valid C string and an out-pointer to a zeroed statvfs.
    if unsafe { libc::statvfs(path.as_ptr(), &mut st) } != 0 {
        return json!({"free_gb": null, "total_gb": null, "used_pct": null});
    }
    let total = st.f_frsize as f64 * st.f_blocks as f64;
    let free = st.f_frsize as f64 * st.f_bavail as f64;
    let gib = 1024f64.powi(3);
    json!({"free_gb": round1(free / gib), "total_gb": round1(total / gib), "used_pct": if total > 0.0 { json!(round1(100.0 * (total - free) / total)) } else { Value::Null }})
}

fn monitor_path() -> String {
    std::env::var("MONITOR_STATE").unwrap_or_else(|_| "/var/lib/dgx-monitor/state.json".into())
}

fn backup_state() -> Value {
    if let Some(b) = std::fs::read_to_string(monitor_path()).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()).and_then(|v| v.get("backup").cloned()) {
        if b.get("latest").is_some_and(keys::truthy) {
            let age = b.get("age_hours").cloned().unwrap_or(Value::Null);
            return json!({"latest": b["latest"], "age_hours": age, "fresh": if age.is_null() { Value::Null } else { json!(b.get("up").is_some_and(keys::truthy)) }, "count": b.get("count"), "readable": true});
        }
    }
    let dir = std::env::var("BACKUP_DIR").unwrap_or_else(|_| "/var/backups/dgx".into());
    let Ok(rd) = std::fs::read_dir(&dir) else {
        return json!({"latest": null, "age_hours": null, "fresh": null, "count": null, "readable": false});
    };
    let mut found: Vec<(String, f64)> = Vec::new();
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if name.starts_with("portal-") && name.ends_with(".db") {
            let Ok(m) = e.metadata().and_then(|m| m.modified()) else {
                return json!({"latest": null, "age_hours": null, "fresh": null, "count": null, "readable": false});
            };
            found.push((name, m.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)));
        }
    }
    let Some((last, t)) = found.iter().max_by(|a, b| a.1.total_cmp(&b.1)).cloned() else {
        return json!({"latest": null, "age_hours": null, "fresh": false, "count": 0, "readable": true});
    };
    let age = (epoch_now() - t) / 3600.0;
    json!({"latest": last, "age_hours": round1(age), "fresh": age <= 26.0, "count": found.len(), "readable": true})
}

fn monitor_state() -> Value {
    let path = monitor_path();
    let parsed = std::fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok());
    let mtime = std::fs::metadata(&path).and_then(|m| m.modified()).ok();
    let (Some(st), Some(mtime)) = (parsed, mtime) else {
        return json!({"readable": false, "active_incidents": null, "since": null});
    };
    let mut down: Vec<String> = match st.get("down") {
        Some(Value::Object(o)) => o.iter().filter(|(_, v)| keys::truthy(v)).map(|(k, _)| k.clone()).collect(),
        Some(Value::Array(a)) => a.iter().map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())).collect(),
        _ => Vec::new(),
    };
    down.sort();
    let since = chrono::DateTime::<chrono::Utc>::from(mtime).format("%Y-%m-%dT%H:%M:%S+00:00").to_string();
    json!({"readable": true, "active_incidents": down, "since": since})
}

async fn model_state(state: &AppState) -> Value {
    let st = crate::system::runner_status().await;
    let name = st.get("model").cloned().unwrap_or(Value::Null);
    let status = st.get("status").cloned().unwrap_or(Value::Null);
    let mut uptime = Value::Null;
    if status == "running" {
        let served = state.db.get_setting("served_model").await.ok().flatten().unwrap_or_default();
        if let Ok(o) = serde_json::from_str::<Value>(if served.is_empty() { "{}" } else { &served }) {
            if o.get("name") == Some(&name) {
                if let Some(since) = o.get("since").filter(|v| keys::truthy(v)).and_then(|v| v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok()))) {
                    uptime = json!(((epoch_now() - since) as i64).max(0));
                }
            }
        }
    }
    json!({"name": name, "status": status, "uptime_s": uptime})
}

async fn portal_state(pool: &sqlx::SqlitePool) -> Value {
    let path: Option<String> = sqlx::query("PRAGMA database_list").fetch_optional(pool).await.ok().flatten().and_then(|r| r.try_get::<String, _>(2).ok());
    let size = path.filter(|p| !p.is_empty()).and_then(|p| std::fs::metadata(p).ok()).map(|m| round1(m.len() as f64 / 1024f64.powi(2)));
    let now = epoch_now();
    let one = |sql: &'static str| async move { sqlx::query_scalar::<_, i64>(sql).bind(now).fetch_one(pool).await.unwrap_or(0) };
    let utc = keys::iso(&chrono::Utc::now().naive_utc());
    json!({
        "db_mb": size,
        "active_sessions": one("SELECT COUNT(*) FROM user_sessions WHERE revoked = 0 AND expires_at > ?").await,
        "connected_users": one("SELECT COUNT(DISTINCT username) FROM user_sessions WHERE revoked = 0 AND expires_at > ?").await,
        "local_users": sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM local_users WHERE enabled=1").fetch_one(pool).await.unwrap_or(0),
        "active_keys": sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM api_keys").fetch_one(pool).await.unwrap_or(0),
        "budget_grants": sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM budget_grants WHERE expires_at > ?").bind(utc).fetch_one(pool).await.unwrap_or(0),
        "audit_entries": sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM audit_log").fetch_one(pool).await.unwrap_or(0),
    })
}

async fn directory_state(state: &AppState) -> Value {
    let names: Vec<String> = sqlx::query_scalar(
        "SELECT s.username FROM user_sources s LEFT JOIN local_users l ON l.username = s.username WHERE l.username IS NULL AND (s.sources LIKE '%ldap%' OR s.sources LIKE '%sso%')",
    )
    .fetch_all(state.db.pool())
    .await
    .unwrap_or_default();
    let Some(states) = crate::ldap::states_for(state, &names) else { return json!({"readable": false}) };
    let mut out = serde_json::Map::new();
    out.insert("readable".into(), json!(true));
    for k in ["active", "disabled", "absent"] {
        out.insert(k.into(), json!(0));
    }
    for v in states.values() {
        let n = out.get(v.as_str()).and_then(Value::as_i64).unwrap_or(0) + 1;
        out.insert(v.clone(), json!(n));
    }
    Value::Object(out)
}

/// GET /admin/platform - disk, backup, incidents, served model, portal,
/// memory, directory: what is asked during an incident.
pub async fn admin_platform(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(r) = admin_guard(&state, &headers, "/admin/platform").await {
        return r;
    }
    let mem = crate::asr_ondemand::mem_available_gb();
    Json(json!({
        "disk": disk_state(),
        "backup": backup_state(),
        "monitor": monitor_state(),
        "model": model_state(&state).await,
        "portal": portal_state(state.db.pool()).await,
        "memory": {"available_gib": mem.map(round1), "low_gib": 8},
        "directory": directory_state(&state).await,
        "maintenance": state.db.maintenance_active().await,
        "checked_at": epoch_now() as i64,
    }))
    .into_response()
}

/// `admin_models.DEFAULT_VLLM_ARGS` / `DEFAULT_LLAMA_ARGS`.
const DEFAULT_VLLM_ARGS: &str = "--enable-auto-tool-choice --tool-call-parser qwen3_coder --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.7 --max-num-seqs 4";
const DEFAULT_LLAMA_ARGS: &str = "--ctx-size 32768 --n-gpu-layers 999 --parallel 4 --flash-attn --jinja";

/// `_model_slug(hf_id)`.
fn model_slug(hf_id: &str) -> String {
    let base = hf_id.rsplit('/').next().unwrap_or("");
    let s: String = base.chars().map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '-' { c } else { '-' }).collect();
    let s: String = s.trim_matches('-').to_lowercase().chars().take(40).collect();
    if s.is_empty() { "modele".into() } else { s }
}

/// `hf_engine_for(hf_id)`: GGUF on the Hub → llama.cpp, else vLLM (also on a failure).
async fn hf_engine_for(hf_id: &str) -> &'static str {
    let shape = hf_id.split_once('/').is_some_and(|(a, b)| {
        let ok = |p: &str| !p.is_empty() && p.chars().all(|c| c.is_alphanumeric() || matches!(c, '_' | '.' | '-'));
        ok(a) && ok(b)
    });
    if !shape {
        return "vllm";
    }
    let base = std::env::var("HF_API").unwrap_or_else(|_| "https://huggingface.co/api".into());
    match crate::sidecars::http().get(format!("{base}/models/{hf_id}")).timeout(std::time::Duration::from_secs(6)).send().await {
        Ok(r) if r.status().is_success() => match r.json::<Value>().await {
            Ok(m) => crate::hf::guess_engine(&m),
            Err(_) => "vllm",
        },
        _ => "vllm",
    }
}

/// `_add_model_to_catalog(hf_id)`: (name, already there).
async fn add_to_catalog(pool: &sqlx::SqlitePool, hf_id: &str) -> (String, bool) {
    if let Some(n) = sqlx::query_scalar::<_, String>("SELECT name FROM model_configs WHERE hf_model_id=?").bind(hf_id).fetch_optional(pool).await.ok().flatten() {
        return (n, true);
    }
    let base = model_slug(hf_id);
    let mut name = base.clone();
    let mut n = 2;
    while sqlx::query_scalar::<_, i64>("SELECT 1 FROM model_configs WHERE name=?").bind(&name).fetch_optional(pool).await.ok().flatten().is_some() {
        name = format!("{base}-{n}");
        n += 1;
    }
    let engine = hf_engine_for(hf_id).await;
    let args = if engine == "llamacpp" { DEFAULT_LLAMA_ARGS } else { DEFAULT_VLLM_ARGS };
    let _ = sqlx::query("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) VALUES (?,?,?,?,?)")
        .bind(&name)
        .bind(hf_id)
        .bind(args)
        .bind(engine)
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .execute(pool)
        .await;
    (name, false)
}

/// POST /admin/update/<int:req_id> - a model request's status. « done »
/// adds the model to the catalog and routes it first; the requester is told
/// only once it is routed (notification, then e-mail).
pub async fn admin_update_request(State(state): State<AppState>, Path(id): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let Some(id) = (!id.is_empty() && id.bytes().all(|c| c.is_ascii_digit())).then(|| id.parse::<i64>().ok()).flatten() else {
        return auth::html_status(StatusCode::NOT_FOUND);
    };
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/update/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let status = form.get("status").cloned().unwrap_or_default();
    if !["pending", "done", "rejected"].contains(&status.as_str()) {
        return json_error("Statut invalide.", StatusCode::BAD_REQUEST);
    }
    let pool = state.db.pool();
    let req: Option<(Option<String>, Option<String>)> = sqlx::query_as("SELECT username, model_id FROM model_requests WHERE id=?").bind(id).fetch_optional(pool).await.ok().flatten();
    let Some((user, model_id)) = req else {
        return json_error("Demande introuvable.", StatusCode::NOT_FOUND);
    };
    let user = user.unwrap_or_default();
    let model_id = model_id.unwrap_or_default();
    let (mut name, mut already) = (None, false);
    if status == "done" && !model_id.is_empty() {
        let (n, a) = add_to_catalog(pool, &model_id).await;
        let cfg: Option<(Option<String>, Option<String>)> = sqlx::query_as("SELECT vllm_args, engine FROM model_configs WHERE name=?").bind(&n).fetch_optional(pool).await.ok().flatten();
        let (args, engine) = match cfg {
            Some((a, e)) => (a.unwrap_or_default(), e.filter(|e| !e.is_empty()).unwrap_or_else(|| "vllm".into())),
            None => (DEFAULT_VLLM_ARGS.to_string(), "vllm".into()),
        };
        if !crate::litellm_models::register(&n, &args, &engine).await {
            crate::sessions::log_audit(pool, &s.username, "request.routage_echec", &format!("demande #{id} ({n}) : catalogue OK, LiteLLM non enregistré")).await;
            return json_error(
                &format!("Le modèle « {n} » est dans le catalogue, mais son enregistrement LiteLLM a échoué : la demande reste EN ATTENTE et le demandeur n'est pas prévenu (il ne pourrait pas l'utiliser). Réessaie."),
                StatusCode::SERVICE_UNAVAILABLE,
            );
        }
        name = Some(n);
        already = a;
    }
    let _ = sqlx::query("UPDATE model_requests SET status=?, updated_at=? WHERE id=?").bind(&status).bind(keys::iso(&chrono::Local::now().naive_local())).bind(id).execute(pool).await;
    if !user.is_empty() {
        if status == "done" {
            let what = if model_id.is_empty() { "demande validée" } else { model_id.as_str() };
            super::image_routes::add_notification(pool, &user, "request", &format!("Ton modèle est disponible : {what}.")).await;
        } else if status == "rejected" {
            super::image_routes::add_notification(pool, &user, "request", "Ta demande de modèle a été refusée.").await;
        }
    }
    let tail = name.as_ref().map(|n| format!(" ({n})")).unwrap_or_default();
    crate::sessions::log_audit(pool, &s.username, "request.status", &format!("demande #{id} → {status}{tail}")).await;
    if status != "done" || model_id.is_empty() {
        return json_ok("Demande mise à jour.", json!({}));
    }
    if let Some(to) = crate::ldap::lookup_email(pool, &user).await {
        let instance = crate::chat::support_tools::platform_name(pool).await;
        let web = std::env::var("DGX_WEB").unwrap_or_else(|_| "https://example.org".into());
        let web = web.trim_end_matches('/');
        let subject = format!("[{instance}] Ton modèle est disponible");
        let text = format!(
            "Bonne nouvelle — le modèle que tu as demandé est validé et disponible sur la plateforme {instance} :\n\n  {model_id}\n\nTu peux l'utiliser via l'API / le Playground une fois lancé.\n{web}/\n"
        );
        crate::mailer::send_user(pool, &to, &subject, &text).await;
    }
    let name = name.unwrap_or_default();
    if already {
        return json_ok(&format!("Modèle déjà dans le catalogue sous « {name} »."), json!({}));
    }
    add_announcement(pool, "model_add", &name, "").await;
    json_ok(&format!("Modèle « {name} » ajouté au catalogue et routé par LiteLLM — vérifie ses args vLLM puis lance-le."), json!({}))
}

/// POST /admin/request/delete/<id> - removes a model request from the queue
/// (a test row, a duplicate, a stale one). No notification goes to the requester.
pub async fn admin_request_delete(State(state): State<AppState>, Path(id): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let Some(id) = (!id.is_empty() && id.bytes().all(|c| c.is_ascii_digit())).then(|| id.parse::<i64>().ok()).flatten() else {
        return auth::html_status(StatusCode::NOT_FOUND);
    };
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/request/delete/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let row: Option<(Option<String>, Option<String>)> = sqlx::query_as("SELECT username, model_id FROM model_requests WHERE id=?").bind(id).fetch_optional(pool).await.ok().flatten();
    let Some((user, model_id)) = row else {
        return json_error("Demande introuvable.", StatusCode::NOT_FOUND);
    };
    if sqlx::query("DELETE FROM model_requests WHERE id=?").bind(id).execute(pool).await.is_err() {
        return json_error("Base du portail illisible.", StatusCode::SERVICE_UNAVAILABLE);
    }
    crate::sessions::log_audit(pool, &s.username, "request.delete", &format!("demande #{id} ({}) supprimée", model_id.unwrap_or_default())).await;
    let _ = user;
    json_ok("Demande supprimée.", json!({}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugs_follow_flask() {
        assert_eq!(model_slug("Qwen/Qwen3-8B-GGUF"), "qwen3-8b-gguf");
        assert_eq!(model_slug("org/Ünïcode.Name"), "-n-code-name".trim_matches('-'));
        assert_eq!(model_slug("org/..."), "modele");
    }
}
