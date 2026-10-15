//! Memory routes (memory_routes.py): graph, switches, facts, purge, export, import.
#![allow(unused_imports)]
use super::*;

// ── Memory (memory_routes.py) ───────────────────────────────────────────────

/// `request.get_json(silent=True) or {}`: a JSON object body, else an empty map.
pub(crate) fn json_object(headers: &HeaderMap, body: &axum::body::Bytes) -> serde_json::Map<String, Value> {
    let is_json = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/json") || v.contains("+json"));
    if !is_json {
        return Default::default();
    }
    match serde_json::from_slice::<Value>(body) {
        Ok(Value::Object(m)) => m,
        _ => Default::default(),
    }
}

/// CSRF then the session, for a JSON or raw-body write.
pub(crate) async fn write_guard(state: &AppState, headers: &HeaderMap, body: &axum::body::Bytes) -> Result<auth::SessionData, Response> {
    csrf_then_viewer(state, headers, body).await
}

/// Flask's `<int:edge_id>`: digits only, else the route does not match (404).
pub(crate) fn int_param(raw: &str) -> Option<i64> {
    (!raw.is_empty() && raw.chars().all(|c| c.is_ascii_digit())).then(|| raw.parse().ok()).flatten()
}

pub(crate) fn db_down() -> Response {
    (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"ok": false, "error": "Base du portail illisible."}))).into_response()
}

/// GET /api/memory - the viewer's whole graph (`?expired=1` includes expired facts)
pub async fn api_memory(State(state): State<AppState>, headers: HeaderMap, Query(q): Query<HashMap<String, String>>) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let db = state.db.pool();
    let include_expired = q.get("expired").map(String::as_str) == Some("1");
    let Ok((nodes, edges)) = memory::graph(db, &session.username, include_expired).await else { return db_down() };
    let (search_chats, sensitive) = memory::settings(db, &session.username).await;
    Json(json!({
        "enabled": memory::enabled(db, &session.username).await,
        "max_facts": memory::MEM_MAX_FACTS,
        "search_chats": search_chats,
        "sensitive": sensitive,
        "nodes": nodes,
        "edges": edges,
    }))
    .into_response()
}

/// POST /api/memory/settings - the two secondary switches
pub async fn api_memory_settings(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    let db = state.db.pool();
    for (key, column) in [("search_chats", "memory_search_chats"), ("sensitive", "memory_sensitive")] {
        if let Some(v) = data.get(key) {
            if memory::set_pref(db, &session.username, column, keys::truthy(v)).await.is_err() {
                return db_down();
            }
        }
    }
    let (search_chats, sensitive) = memory::settings(db, &session.username).await;
    Json(json!({"ok": true, "search_chats": search_chats, "sensitive": sensitive})).into_response()
}

/// POST /api/memory/enabled - turn the memory on or off (erases nothing)
pub async fn api_memory_enabled(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    let active = data.get("enabled").is_some_and(keys::truthy);
    if memory::set_pref(state.db.pool(), &session.username, "memory_enabled", active).await.is_err() {
        return db_down();
    }
    Json(json!({"ok": true, "enabled": active})).into_response()
}

