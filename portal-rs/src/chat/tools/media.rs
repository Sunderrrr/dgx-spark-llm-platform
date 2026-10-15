//! The chat's media tools (image_tools.py, video_tools.py, document_tools.py):
//! a job started on an EXPLICIT request, waited for with SSE heartbeats (the
//! proxy cuts an idle stream at 60 s), its result handed back to the model as
//! markdown it must keep as is. The video and OCR sidecars are started on
//! first use, under the memory guard.
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::chat::history::py_str;
use crate::chat::Out;
use crate::AppState;

const POLL: Duration = Duration::from_secs(2);
const HEARTBEAT: Duration = Duration::from_secs(15);
const IMAGE_TIMEOUT: Duration = Duration::from_secs(420);
const VIDEO_TIMEOUT: Duration = Duration::from_secs(900);
const VIDEO_JOB_MAX: Duration = Duration::from_secs(3600);
const DOC_TIMEOUT: Duration = Duration::from_secs(210);
const TEXT_MAX: usize = 20000;

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `_SIDECAR_MEM_NEED_GB`: what a sidecar must find free to load.
fn mem_need_gb(kind: &str) -> Option<f64> {
    match kind {
        "ocr" => Some(20.0),
        "video" => Some(28.0),
        "voice" => Some(15.0),
        "asr" => Some(5.0),
        "image" => Some(40.0),
        _ => None,
    }
}

/// `_mem_guard`: the refusal when starting `kind` risks an OOM.
pub fn mem_guard(kind: &str) -> Option<String> {
    let need = mem_need_gb(kind)?;
    let avail = crate::asr_ondemand::mem_available_gb()?;
    (avail < need).then(|| format!(
        "Mémoire insuffisante pour démarrer {kind} : {avail:.0} Go libres, ~{} Go requis. Arrête un autre backend, ou réduis le contexte du modèle de chat, puis réessaie.",
        need as i64
    ))
}

/// `sidecar_startable`.
pub fn startable(kind: &str) -> bool {
    mem_guard(kind).is_none()
}

static STARTS: std::sync::Mutex<Option<std::collections::HashMap<String, (Instant, bool)>>> = std::sync::Mutex::new(None);

/// `sidecar_autostart`: does `kind` answer, starting it first if needed? Ok, or the reason.
pub async fn autostart<F, Fut>(state: &AppState, kind: &str, ready: F, out: &Out, timeout: Duration) -> Result<(), String>
where
    F: Fn() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    if ready().await {
        return Ok(());
    }
    if let Some(reason) = mem_guard(kind) {
        return Err(reason);
    }
    let last = STARTS.lock().unwrap_or_else(|e| e.into_inner()).as_ref().and_then(|m| m.get(kind).copied());
    let recent = last.is_some_and(|(t, _)| t.elapsed() < Duration::from_secs(60));
    if recent && last.is_some_and(|(_, ok)| !ok) {
        return Err("le service est arrêté et son démarrage a échoué à l'instant".into());
    }
    if !recent {
        let (ok, detail) = crate::asr_ondemand::sidecar_action(&state.db, kind, "start", "système", "démarrage au premier usage (outil du playground)").await;
        STARTS.lock().unwrap_or_else(|e| e.into_inner()).get_or_insert_with(Default::default).insert(kind.to_string(), (Instant::now(), ok));
        if !ok {
            return Err(format!("le service est arrêté et n'a pas pu démarrer{detail}"));
        }
    }
    let end = Instant::now() + timeout;
    let mut next_beat = Instant::now() + HEARTBEAT;
    while Instant::now() < end {
        if ready().await {
            return Ok(());
        }
        tokio::time::sleep(POLL).await;
        if Instant::now() >= next_beat {
            out.send(": heartbeat\n\n").await;
            next_beat = Instant::now() + HEARTBEAT;
        }
    }
    Err("le service a démarré mais ne répond pas encore".into())
}

