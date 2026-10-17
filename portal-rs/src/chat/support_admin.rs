//! The Support's admin tools (support.py, the `is_admin` branches): reading a
//! service's log tail, starting or stopping a media sidecar, and launching or
//! stopping the chat model. Served only to admin sessions: the tool schemas
//! are offered to them alone, and the callers in `support_tools` refuse the
//! rest. `launch_model`, `stop_model` and `manage_service` are GUARDED: they
//! run from `/support/confirm`, never from the chat loop.
use std::sync::OnceLock;

use serde_json::{json, Value};
use sqlx::SqlitePool;

use crate::chat::support_tools::{arg, SERVICE_KINDS};

/// Services whose log tail the Support may read: the chat model's runner
/// buffer, the LiteLLM gateway's file, and the media sidecars.
pub const SERVICE_LOG_KINDS: [&str; 7] = ["model", "litellm", "ocr", "video", "voice", "asr", "image"];

/// The admin tools' names: the ones `support_tools::exec` hands to this module.
pub const ADMIN_TOOLS: [&str; 4] = ["read_logs", "manage_service", "launch_model", "stop_model"];

/// `_LOG_NOISE_RE`: the access lines of the probes, dropped from a tail.
fn log_noise(line: &str) -> bool {
    static NOISE: OnceLock<regex::Regex> = OnceLock::new();
    NOISE
        .get_or_init(|| regex::Regex::new(r#""GET /(?:v1/models|metrics|health\S*|version|ping)\b"#).expect("static regex"))
        .is_match(line)
}

fn last(lines: Vec<String>, n: usize) -> Vec<String> {
    lines[lines.len().saturating_sub(n)..].to_vec()
}

/// `_support_tools(True)`: the four admin schemas, appended to the self-service ones.
pub fn tools() -> Vec<Value> {
    vec![
        json!({"type": "function", "function": {
            "name": "launch_model",
            "description": "(Admin) Lance un modèle du catalogue par son nom. Remplace le modèle actif — confirmer avant.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "description": "Nom du modèle dans le catalogue."}},
                "required": ["name"]}}}),
        json!({"type": "function", "function": {
            "name": "stop_model",
            "description": "(Admin) Arrête le modèle actuellement chargé. Confirmer avant.",
            "parameters": {"type": "object", "properties": {}}}}),
        json!({"type": "function", "function": {
            "name": "read_logs",
            "description": "(Admin) Lit la fin des logs d'un service (model, litellm, ocr, video, voice, asr, image). Les logs ne sont PAS dans ton contexte : appelle cet outil quand un service ne répond pas — la cause exacte (erreur de chargement, OOM, dépendance absente) y est écrite. Lecture seule, aucun effet de bord.",
            "parameters": {"type": "object", "properties": {
                "service": {"type": "string", "enum": SERVICE_LOG_KINDS, "description": "Le service dont lire les logs."},
                "lines": {"type": "integer", "description": "Nombre de lignes à lire, défaut 80 (max 300)."}},
                "required": ["service"]}}}),
        json!({"type": "function", "function": {
            "name": "manage_service",
            "description": "(Admin) Démarre, redémarre ou arrête un service de la plateforme (ocr, video, voice, asr, image). À utiliser quand un service est arrêté, en échec ou bloqué au démarrage : l'outil renvoie son nouvel état. Confirmer avant.",
            "parameters": {"type": "object", "properties": {
                "service": {"type": "string", "enum": SERVICE_KINDS, "description": "Le service concerné."},
                "action": {"type": "string", "enum": ["start", "restart", "stop"],
                    "description": "start = démarrer un service arrêté ; restart = le relancer (utile s'il est bloqué ou en échec) ; stop = l'arrêter."}},
                "required": ["service", "action"]}}}),
    ]
}

/// `_exec_support_tool` for an admin tool. Only called for an admin session.
pub async fn exec(pool: &SqlitePool, name: &str, args: &Value, username: &str) -> (String, bool) {
    match name {
        "read_logs" => read_logs(args).await,
        "manage_service" => manage_service(pool, args, username).await,
        "launch_model" => launch_model(pool, args).await,
        _ => stop_model().await,
    }
}

/// `int(args.get('lines') or 80)` clamped to 1..=300; anything unreadable is 80.
fn lines_arg(args: &Value) -> usize {
    let n: Option<i64> = match args.get("lines").filter(|v| crate::keys::truthy(v)) {
        None => Some(80),
        Some(Value::Number(x)) => x.as_f64().map(|f| f.trunc() as i64),
        Some(Value::String(s)) => s.trim().parse().ok(),
        Some(Value::Bool(b)) => Some(i64::from(*b)),
        Some(_) => None,
    };
    n.unwrap_or(80).clamp(1, 300) as usize
}

/// `sidecar_logs(kind, n)`: the tail of a service's log. Empty when unreachable.
pub async fn sidecar_logs(kind: &str, n: usize) -> Vec<String> {
    match kind {
        "model" => crate::handlers::admin_routes::runner_logs(n).await.iter().filter_map(|v| v.as_str().map(str::to_string)).collect(),
        // The proxy writes to the shared volume, read directly (no runner, no
        // restart of the served model). The `.1` file is the rotated one.
        "litellm" => {
            let mut out: Vec<String> = Vec::new();
            for path in ["/run/dgx/litellm.log", "/run/dgx/litellm.log.1"] {
                if let Ok(bytes) = tokio::fs::read(path).await {
                    out.extend(String::from_utf8_lossy(&bytes).lines().map(str::to_string));
                }
            }
            let kept: Vec<String> = out.into_iter().filter(|l| !log_noise(l)).collect();
            last(kept, n).into_iter().map(|l| l.trim_end().chars().take(400).collect()).collect()
        }
        _ => {
            let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
            let fetched = async {
                let c = reqwest::Client::builder().timeout(std::time::Duration::from_secs(10)).build().ok()?;
                let r = c.get(format!("{}/{kind}/logs", crate::probes::runner_url())).bearer_auth(token).send().await.ok()?;
                if !r.status().is_success() {
                    return None;
                }
                r.json::<Value>().await.ok()
            };
            let Some(v) = fetched.await else { return Vec::new() };
            let lines: Vec<String> = v.get("logs").and_then(Value::as_array).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect()).unwrap_or_default();
            let kept: Vec<String> = lines.into_iter().filter(|l| !log_noise(l)).collect();
            last(kept, n).into_iter().map(|l| l.chars().take(400).collect()).collect()
        }
    }
}

