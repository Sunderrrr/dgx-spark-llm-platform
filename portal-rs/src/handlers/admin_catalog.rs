//! The chat models' catalog (admin_models.py): add, edit the engine args,
//! delete (route at LiteLLM, then the weights on disk unless another entry
//! uses them). Launching and stopping stay with Flask.
use super::*;
use super::admin_writes::{add_announcement, admin_form, field, json_error, json_ok};

/// `re.sub(r'[^a-zA-Z0-9_-]', '-', name)[:40]`.
fn catalog_name(raw: &str) -> String {
    raw.chars().map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '-' { c } else { '-' }).take(40).collect()
}

/// `<int:mid>`: digits only, else werkzeug's 404 before any other check.
fn int_id(raw: &str) -> Option<i64> {
    (!raw.is_empty() && raw.bytes().all(|c| c.is_ascii_digit())).then(|| raw.parse().ok()).flatten()
}

/// POST /admin/model/add - a catalog entry, routed by LiteLLM at once.
pub async fn admin_model_add(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/model/add").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let name = catalog_name(&field(&form, "name"));
    let hf_id = field(&form, "hf_model_id");
    let args = field(&form, "vllm_args");
    let mut engine = crate::memory::py_strip(form.get("engine").map(String::as_str).unwrap_or("vllm")).to_lowercase();
    if !["vllm", "llamacpp", "ds4", "exllamav3"].contains(&engine.as_str()) {
        engine = "vllm".into();
    }
    if name.is_empty() || hf_id.is_empty() {
        return json_error("Nom et HF model ID requis.", StatusCode::BAD_REQUEST);
    }
    let pool = state.db.pool();
    let r = sqlx::query("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) VALUES (?,?,?,?,?)")
        .bind(&name)
        .bind(&hf_id)
        .bind(&args)
        .bind(&engine)
        .bind(keys::iso(&chrono::Local::now().naive_local()))
        .execute(pool)
        .await;
    match r {
        Err(sqlx::Error::Database(e)) if e.is_unique_violation() => return json_error("Un modèle avec ce nom existe déjà.", StatusCode::CONFLICT),
        Err(_) => return auth::html_status(StatusCode::INTERNAL_SERVER_ERROR),
        Ok(_) => {}
    }
    add_announcement(pool, "model_add", &name, "").await;
    let ok = crate::litellm_models::register(&name, &args, &engine).await;
    let tail = if ok { "" } else { " — enregistrement LiteLLM ÉCHOUÉ" };
    crate::sessions::log_audit(pool, &s.username, "model.add", &format!("{name} ({engine}, {hf_id}){tail}")).await;
    if ok {
        json_ok(&format!("Modèle {name} ajouté ({engine}) et routé par LiteLLM."), json!({}))
    } else {
        json_ok(&format!("Modèle {name} ajouté ({engine})."), json!({"warning": "Enregistrement LiteLLM échoué : le modèle n'est pas routé."}))
    }
}