/// `wait_media_job`: polls a job row until it leaves running/pending, heartbeats between.
async fn wait_job(state: &AppState, table: &str, prompt_id: &str, username: &str, timeout: Duration, out: &Out) -> Option<(String, i64)> {
    let start = Instant::now();
    let mut next_beat = start + HEARTBEAT;
    let sql = format!("SELECT status, {} FROM {table} WHERE prompt_id=? AND username=?", if table == "image_jobs" { "done_count" } else { "0" });
    while start.elapsed() < timeout {
        tokio::time::sleep(POLL).await;
        let row: Option<(Option<String>, Option<i64>)> = sqlx::query_as(&sql).bind(prompt_id).bind(username).fetch_optional(state.db.pool()).await.ok().flatten();
        if let Some((status, done)) = row {
            let status = status.unwrap_or_default();
            if status != "running" && status != "pending" {
                return Some((status, done.unwrap_or(0)));
            }
        }
        if Instant::now() >= next_beat {
            out.send(": heartbeat\n\n").await;
            next_beat = Instant::now() + HEARTBEAT;
        }
    }
    None
}

/// Python's `int(x or default)` on a JSON value; None where it raises.
fn int_or(args: &Value, key: &str, default: i64) -> Option<i64> {
    match args.get(key) {
        None => Some(default),
        Some(v) if !crate::keys::truthy(v) => Some(default),
        Some(v) => crate::handlers::py_int(v),
    }
}

pub fn image_tools() -> Vec<Value> {
    vec![json!({"type": "function", "function": {
        "name": "generate_image",
        "description": "Génère une image à partir d'une description. Réécris la demande en prompt visuel détaillé (sujet, style, lumière, cadrage) — idéalement en anglais, les modèles de diffusion y répondent mieux. Ne l'appelle que sur une demande explicite de l'utilisateur.",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "Description visuelle détaillée de l'image à produire."},
            "format": {"type": "string", "enum": ["png", "jpeg", "webp"], "description": "Format du fichier (défaut png)."},
            "width": {"type": "integer", "description": "Largeur en pixels (256–1536, défaut 1024)."},
            "height": {"type": "integer", "description": "Hauteur en pixels (256–1536, défaut 1024)."},
            "count": {"type": "integer", "description": "Nombre d'images à produire (1-4, défaut 1)."},
            "output_width": {"type": "integer", "description": "Largeur FINALE après agrandissement (jusqu'à 3840 — la 4K — défaut : la taille de génération)."},
            "output_height": {"type": "integer", "description": "Hauteur FINALE après agrandissement (jusqu'à 3840)."},
        }, "required": ["prompt"]}}})]
}

pub fn announce_image(args: &Value) -> Value {
    let count = int_or(args, "count", 1).unwrap_or(1).clamp(1, 4);
    json!({"step": "generation", "tool": "diffusers", "count": count, "question": take(&args.get("prompt").map(py_str).unwrap_or_default(), 200)})
}