async fn read_logs(args: &Value) -> (String, bool) {
    let kind = arg(args, "service").to_lowercase();
    if !SERVICE_LOG_KINDS.contains(&kind.as_str()) {
        return (format!("Service inconnu : « {kind} » (attendu : {}).", SERVICE_LOG_KINDS.join(", ")), false);
    }
    let lines = sidecar_logs(&kind, lines_arg(args)).await;
    if lines.is_empty() {
        return (format!("Aucun log disponible pour « {kind} » : service injoignable, ou tampon vide (rien n'a encore été écrit)."), true);
    }
    (format!("Logs de « {kind} » ({} dernières lignes) :\n{}", lines.len(), lines.join("\n")), true)
}

/// `_sidecar_action(kind, action)` as the Support calls it: the runner's answer,
/// audited under the session user (the same line the admin routes write).
async fn sidecar_act(pool: &SqlitePool, username: &str, kind: &str, action: &str) -> (bool, String) {
    let (ok, detail) = crate::launches::sidecar_action(kind, action).await;
    let outcome = if ok { "OK".to_string() } else { format!("échec{detail}") };
    crate::sessions::log_audit(pool, username, &format!("sidecar.{action}"), &format!("{kind} : {outcome}")).await;
    (ok, detail)
}

async fn manage_service(pool: &SqlitePool, args: &Value, username: &str) -> (String, bool) {
    let kind = arg(args, "service").to_lowercase();
    let action = arg(args, "action").to_lowercase();
    if !SERVICE_KINDS.contains(&kind.as_str()) {
        return (format!("Service inconnu : « {kind} » (attendu : {}).", SERVICE_KINDS.join(", ")), false);
    }
    if !["start", "restart", "stop"].contains(&action.as_str()) {
        return (format!("Action inconnue : « {action} » (attendu : start, restart, stop)."), false);
    }
    // The memory guard belongs to the START path: on unified memory a sidecar
    // that overflows takes the chat model down with it.
    if action != "stop" {
        if let Some(err) = crate::chat::tools::media::mem_guard(&kind) {
            return (err, false);
        }
    }
    if action == "restart" {
        sidecar_act(pool, username, &kind, "stop").await;
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
    let (ok, detail) = sidecar_act(pool, username, &kind, if action == "stop" { "stop" } else { "start" }).await;
    let state = crate::chat::support_tools::sidecar_status(&kind).await;
    (format!("{kind} : {state}{}", if detail.is_empty() { String::new() } else { format!(" — {detail}") }), ok)
}

async fn launch_model(pool: &SqlitePool, args: &Value) -> (String, bool) {
    let mname = arg(args, "name");
    let cfg: Option<(Option<String>, Option<String>, Option<String>, Option<String>)> =
        sqlx::query_as("SELECT hf_model_id, name, vllm_args, engine FROM model_configs WHERE name=?").bind(&mname).fetch_optional(pool).await.ok().flatten();
    let Some((hf, name, vllm_args, engine)) = cfg else {
        return (format!("Modèle « {mname} » introuvable dans le catalogue."), false);
    };
    let (ok, reason, uncertain) = crate::launches::launch_chat(
        pool,
        &hf.unwrap_or_default(),
        &name.unwrap_or_default(),
        &vllm_args.unwrap_or_default(),
        &engine.filter(|e| !e.is_empty()).unwrap_or_else(|| "vllm".into()),
    )
    .await;
    if ok {
        return (format!("Lancement de « {mname} » demandé (démarrage en cours)."), true);
    }
    if uncertain {
        return (format!("Le runner n'a pas répondu dans le délai : le lancement de « {mname} » est peut-être en cours. Vérifie l'état avant de relancer."), false);
    }
    (format!("Lancement refusé : {reason}"), false)
}

async fn stop_model() -> (String, bool) {
    let (ok, reason, uncertain) = crate::launches::stop_chat().await;
    if ok {
        return ("Modèle arrêté.".into(), true);
    }
    if uncertain {
        return ("Le runner n'a pas répondu dans le délai : l'arrêt est peut-être en cours.".into(), false);
    }
    (format!("Échec de l'arrêt : {reason}"), false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines_default_and_clamp() {
        assert_eq!(lines_arg(&json!({})), 80);
        assert_eq!(lines_arg(&json!({"lines": 0})), 80);
        assert_eq!(lines_arg(&json!({"lines": 12})), 12);
        assert_eq!(lines_arg(&json!({"lines": 12.9})), 12);
        assert_eq!(lines_arg(&json!({"lines": " 40 "})), 40);
        assert_eq!(lines_arg(&json!({"lines": "1.5"})), 80);
        assert_eq!(lines_arg(&json!({"lines": 5000})), 300);
        assert_eq!(lines_arg(&json!({"lines": -3})), 1);
    }

    #[test]
    fn probe_noise_is_dropped_like_python() {
        assert!(log_noise(r#"INFO: 127.0.0.1 "GET /metrics HTTP/1.1" 200"#));
        assert!(log_noise(r#""GET /v1/models HTTP/1.1""#));
        assert!(!log_noise(r#""GET /v1/chat/completions HTTP/1.1""#));
        assert_eq!(last(vec!["a".into(), "b".into(), "c".into()], 2), vec!["b".to_string(), "c".to_string()]);
    }
}
