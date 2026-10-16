//! The Admin's sidecar buttons (start / stop of OCR, voice, ASR, image and
//! video) and the catalog deletions of OCR and voice models — admin_models.py.
//! Every answer is the JSON contract of the admin actions: `{ok, error}`, with
//! a memory refusal at 507 and a failed runner call at 503.
use super::*;
use super::admin_writes::{admin_form, json_error, json_ok};

/// `_sidecar_start_json(kind)` / `_sidecar_stop_json(kind)`.
async fn sidecar(state: AppState, headers: HeaderMap, body: axum::body::Bytes, path: &str, kind: &'static str, action: &'static str) -> Response {
    let (s, _) = match admin_form(&state, &headers, &body, path).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    if action == "start" {
        if let Some(err) = crate::chat::tools::media::mem_guard(kind) {
            return (StatusCode::INSUFFICIENT_STORAGE, Json(json!({"ok": false, "error": err}))).into_response();
        }
    }
    let (ok, detail) = crate::launches::sidecar_action(kind, action).await;
    if kind == "asr" && ok {
        // A manual start pins the dictation sidecar against the idle reaper, a
        // manual stop clears the pin (asr_epingler in Flask).
        if action == "start" {
            crate::asr_ondemand::pin();
        } else {
            crate::asr_ondemand::unpin();
        }
    }
    let outcome = format!("{kind} : {}", if ok { "OK".to_string() } else { format!("échec{detail}") });
    crate::sessions::log_audit(state.db.pool(), &s.username, &format!("sidecar.{action}"), &outcome).await;
    let (code, error) = match (ok, action) {
        (true, _) => (StatusCode::OK, Value::Null),
        (false, "start") => (StatusCode::SERVICE_UNAVAILABLE, json!(format!("Échec du démarrage {kind}.{detail}"))),
        (false, _) => (StatusCode::SERVICE_UNAVAILABLE, json!(format!("Échec de l'arrêt {kind}.{detail}"))),
    };
    (code, Json(json!({"ok": ok, "error": error}))).into_response()
}

macro_rules! sidecar_route {
    ($name:ident, $kind:literal, $action:literal, $path:literal) => {
        pub async fn $name(State(state): State<AppState>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
            sidecar(state, headers, body, $path, $kind, $action).await
        }
    };
}

sidecar_route!(admin_ocr_start, "ocr", "start", "/admin/ocr/start");
sidecar_route!(admin_ocr_stop, "ocr", "stop", "/admin/ocr/stop");
sidecar_route!(admin_voice_start, "voice", "start", "/admin/voice/start");
sidecar_route!(admin_voice_stop, "voice", "stop", "/admin/voice/stop");
sidecar_route!(admin_asr_start, "asr", "start", "/admin/asr/start");
sidecar_route!(admin_asr_stop, "asr", "stop", "/admin/asr/stop");
sidecar_route!(admin_image_start, "image", "start", "/admin/image/start");
sidecar_route!(admin_image_stop, "image", "stop", "/admin/image/stop");
sidecar_route!(admin_video_start, "video", "start", "/admin/video/start");
sidecar_route!(admin_video_stop, "video", "stop", "/admin/video/stop");

/// `POST /admin/ocr/catalog/delete/<int:cid>` and the voice twin.
async fn catalog_delete(state: AppState, headers: HeaderMap, body: axum::body::Bytes, path: String, table: &'static str, audit: &'static str, missing: &'static str, done: &'static str) -> Response {
    let (s, _) = match admin_form(&state, &headers, &body, &path).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let id: i64 = path.rsplit('/').next().and_then(|v| v.parse().ok()).unwrap_or(-1);
    let row: Option<(String,)> = sqlx::query_as(&format!("SELECT name FROM {table} WHERE id=?")).bind(id).fetch_optional(state.db.pool()).await.ok().flatten();
    let Some((name,)) = row else {
        return json_error(missing, StatusCode::NOT_FOUND);
    };
    let _ = sqlx::query(&format!("DELETE FROM {table} WHERE id=?")).bind(id).execute(state.db.pool()).await;
    crate::sessions::log_audit(state.db.pool(), &s.username, audit, &name).await;
    json_ok(done, json!({}))
}

/// POST /admin/ocr/catalog/delete/<int:cid>
pub async fn admin_ocr_catalog_delete(State(state): State<AppState>, Path(cid): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    catalog_delete(state, headers, body, format!("/admin/ocr/catalog/delete/{cid}"), "ocr_configs", "ocr.catalog.delete", "Modèle OCR introuvable.", "Modèle OCR supprimé du catalogue.").await
}

/// POST /admin/voice/catalog/delete/<int:cid>
pub async fn admin_voice_catalog_delete(State(state): State<AppState>, Path(cid): Path<String>, headers: HeaderMap, body: axum::body::Bytes) -> Response {
    catalog_delete(state, headers, body, format!("/admin/voice/catalog/delete/{cid}"), "voice_configs", "voice.catalog.delete", "Modèle voix introuvable.", "Modèle voix supprimé du catalogue.").await
}