/// `_exec_image_tool`.
pub async fn exec_image(state: &AppState, args: &Value, username: &str, step_log: &mut Vec<Value>, out: &Out) -> String {
    let prompt = take(crate::memory::py_strip(&args.get("prompt").map(py_str).unwrap_or_default()), 10000);
    if prompt.is_empty() {
        return "Aucun prompt fourni : impossible de générer une image.".into();
    }
    let fmt_raw = args.get("format").filter(|v| crate::keys::truthy(v)).map(py_str).unwrap_or_else(|| "png".into());
    let fmt = crate::handlers::tool_image_format(&fmt_raw);
    let dim = |k: &str| int_or(args, k, 1024).unwrap_or(1024).clamp(256, 1536);
    let out_dim = |k: &str| int_or(args, k, 0).unwrap_or(0).clamp(0, 3840);
    let count = int_or(args, "count", 1).unwrap_or(1).clamp(1, 4);
    if !crate::guards::job_slot(username) {
        return "Trop de générations d'images en cours pour ce compte — attends la fin des précédentes.".into();
    }
    let mut b = [0u8; 12];
    let _ = getrandom::getrandom(&mut b);
    let pid = hex::encode(b);
    let inserted = sqlx::query("INSERT INTO image_jobs (username, prompt_id, prompt, status, count, done_count, format, created_at) VALUES (?,?,?,?,?,?,?,?)")
        .bind(username).bind(&pid).bind(take(&prompt, 2000)).bind("running").bind(count).bind(0).bind(fmt)
        .bind(crate::keys::iso(&chrono::Local::now().naive_local()))
        .execute(state.db.pool()).await;
    if inserted.is_err() {
        crate::guards::job_done(username);
        return "La génération d'image a échoué (sidecar indisponible ou surchargé). Dis-le tel quel à l'utilisateur.".into();
    }
    crate::handlers::spawn_image_worker(state.db.clone(), username.to_string(), pid.clone(), prompt.clone(), count, fmt, (dim("width"), dim("height")), (out_dim("output_width"), out_dim("output_height")));
    let (status, done) = wait_job(state, "image_jobs", &pid, username, IMAGE_TIMEOUT, out).await.unwrap_or(("running".into(), 0));
    let urls: Vec<String> = if status == "done" { (0..done).map(|i| format!("/image/file/{pid}/{i}")).collect() } else { Vec::new() };
    step_log.push(json!({"step": "generation_done", "tool": "diffusers", "question": take(&prompt, 200), "prompt_id": pid, "images": urls,
        "error": if urls.is_empty() { json!("La génération a échoué.") } else { Value::Null }}));
    if urls.is_empty() {
        return "La génération d'image a échoué (sidecar indisponible ou surchargé). Dis-le tel quel à l'utilisateur.".into();
    }
    format!("Image générée. Intègre-la TELLE QUELLE dans ta réponse en markdown (ne modifie jamais l'adresse) :\n{}",
        urls.iter().map(|u| format!("![image générée]({u})")).collect::<Vec<_>>().join("\n"))
}

pub fn video_tools() -> Vec<Value> {
    vec![json!({"type": "function", "function": {
        "name": "generate_video",
        "description": "Génère une courte vidéo à partir d'une description. Réécris la demande en description de SCÈNE détaillée (sujet, mouvement, cadrage, caméra, lumière) — idéalement en anglais. Ne l'appelle que sur une demande explicite de l'utilisateur.",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "Description détaillée de la scène à filmer."},
            "duration": {"type": "integer", "description": "Durée en secondes (2 à 15, défaut 5)."},
        }, "required": ["prompt"]}}})]
}

pub fn announce_video(args: &Value) -> Value {
    json!({"step": "generation_video", "tool": "comfyui", "question": take(&args.get("prompt").map(py_str).unwrap_or_default(), 200)})
}

