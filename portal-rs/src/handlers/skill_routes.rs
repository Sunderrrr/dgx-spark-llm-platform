//! Skill package routes (skills_routes.py): detail, download, enable, sync, zip and Git import.
#![allow(unused_imports)]
use super::*;

// ── Skills (skills_routes.py) ───────────────────────────────────────────────

pub(crate) fn skill_not_found() -> Response {
    (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "Compétence introuvable."}))).into_response()
}

/// `_summary` of one of the viewer's skills, with its stored row.
pub(crate) async fn skill_summary(pool: &sqlx::SqlitePool, id: i64, username: &str) -> Result<Option<(Value, String, String, String)>, sqlx::Error> {
    let Some(r) = sqlx::query("SELECT * FROM skills WHERE id = ? AND username = ?").bind(id).bind(username).fetch_optional(pool).await? else {
        return Ok(None);
    };
    let file_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM skill_files WHERE skill_id = ?").bind(id).fetch_one(pool).await?;
    let get = |c: &str| r.try_get::<Option<String>, _>(c).ok().flatten();
    let name = get("name").unwrap_or_default();
    let summary = json!({
        "id": id, "name": name, "description": get("description"),
        "source": get("source").filter(|x| !x.is_empty()).unwrap_or_else(|| "manual".to_string()),
        "source_url": get("source_url"), "source_ref": get("source_ref"), "source_path": get("source_path"),
        "version": get("version").unwrap_or_default(), "synced_at": get("synced_at"), "created_at": get("created_at"),
        "enabled": r.try_get::<Option<i64>, _>("enabled").ok().flatten().unwrap_or(0) != 0,
        "file_count": file_count,
    });
    Ok(Some((summary, name, get("description").unwrap_or_default(), get("instructions").unwrap_or_default())))
}

pub(crate) async fn skill_files_or_manual(pool: &sqlx::SqlitePool, id: i64, name: &str, description: &str, instructions: &str) -> Result<Vec<(String, String)>, sqlx::Error> {
    let files: Vec<(String, String)> = sqlx::query_as("SELECT path, content FROM skill_files WHERE skill_id = ? ORDER BY path")
        .bind(id)
        .fetch_all(pool)
        .await?;
    Ok(if files.is_empty() { vec![("SKILL.md".to_string(), skillstore::manual_skill_md(name, description, instructions))] } else { files })
}

/// GET /api/skills/:id - one of the viewer's skills with its files
pub async fn api_skill_detail(State(state): State<AppState>, headers: HeaderMap, Path(raw): Path<String>) -> Response {
    let Some(id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let pool = state.db.pool();
    let Ok(found) = skill_summary(pool, id, &session.username).await else { return db_down() };
    let Some((summary, name, description, instructions)) = found else { return skill_not_found() };
    let Ok(mut files) = skill_files_or_manual(pool, id, &name, &description, &instructions).await else { return db_down() };
    files.sort_by(|a, b| a.0.cmp(&b.0));
    let mut skill = summary.as_object().cloned().unwrap_or_default();
    skill.insert("instructions".into(), json!(instructions));
    skill.insert("files".into(), json!(files.iter().map(|(p, c)| json!({"path": p, "size": c.len(), "content": c})).collect::<Vec<_>>()));
    Json(json!({"ok": true, "skill": skill})).into_response()
}

/// GET /api/skills/:id/download - the skill as a .zip laid out for Claude Code
pub async fn api_skill_download(State(state): State<AppState>, headers: HeaderMap, Path(raw): Path<String>) -> Response {
    let Some(id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let pool = state.db.pool();
    let Ok(found) = skill_summary(pool, id, &session.username).await else { return db_down() };
    let Some((_, name, description, instructions)) = found else { return skill_not_found() };
    let Ok(files) = skill_files_or_manual(pool, id, &name, &description, &instructions).await else { return db_down() };
    (
        [
            (axum::http::header::CONTENT_TYPE, "application/zip".to_string()),
            (axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename=\"{name}.zip\"")),
        ],
        skillstore::build_zip(&name, &files),
    )
        .into_response()
}

/// POST /api/skills/:id/enabled - offer the skill to the model or not
pub async fn api_skill_enabled(State(state): State<AppState>, headers: HeaderMap, Path(raw): Path<String>, body: axum::body::Bytes) -> Response {
    let Some(id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let enabled = json_object(&headers, &body).get("enabled").is_some_and(keys::truthy);
    match sqlx::query("UPDATE skills SET enabled = ? WHERE id = ? AND username = ?")
        .bind(enabled as i64).bind(id).bind(&session.username).execute(state.db.pool()).await
    {
        Err(_) => db_down(),
        Ok(r) if r.rows_affected() == 0 => skill_not_found(),
        Ok(_) => Json(json!({"ok": true, "enabled": enabled})).into_response(),
    }
}

/// The deployment's directory server name, never a download target (mcp_client._ldap_host).
pub(crate) async fn ldap_host(state: &AppState) -> String {
    match state.db.get_config("ldap_uri").await {
        Some(uri) => skillstore::url_parts(&uri).1,
        None => String::new(),
    }
}

pub(crate) fn import_answer(r: Result<(u16, Value), sqlx::Error>) -> Response {
    match r {
        Ok((200, v)) => Json(v).into_response(),
        Ok((code, v)) => (StatusCode::from_u16(code).unwrap_or(StatusCode::BAD_REQUEST), Json(v)).into_response(),
        Err(_) => db_down(),
    }
}

/// POST /api/skills/:id/sync - re-import a Git skill from its source
pub async fn api_skill_sync(State(state): State<AppState>, headers: HeaderMap, Path(raw): Path<String>) -> Response {
    let Some(id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let pool = state.db.pool();
    let row = match sqlx::query("SELECT name, source, source_url, source_ref, source_path FROM skills WHERE id = ? AND username = ?")
        .bind(id).bind(&session.username).fetch_optional(pool).await
    {
        Err(_) => return db_down(),
        Ok(None) => return skill_not_found(),
        Ok(Some(r)) => r,
    };
    let get = |c: &str| row.try_get::<Option<String>, _>(c).ok().flatten().unwrap_or_default();
    if get("source") != "git" || get("source_url").is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Cette compétence n'a pas de dépôt source."}))).into_response();
    }
    let ldap = ldap_host(&state).await;
    let (skills, _skipped, src) = match skillstore::fetch_git_skills(&get("source_url"), &get("source_ref"), &get("source_path"), &ldap).await {
        Err(skillstore::ImportError(e)) => return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": e}))).into_response(),
        Ok(x) => x,
    };
    let Some(matched) = skills.iter().find(|s| s.name == get("name")) else {
        return (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "La compétence n'existe plus dans le dépôt."}))).into_response();
    };
    let saved = async {
        let mut tx = pool.begin().await?;
        // With replace=true the save cannot be refused (it only refuses an existing name).
        let _ = skillstore::save_skill(&mut tx, &session.username, matched, "git", src["url"].as_str(), src["ref"].as_str(), src["path"].as_str(), true).await?;
        tx.commit().await
    }
    .await;
    if saved.is_err() {
        return db_down();
    }
    match skill_summary(pool, id, &session.username).await {
        Ok(Some((summary, ..))) => Json(json!({"ok": true, "skill": summary})).into_response(),
        _ => db_down(),
    }
}