/// POST /api/memory/facts - add a fact from the Memory page
pub async fn api_memory_add(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    let generic = json!(memory::MEM_GENERIC_RELATION);
    let subject_kind = json!("subject");
    // `data.get('relation') or 'à propos de'`, same for kind.
    let relation = data.get("relation").filter(|v| keys::truthy(v)).unwrap_or(&generic);
    let kind = data.get("kind").filter(|v| keys::truthy(v)).unwrap_or(&subject_kind);
    match memory::add_fact(state.db.pool(), &session.username, data.get("subject"), Some(relation), data.get("fact"), data.get("object"), "user", Some(kind)).await {
        Ok((msg, true)) => Json(json!({"ok": true, "message": msg})).into_response(),
        Ok((msg, false)) => (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": msg}))).into_response(),
        Err(_) => db_down(),
    }
}

/// PATCH /api/memory/facts/:edge_id - correct a fact in place
pub async fn api_memory_update(State(state): State<AppState>, headers: HeaderMap, Path(raw): Path<String>, body: axum::body::Bytes) -> Response {
    let Some(edge_id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match write_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let data = json_object(&headers, &body);
    match memory::update_fact(state.db.pool(), &session.username, edge_id, data.get("fact"), data.get("relation")).await {
        Ok(memory::Update::Done(msg)) => Json(json!({"ok": true, "message": msg})).into_response(),
        Ok(memory::Update::Refused(msg)) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": msg}))).into_response(),
        Ok(memory::Update::Malformed) => auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
        Err(_) => db_down(),
    }
}

/// DELETE /api/memory/facts/:edge_id - forget a fact for good
pub async fn api_memory_forget(State(state): State<AppState>, headers: HeaderMap, Path(raw): Path<String>, body: axum::body::Bytes) -> Response {
    let Some(edge_id) = int_param(&raw) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let session = match write_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match memory::forget(state.db.pool(), &session.username, edge_id).await {
        Ok(true) => Json(json!({"ok": true})).into_response(),
        Ok(false) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "Fait introuvable."}))).into_response(),
        Err(_) => db_down(),
    }
}

/// POST /api/memory/purge - erase the viewer's whole memory
pub async fn api_memory_purge(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match memory::purge(state.db.pool(), &session.username).await {
        Ok(n) => Json(json!({"ok": true, "deleted": n})).into_response(),
        Err(_) => db_down(),
    }
}

pub(crate) fn attachment(content_type: &str, filename: String, body: String) -> Response {
    (
        [
            (axum::http::header::CONTENT_TYPE, content_type.to_string()),
            (axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename={filename}")),
        ],
        body,
    )
        .into_response()
}

/// GET /api/memory/export - the viewer's memory as structured JSON (download)
pub async fn api_memory_export(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    match memory::export_doc(state.db.pool(), &session.username).await {
        Ok(doc) => attachment("application/json", format!("dgx-memory-{}.json", session.username), memory::py_dumps_indent(&doc)),
        Err(_) => db_down(),
    }
}

/// GET /api/memory/export.md - the viewer's memory as readable Markdown (download)
pub async fn api_memory_export_md(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let session = match viewer(&state, &headers).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    let platform = state.db.get_setting_set("platform_name").await.unwrap_or_else(|| "DGX".to_string());
    match memory::export_doc(state.db.pool(), &session.username).await {
        Ok(doc) => attachment(
            "text/markdown; charset=utf-8",
            format!("dgx-memory-{}.md", session.username),
            memory::export_markdown(&doc, &platform),
        ),
        Err(_) => db_down(),
    }
}

/// POST /api/memory/import - merge an exported JSON memory (the session is authoritative)
pub async fn api_memory_import(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    if body.len() > memory::IMPORT_MAX_BYTES {
        return (StatusCode::PAYLOAD_TOO_LARGE, Json(json!({"ok": false, "error": "Fichier trop volumineux."}))).into_response();
    }
    let Ok(mut doc) = serde_json::from_slice::<Value>(&body) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "JSON invalide."}))).into_response();
    };
    if let Some(err) = memory::validate_import(&mut doc) {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": err}))).into_response();
    }
    let db = state.db.pool();
    let user = session.username.as_str();
    let (mut imported_nodes, mut imported_facts) = (0i64, 0i64);
    for n in doc["nodes"].as_array().into_iter().flatten() {
        let name = n["name"].as_str().unwrap_or_default();
        let kind = n["kind"].as_str().unwrap_or("subject");
        let Ok(found) = memory::node(db, user, name, kind, true).await else { return db_down() };
        let Some(node_id) = found else { continue };
        imported_nodes += 1;
        for alias in n["aliases"].as_array().into_iter().flatten().filter_map(Value::as_str) {
            if memory::add_alias(db, user, node_id, alias).await.is_err() {
                return db_down();
            }
        }
    }
    let generic = json!(memory::MEM_GENERIC_RELATION);
    let subject_kind = json!("subject");
    for e in doc["edges"].as_array().into_iter().flatten() {
        let subject = memory::py_strip(e["subject"].as_str().unwrap_or_default()).to_string();
        let fact: String = memory::py_strip(e["fact"].as_str().unwrap_or_default()).chars().take(memory::MEM_MAX_FACT_LEN).collect();
        if subject.is_empty() || fact.is_empty() {
            continue;
        }
        match memory::fact_exists(db, user, &subject, &fact).await {
            Err(_) => return db_down(),
            Ok(true) => continue,
            Ok(false) => {}
        }
        let relation = e.get("relation").filter(|v| keys::truthy(v)).unwrap_or(&generic);
        let source = if e["source"] == "user" { "user" } else { "model" };
        match memory::add_fact(db, user, Some(&json!(subject)), Some(relation), Some(&json!(fact)), e.get("object"), source, Some(&subject_kind)).await {
            Ok((_, true)) => imported_facts += 1,
            Ok((_, false)) => {}
            Err(_) => return db_down(),
        }
    }
    Json(json!({"ok": true, "imported_nodes": imported_nodes, "imported_facts": imported_facts})).into_response()
}