/// `_exec_video_tool`.
pub async fn exec_video(state: &AppState, args: &Value, username: &str, step_log: &mut Vec<Value>, out: &Out) -> String {
    let prompt = take(crate::memory::py_strip(&args.get("prompt").map(py_str).unwrap_or_default()), 10000);
    if prompt.is_empty() {
        return "Aucune description fournie : impossible de générer une vidéo.".into();
    }
    let duration = match args.get("duration") {
        Some(v) if crate::keys::truthy(v) => match v {
            Value::Number(n) => n.as_f64().unwrap_or(5.0),
            Value::String(s) => s.trim().parse::<f64>().unwrap_or(5.0),
            Value::Bool(b) => f64::from(u8::from(*b)),
            _ => 5.0,
        },
        _ => 5.0,
    }
    .clamp(2.0, 15.0);
    if let Err(reason) = autostart(state, "video", crate::sidecars::comfyui_up, out, Duration::from_secs(240)).await {
        step_log.push(json!({"step": "generation_video_done", "tool": "comfyui", "question": take(&prompt, 200), "videos": [], "error": reason}));
        return format!("Le service vidéo est indisponible : {reason}. Dis-le tel quel à l'utilisateur.");
    }
    if !crate::guards::job_slot(username) {
        return "Trop de générations vidéo en cours pour ce compte — attends la fin des précédentes.".into();
    }
    let Some(pid) = crate::handlers::comfyui_generate(None, &prompt, duration).await else {
        crate::guards::job_done(username);
        step_log.push(json!({"step": "generation_video_done", "tool": "comfyui", "question": take(&prompt, 200), "videos": [], "error": "ComfyUI inaccessible ou requête refusée."}));
        return "La génération vidéo a échoué (sidecar indisponible ou surchargé). Dis-le tel quel à l'utilisateur.".into();
    };
    let created = crate::keys::iso(&chrono::Local::now().naive_local());
    let _ = sqlx::query("INSERT INTO video_jobs (username, prompt_id, prompt, status, created_at, req_duration_s) VALUES (?,?,?,?,?,?)")
        .bind(username).bind(&pid).bind(take(&prompt, 2000)).bind("running").bind(&created).bind(duration.trunc() as i64)
        .execute(state.db.pool()).await;
    // `_video_worker`: somebody must close the job, even when the chat client left.
    let (st, user, job, created2) = (state.clone(), username.to_string(), pid.clone(), created.clone());
    tokio::spawn(async move {
        let start = Instant::now();
        let mut output = json!({"status": "error"});
        while start.elapsed() < VIDEO_JOB_MAX {
            tokio::time::sleep(POLL).await;
            let s = crate::handlers::comfyui_status(&job).await;
            if matches!(s["status"].as_str(), Some("done" | "error")) {
                output = s;
                break;
            }
        }
        crate::handlers::persist_video(&st, &job, &user, &output, Some(&created2), None).await;
        crate::guards::job_done(&user);
    });
    let status = wait_job(state, "video_jobs", &pid, username, VIDEO_TIMEOUT, out).await.map(|(s, _)| s).unwrap_or_else(|| "running".into());
    if status == "running" {
        step_log.push(json!({"step": "generation_video_done", "tool": "comfyui", "question": take(&prompt, 200), "videos": [], "error": "Génération toujours en cours."}));
        return "La vidéo est encore en cours de génération (elle apparaîtra dans l'historique de la page Vidéo). Ne donne PAS d'adresse pour l'instant ; dis simplement que la génération continue.".into();
    }
    let urls: Vec<String> = if status == "done" { vec![format!("/video/file/{pid}")] } else { Vec::new() };
    step_log.push(json!({"step": "generation_video_done", "tool": "comfyui", "question": take(&prompt, 200), "videos": urls,
        "error": if urls.is_empty() { json!("La génération a échoué.") } else { Value::Null }}));
    if urls.is_empty() {
        return "La génération vidéo a échoué (sidecar indisponible ou surchargé). Dis-le tel quel à l'utilisateur.".into();
    }
    format!("Vidéo générée. Intègre-la TELLE QUELLE dans ta réponse en markdown (ne modifie jamais l'adresse) :\n{}",
        urls.iter().map(|u| format!("[vidéo générée]({u})")).collect::<Vec<_>>().join("\n"))
}

pub fn document_tools() -> Vec<Value> {
    vec![json!({"type": "function", "function": {
        "name": "lire_document",
        "description": "Lit (OCR) une image jointe à la conversation et en extrait le TEXTE : document scanné, photo d'une page, capture d'écran. À appeler quand l'utilisateur demande explicitement le contenu d'une pièce jointe — pour une simple description d'image, décris-la toi-même.",
        "parameters": {"type": "object", "properties": {
            "fichier": {"type": "string", "description": "Nom ou numéro de la pièce jointe à lire (la plus récente par défaut)."},
        }, "required": []}}})]
}

pub fn announce_document(args: &Value) -> Value {
    json!({"step": "ocr", "tool": "unlimited-ocr", "question": take(&args.get("fichier").map(py_str).unwrap_or_default(), 200)})
}

/// `_attachment`: the attachment a name points to (its LAST number), else the newest.
pub fn attachment<'a>(attachments: &'a [String], file: Option<&Value>) -> Option<(&'a String, String)> {
    let last = attachments.last()?;
    let name = crate::memory::py_strip(&file.filter(|v| crate::keys::truthy(v)).map(py_str).unwrap_or_default()).to_string();
    static DIGITS: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let digits = DIGITS.get_or_init(|| regex::Regex::new(r"\d+").expect("static regex"));
    if let Some(n) = digits.find_iter(&name).last().and_then(|m| m.as_str().parse::<usize>().ok()) {
        if (1..=attachments.len()).contains(&n) {
            let shown = if name.is_empty() { format!("pièce jointe {n}") } else { name };
            return Some((&attachments[n - 1], shown));
        }
    }
    Some((last, name))
}