/// POST /api/skills/import - import every skill of an uploaded .zip
pub async fn api_skill_import_zip(State(state): State<AppState>, headers: HeaderMap, multipart: Option<axum::extract::Multipart>) -> Response {
    // request.files / request.form of a multipart body.
    let (mut file, mut fields): (Option<Vec<u8>>, HashMap<String, String>) = (None, HashMap::new());
    if let Some(mut mp) = multipart {
        while let Ok(Some(field)) = mp.next_field().await {
            let name = field.name().unwrap_or_default().to_string();
            let is_file = field.file_name().is_some();
            match field.bytes().await {
                Ok(b) if name == "file" && is_file => file = Some(b.to_vec()),
                Ok(b) => {
                    fields.insert(name, String::from_utf8_lossy(&b).into_owned());
                }
                Err(_) => break,
            }
        }
    }
    match auth::csrf_ok(&headers, fields.get("csrf_token").map(String::as_str), &state.db).await {
        Err(e) => return e.into_response(),
        Ok(false) => return auth::html_status(StatusCode::BAD_REQUEST),
        Ok(true) => {}
    }
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let Some(data) = file else {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Fichier .zip requis."}))).into_response();
    };
    let data = &data[..data.len().min(skillstore::MAX_ARCHIVE_BYTES + 1)];
    let (skills, skipped) = match skillstore::skills_from_zip(data, "") {
        Err(skillstore::ImportError(e)) => return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": e}))).into_response(),
        Ok(x) => x,
    };
    let replace = fields.get("replace").map(String::as_str) == Some("1");
    import_answer(skillstore::import_skills(state.db.pool(), &session.username, &skills, &skipped, replace, "zip", None, None, None).await)
}

/// POST /api/skills/import-git - import the skills of a Git repository
pub async fn api_skill_import_git(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    let field = |k: &str, n: usize| -> String {
        skillstore::py_str_or_empty(data.get(k)).trim_matches(memory::py_is_space).chars().take(n).collect()
    };
    let url = field("url", 500);
    if url.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Adresse du dépôt requise."}))).into_response();
    }
    let ldap = ldap_host(&state).await;
    let (skills, skipped, src) = match skillstore::fetch_git_skills(&url, &field("ref", 100), &field("path", 300), &ldap).await {
        Err(skillstore::ImportError(e)) => return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": e}))).into_response(),
        Ok(x) => x,
    };
    let replace = data.get("replace").is_some_and(keys::truthy);
    import_answer(skillstore::import_skills(state.db.pool(), &session.username, &skills, &skipped, replace, "git",
        src["url"].as_str(), src["ref"].as_str(), src["path"].as_str()).await)
}