/// POST /api/memory/import.md - merge facts read from a Markdown file
pub async fn api_memory_import_md(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let session = match write_guard(&state, &headers, &body).await {
        Err(r) => return r,
        Ok(s) => s,
    };
    if body.len() > memory::IMPORT_MAX_BYTES {
        return (StatusCode::PAYLOAD_TOO_LARGE, Json(json!({"ok": false, "error": "Fichier trop volumineux."}))).into_response();
    }
    let Ok(text) = std::str::from_utf8(&body) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Encodage invalide (UTF-8 attendu)."}))).into_response();
    };
    let (edges, aliases) = memory::md_parse(text);
    if edges.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "Aucun fait reconnu dans ce fichier."}))).into_response();
    }
    if edges.len() as i64 > memory::MEM_MAX_FACTS {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"ok": false, "code": "too_many_facts",
                        "error": format!("Trop de faits dans ce fichier ({} ; {} au maximum).", edges.len(), memory::MEM_MAX_FACTS)})),
        )
            .into_response();
    }
    let db = state.db.pool();
    let user = session.username.as_str();
    let subject_kind = json!("subject");
    let mut imported_facts = 0i64;
    for e in &edges {
        let subject: String = memory::py_strip(&e.subject).chars().take(crate::memnorm::MEM_MAX_NAME_LEN).collect();
        let fact: String = memory::py_strip(&e.fact).chars().take(memory::MEM_MAX_FACT_LEN).collect();
        if subject.is_empty() || fact.is_empty() {
            continue;
        }
        match memory::fact_exists(db, user, &subject, &fact).await {
            Err(_) => return db_down(),
            Ok(true) => continue,
            Ok(false) => {}
        }
        let object = e.object.as_ref().map(|o| json!(o));
        match memory::add_fact(db, user, Some(&json!(subject)), Some(&json!(e.relation)), Some(&json!(fact)), object.as_ref(), "user", Some(&subject_kind)).await {
            Ok((_, true)) => imported_facts += 1,
            Ok((_, false)) => {}
            Err(_) => return db_down(),
        }
    }
    let mut imported_aliases = 0u64;
    for (subject, alias) in &aliases {
        let Ok(found) = memory::node(db, user, subject, "subject", false).await else { return db_down() };
        let Some(node_id) = found else { continue };
        match memory::add_alias(db, user, node_id, alias).await {
            Ok(n) => imported_aliases += n,
            Err(_) => return db_down(),
        }
    }
    Json(json!({"ok": true, "imported_facts": imported_facts, "imported_aliases": imported_aliases})).into_response()
}