/// `_exec_document_tool`.
pub async fn exec_document(state: &AppState, args: &Value, step_log: &mut Vec<Value>, attachments: &[String], out: &Out) -> String {
    use base64::Engine;
    let Some((target, name)) = attachment(attachments, args.get("fichier")) else {
        return "Aucune image jointe à cette conversation : je ne peux lire aucun document. Demande à l'utilisateur d'en joindre une.".into();
    };
    let ready = || async { crate::sidecars::ocr_model().await.is_some() };
    if let Err(reason) = autostart(state, "ocr", ready, out, Duration::from_secs(240)).await {
        step_log.push(json!({"step": "ocr_done", "tool": "unlimited-ocr", "question": name, "chars": 0, "error": reason}));
        return format!("Le service de lecture est indisponible : {reason}. Dis-le tel quel à l'utilisateur.");
    }
    let (header, b64) = target.split_once(',').unwrap_or((target.as_str(), ""));
    let mime = header.trim_start_matches("data:").split(';').next().filter(|m| !m.is_empty()).unwrap_or("image/png").to_string();
    let bytes = base64::engine::general_purpose::STANDARD.decode(b64).unwrap_or_default();
    let job = tokio::spawn(async move { crate::handlers::ocr_extract_text(&bytes, &mime, "document parsing.").await });
    let start = Instant::now();
    let mut next_beat = start + HEARTBEAT;
    tokio::pin!(job);
    let res = loop {
        tokio::select! {
            r = &mut job => break Some(r),
            _ = tokio::time::sleep(POLL) => {
                if start.elapsed() >= DOC_TIMEOUT { break None; }
                if Instant::now() >= next_beat {
                    out.send(": heartbeat\n\n").await;
                    next_beat = Instant::now() + HEARTBEAT;
                }
            }
        }
    };
    let Some(res) = res else {
        step_log.push(json!({"step": "ocr_done", "tool": "unlimited-ocr", "question": name, "chars": 0, "error": "Lecture interrompue : trop longue."}));
        return "La lecture du document a pris trop de temps et a été interrompue. Dis-le tel quel à l'utilisateur.".into();
    };
    let (text, err) = match res {
        Ok(Ok(t)) => (crate::memory::py_strip(&t).to_string(), None),
        Ok(Err(e)) => (String::new(), Some(take(&e, 120))),
        Err(e) => (String::new(), Some(take(&e.to_string(), 120))),
    };
    if text.is_empty() {
        let reason = err.unwrap_or_else(|| "service en difficulté ou image illisible".into());
        step_log.push(json!({"step": "ocr_done", "tool": "unlimited-ocr", "question": name, "chars": 0, "error": reason}));
        return format!("Aucun texte n'a pu être extrait de ce document ({reason}). Dis-le tel quel à l'utilisateur.");
    }
    step_log.push(json!({"step": "ocr_done", "tool": "unlimited-ocr", "question": name, "chars": text.chars().count(), "error": null}));
    format!("Texte extrait du document par OCR (brut, dactylographié ou manuscrit — cite-le, ne le réécris pas) :\n\n{}", take(&text, TEXT_MAX))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn attachments_are_aimed_by_their_last_number() {
        let a: Vec<String> = vec!["a".into(), "b".into(), "c".into()];
        assert_eq!(attachment(&a, Some(&json!("image-2.jpg"))).map(|(x, n)| (x.clone(), n)), Some(("b".into(), "image-2.jpg".into())));
        assert_eq!(attachment(&a, Some(&json!(9))).map(|(x, _)| x.clone()), Some("c".into()));
        assert_eq!(attachment(&a, None).map(|(x, n)| (x.clone(), n)), Some(("c".into(), "".into())));
        assert_eq!(attachment(&a, Some(&json!(1))).map(|(x, n)| (x.clone(), n)), Some(("a".into(), "1".into())));
        assert!(attachment(&[], None).is_none());
    }
}