/// POST /admin/model/edit/<int:mid> - the engine args; the route's context
/// limits follow. The flags are checked by the runner at launch, not here.
pub async fn admin_model_edit(State(state): State<AppState>, Path(mid): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let Some(mid) = int_id(&mid) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let (s, form) = match admin_form(&state, &headers, &body, "/admin/model/edit/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let args = field(&form, "vllm_args");
    let pool = state.db.pool();
    let row: Option<(String, Option<String>)> = sqlx::query_as("SELECT name, engine FROM model_configs WHERE id=?").bind(mid).fetch_optional(pool).await.ok().flatten();
    let Some((name, engine)) = row else {
        return json_error("Modèle introuvable dans le catalogue.", StatusCode::NOT_FOUND);
    };
    let _ = sqlx::query("UPDATE model_configs SET vllm_args=? WHERE id=?").bind(&args).bind(mid).execute(pool).await;
    let engine = engine.filter(|e| !e.is_empty()).unwrap_or_else(|| "vllm".into());
    let ok = crate::litellm_models::register(&name, &args, &engine).await;
    let tail = if ok { "" } else { " — routage LiteLLM ÉCHOUÉ" };
    crate::sessions::log_audit(pool, &s.username, "model.edit", &format!("{name} — args mis à jour{tail}")).await;
    if ok {
        json_ok("Args du modèle mis à jour (routage LiteLLM rafraîchi).", json!({}))
    } else {
        json_ok(
            "Args du modèle mis à jour.",
            json!({"warning": "Rafraîchissement LiteLLM échoué : les limites de contexte annoncées par la passerelle peuvent être fausses."}),
        )
    }
}

/// `runner_delete_files(hf_model_id)`: (ok, bytes, reason); nothing to
/// erase counts as done.
async fn delete_files(hf_id: &str) -> (bool, u64, String) {
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let sent = crate::sidecars::http()
        .post(format!("{}/models/delete-files", crate::probes::runner_url()))
        .bearer_auth(token)
        .json(&json!({"hf_model_id": hf_id}))
        .timeout(std::time::Duration::from_secs(620))
        .send()
        .await;
    let r = match sent {
        Ok(r) => r,
        Err(e) => return (false, 0, format!("runner injoignable ({})", if e.is_timeout() { "ReadTimeout" } else { "ConnectionError" })),
    };
    let status = r.status();
    let body: Value = r.json().await.unwrap_or_else(|_| json!({}));
    if status.is_success() {
        let bytes = body.get("bytes").and_then(|b| b.as_u64().or_else(|| b.as_f64().map(|f| f as u64))).unwrap_or(0);
        return (true, bytes, String::new());
    }
    if status.as_u16() == 404 && body.get("absent").is_some_and(keys::truthy) {
        return (true, 0, "aucun fichier sur le disque".into());
    }
    let reason = body.get("error").filter(|v| keys::truthy(v)).and_then(Value::as_str).map(str::to_string).unwrap_or_else(|| format!("HTTP {}", status.as_u16()));
    (false, 0, reason)
}

/// POST /admin/model/delete/<int:mid> - the entry, its LiteLLM route, its
/// « added » announcement, and its weights unless another entry shares them.
pub async fn admin_model_delete(State(state): State<AppState>, Path(mid): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    let Some(mid) = int_id(&mid) else { return auth::html_status(StatusCode::NOT_FOUND) };
    let (s, _) = match admin_form(&state, &headers, &body, "/admin/model/delete/x").await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let pool = state.db.pool();
    let row: Option<(String, String)> = sqlx::query_as("SELECT name, hf_model_id FROM model_configs WHERE id=?").bind(mid).fetch_optional(pool).await.ok().flatten();
    let Some((name, hf_id)) = row else {
        return json_error("Modèle introuvable dans le catalogue.", StatusCode::NOT_FOUND);
    };
    // The served model: its weights are being read; stop it first.
    if crate::models::running_models().await.contains(&name) {
        return json_error(
            &format!("{name} est le modèle actuellement servi : arrête-le d'abord, puis supprime-le (ses fichiers seront effacés du disque)."),
            StatusCode::CONFLICT,
        );
    }
    let share: Vec<String> = sqlx::query_scalar("SELECT name FROM model_configs WHERE hf_model_id=? AND id<>? UNION SELECT name FROM ocr_configs WHERE hf_model_id=?")
        .bind(&hf_id)
        .bind(mid)
        .bind(&hf_id)
        .fetch_all(pool)
        .await
        .unwrap_or_default();
    let _ = sqlx::query("DELETE FROM model_configs WHERE id=?").bind(mid).execute(pool).await;
    let deregistered = crate::litellm_models::unregister(&name).await;
    let removed = sqlx::query("DELETE FROM announcements WHERE kind='model_add' AND a=?").bind(&name).execute(pool).await.map(|r| r.rows_affected()).unwrap_or(0);
    let mut warnings = Vec::new();
    let files = if !share.is_empty() {
        warnings.push(format!("Fichiers conservés : {} les utilise aussi.", share.join(", ")));
        format!("fichiers conservés (utilisés aussi par {})", share.join(", "))
    } else {
        let (ok, bytes, reason) = delete_files(&hf_id).await;
        if ok {
            let gib = 1u64 << 30;
            let size = if bytes >= gib {
                format!("{:.1} Gio", bytes as f64 / gib as f64)
            } else {
                format!("{} Mio", ((bytes as f64 / (1u64 << 20) as f64).round_ties_even() as u64).max(1))
            };
            if bytes > 0 { format!("{size} effacés du disque") } else { "aucun fichier sur le disque".to_string() }
        } else {
            warnings.push(format!("Les fichiers n'ont pas pu être effacés : {reason}"));
            format!("fichiers NON effacés ({reason})")
        }
    };
    if !deregistered {
        warnings.push("L'entrée LiteLLM n'a pas pu être retirée : elle survit sans ligne de catalogue. Vérifie la passerelle.".into());
    }
    crate::sessions::log_audit(
        pool,
        &s.username,
        "model.delete",
        &format!(
            "{name} — retiré du catalogue, LiteLLM {}, {files}, {removed} annonce(s) retirée(s)",
            if deregistered { "dérégistré" } else { "NON DÉRÉGISTRÉ" }
        ),
    )
    .await;
    let message = format!("{name} supprimé : catalogue, LiteLLM, {files}.");
    if warnings.is_empty() {
        json_ok(&message, json!({}))
    } else {
        json_ok(&message, json!({"warning": warnings.join(" ")}))
    }
}
