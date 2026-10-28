//! The decision model (Cloudflare Clef, GGUF) through the platform.
//!
//! A decision model answers TYPED questions about a `state` and returns
//! probabilities; it never generates text. llama.cpp serves it on
//! `/v1/systemone` only, so it gets its own `llama-server` on its own port
//! (runner `/decisions/*`), next to the chat model — never instead of it.
//!
//! Two callers, one route:
//!  - the « Décisions » page goes through the portal (`POST /api/decisions/run`),
//!    authenticated by the session, and the portal calls LiteLLM with the
//!    user's own API key — budgets and usage apply exactly as on chat;
//!  - an API client calls LiteLLM's `POST /v1/systemone` directly with its key.
//!
//! The validation is deliberately strict and here, not in the engine: a body
//! the System One schema does not allow would be rejected by llama.cpp far from
//! the user, with an error that names nothing.
use serde_json::{json, Map, Value};
use std::time::Duration;

/// A state is a document or a JSON blob; 64 KiB is already a long one, and
/// Clef evaluates everything in one batch.
pub const MAX_STATE_BYTES: usize = 64 * 1024;
/// The System One API allows 128; a page with more than 32 questions is a form,
/// not a decision.
pub const MAX_QUESTIONS: usize = 32;
/// llama.cpp's own limits for Clef (README: 255 options per choice question).
pub const MAX_CHOICES: usize = 255;
/// « score » levels, lowest first (System One: 2 to 10).
pub const MAX_LEVELS: usize = 10;

fn env(name: &str, default: &str) -> String {
    std::env::var(name).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

/// The public model name, the one registered in LiteLLM and shown by the page.
pub fn model_name() -> String {
    env("DECISIONS_NAME", "clef-q4km")
}

fn runner_url() -> String {
    crate::probes::runner_url().trim_end_matches('/').to_string()
}

fn runner_token() -> String {
    std::env::var("RUNNER_TOKEN").unwrap_or_default()
}

async fn runner(path: &str, body: Option<&Value>) -> Option<(u16, Value)> {
    let url = format!("{}{}", runner_url(), path);
    let req = match body {
        Some(b) => crate::sidecars::http().post(url).json(b).timeout(Duration::from_secs(60)),
        None => crate::sidecars::http().get(url).timeout(Duration::from_secs(5)),
    };
    let r = req.bearer_auth(runner_token()).send().await.ok()?;
    let code = r.status().as_u16();
    // A route the runner does not know answers 404 with no JSON: that is NOT
    // "the runner is down", and the page must not say so.
    if code == 404 {
        return Some((404, Value::Null));
    }
    let body = r.json::<Value>().await.ok()?;
    Some((code, body))
}

/// `GET /decisions/status` on the runner, as the page needs it: enabled, state,
/// port, memory left. An unreachable runner is a state, not an error.
pub async fn status() -> Value {
    let runner = runner("/decisions/status", None).await;
    let name = model_name();
    if let Some((404, _)) = runner {
        // The runner predates the decision routes (an update not yet shipped):
        // enabled=false + its own status, so the page names the real reason.
        return json!({
            "model": name,
            // The declaration, so the catalog can show what WILL be served
            // instead of a dash while the runner predates the routes.
            "ref": env("DECISIONS_MODEL", ""),
            "args": env("DECISIONS_ARGS", ""),
            "enabled": !env("DECISIONS_MODEL", "").is_empty(),
            "status": "unsupported",
            "port": null,
            "uptime_s": null,
            "libre_gib": crate::asr_ondemand::mem_available_gb(),
            "max_state_bytes": MAX_STATE_BYTES,
            "max_questions": MAX_QUESTIONS,
            "service": "runner",
        });
    }
    match runner.map(|(_, v)| v) {
        Some(v) => json!({
            "model": name,
            "ref": v.get("ref").cloned().filter(|x| crate::keys::truthy(x)).unwrap_or_else(|| json!(env("DECISIONS_MODEL", ""))),
            "args": v.get("args").cloned().filter(|x| crate::keys::truthy(x)).unwrap_or_else(|| json!(env("DECISIONS_ARGS", ""))),
            "enabled": v.get("enabled").cloned().unwrap_or(json!(false)),
            "status": v.get("status").cloned().unwrap_or(json!("unknown")),
            "port": v.get("port").cloned().unwrap_or(json!(null)),
            "uptime_s": v.get("uptime_s").cloned().unwrap_or(json!(null)),
            "libre_gib": v.get("libre_gib").cloned().unwrap_or(json!(null)),
            "max_state_bytes": MAX_STATE_BYTES,
            "max_questions": MAX_QUESTIONS,
            "service": "runner",
        }),
        None => json!({
            "model": name,
            "enabled": false,
            "status": "unreachable",
            "port": null,
            "uptime_s": null,
            "libre_gib": crate::asr_ondemand::mem_available_gb(),
            "max_state_bytes": MAX_STATE_BYTES,
            "max_questions": MAX_QUESTIONS,
            "service": "runner",
        }),
    }
}

/// The decision model's public name when its engine actually serves.
///
/// The home page lists it as a capability, and a card that names a stopped
/// service reads as « it works » — so a stopped engine returns None. The runner
/// answers in milliseconds; the timeout is only a guard for a runner that hangs.
pub async fn running_name() -> Option<String> {
    let (code, v) = runner("/decisions/status", None).await?;
    if code != 200 || v.get("status").and_then(Value::as_str) != Some("running") {
        return None;
    }
    v.get("name").and_then(Value::as_str).map(str::to_string)
}

/// Start/stop the decision engine. The runner answers the memory refusals; the
/// portal only relays them (never hides a refusal behind « démarré »).
pub async fn start() -> (u16, Value) {
    match runner("/decisions/start", Some(&json!({}))).await {
        // The runner's own status is the truth (200, 400 not configured,
        // 409 memory floor): the page shows its sentence as it is.
        Some((code, v)) => (code, v),
        None => (503, json!({"error": "Le runner ne répond pas : l'état du service de décision est inconnu."})),
    }
}

pub async fn stop() -> (u16, Value) {
    match runner("/decisions/stop", Some(&json!({}))).await {
        Some((code, v)) => (code, v),
        None => (503, json!({"error": "Le runner ne répond pas : l'état du service de décision est inconnu."})),
    }
}

/// One `noul` question's `criteria`: an object describing true and false.
fn check_noul(criteria: &Value, id: &str) -> Result<Value, String> {
    let Some(obj) = criteria.as_object() else {
        return Err(format!("La question « {id} » (noul) : « criteria » doit être un objet ({{\"true\": …, \"false\": …}})."));
    };
    for (k, v) in obj {
        if k != "true" && k != "false" {
            return Err(format!("La question « {id} » (noul) : clé « {k} » inconnue (true ou false)."));
        }
        if !(v.is_null() || v.is_string()) {
            return Err(format!("La question « {id} » (noul) : la description de « {k} » doit être un texte ou null."));
        }
    }
    Ok(criteria.clone())
}

/// The System One body, validated and narrowed to the fields the API defines.
///
/// Returning a new object (instead of passing the caller's through) is what
/// keeps an unknown field from reaching the engine and being silently ignored:
/// a client that sends `temperature` deserves to know it means nothing here.
pub fn validate(body: &Value) -> Result<Value, String> {
    let obj = body.as_object().ok_or("Corps JSON attendu (objet).")?;
    for key in obj.keys() {
        if !matches!(key.as_str(), "state" | "questions" | "files" | "images") {
            return Err(format!("Champ inconnu « {key} » (state, questions, files, images)."));
        }
    }
    let state = obj.get("state").ok_or("Le champ « state » est requis (texte ou JSON).")?;
    let state_text = serde_json::to_string(state).unwrap_or_default();
    if state_text.len() > MAX_STATE_BYTES {
        return Err(format!("« state » dépasse {} octets.", MAX_STATE_BYTES));
    }
    if state.is_null() {
        return Err("« state » ne peut pas être null pour ce modèle (texte ou JSON attendu).".into());
    }
    let questions = obj.get("questions").and_then(Value::as_object).ok_or("« questions » doit être un objet (id → question).")?;
    if questions.is_empty() {
        return Err("Au moins une question est requise.".into());
    }
    if questions.len() > MAX_QUESTIONS {
        return Err(format!("{} questions au maximum.", MAX_QUESTIONS));
    }
    let mut out: Map<String, Value> = Map::new();
    for (id, q) in questions {
        if id.is_empty() || id.chars().count() > 64 {
            return Err("Identifiant de question invalide (1 à 64 caractères).".into());
        }
        let qo = q.as_object().ok_or_else(|| format!("La question « {id} » doit être un objet."))?;
        let kind = qo.get("type").and_then(Value::as_str).unwrap_or("");
        let instructions = qo.get("instructions").ok_or_else(|| format!("La question « {id} » n'a pas d'« instructions »."))?;
        if !(instructions.is_string() || instructions.is_object() || instructions.is_array()) {
            return Err(format!("La question « {id} » : « instructions » doit être un texte, un objet ou une liste."));
        }
        let mut entry = Map::new();
        entry.insert("instructions".into(), instructions.clone());
        match kind {
            "noul" => {
                entry.insert("type".into(), json!("noul"));
                if let Some(c) = qo.get("criteria") {
                    entry.insert("criteria".into(), check_noul(c, id)?);
                }
            }
            "choice" => {
                let c = qo.get("criteria").and_then(Value::as_object).ok_or_else(|| {
                    format!("La question « {id} » (choice) exige des « criteria » : {{\"valeur\": description|null}}.")
                })?;
                if c.len() < 2 || c.len() > MAX_CHOICES {
                    return Err(format!("La question « {id} » doit offrir 2 à {MAX_CHOICES} choix."));
                }
                for (value, description) in c {
                    if !(description.is_null() || description.is_string()) {
                        return Err(format!("La question « {id} » : la description du choix « {value} » doit être un texte ou null."));
                    }
                }
                entry.insert("type".into(), json!("choice"));
                entry.insert("criteria".into(), Value::Object(c.clone()));
            }
            "score" => {
                let c = qo.get("criteria").and_then(Value::as_array).ok_or_else(|| {
                    format!("La question « {id} » (score) exige des « criteria » : une liste de niveaux, du plus bas au plus haut.")
                })?;
                if c.len() < 2 || c.len() > MAX_LEVELS {
                    return Err(format!("La question « {id} » doit avoir 2 à {MAX_LEVELS} niveaux."));
                }
                if !c.iter().all(Value::is_string) {
                    return Err(format!("La question « {id} » : chaque niveau doit être un texte."));
                }
                entry.insert("type".into(), json!("score"));
                entry.insert("criteria".into(), Value::Array(c.clone()));
            }
            "" => return Err(format!("La question « {id} » n'a pas de « type » (noul, choice ou score).")),
            other => return Err(format!("La question « {id} » : type « {other} » inconnu (noul, choice ou score).")),
        }
        out.insert(id.clone(), Value::Object(entry));
    }
    let mut payload = json!({"state": state, "questions": out});
    // Images are an extension: relayed only when the caller sends them, and the
    // engine decides (the page does not offer them in this version).
    for key in ["files", "images"] {
        if let Some(v) = obj.get(key) {
            if !v.is_array() {
                return Err(format!("« {key} » doit être une liste."));
            }
            payload[key] = v.clone();
        }
    }
    Ok(payload)
}

/// The user's most recent API key: the portal calls LiteLLM with it so the
/// decision call is billed, budgeted and logged like a chat call.
async fn user_key(pool: &sqlx::SqlitePool, username: &str) -> Option<String> {
    sqlx::query_scalar("SELECT key_value FROM api_keys WHERE username = ? ORDER BY created_at DESC LIMIT 1")
        .bind(username)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
}

/// `POST /api/decisions/run`: validate, then ask LiteLLM. Every failure is a
/// sentence the page can show — never a bare status code.
pub async fn run(pool: &sqlx::SqlitePool, username: &str, body: &Value) -> Result<Value, (u16, String)> {
    let payload = validate(body).map_err(|e| (400, e))?;
    let Some(key) = user_key(pool, username).await else {
        return Err((400, "Aucune clé API — crée une clé dans Réglages ▸ Clés API.".into()));
    };
    // Images do NOT go through the gateway: LiteLLM 1.104.3 refuses an
    // `input_image` for every System One provider (« System One providers accept
    // text input only », measured), while llama.cpp's /v1/systemone takes `files`
    // — the model itself is multimodal, verified on this box. So an image request
    // goes to the engine directly. The cost is the billing: these calls are not
    // in LiteLLM's spend logs, they stay bounded by the page's rate limit.
    let has_files = payload
        .get("files")
        .or_else(|| payload.get("images"))
        .and_then(Value::as_array)
        .is_some_and(|f| !f.is_empty());
    let (url, request) = if has_files {
        let base = env("DECISIONS_API_BASE", "http://172.19.0.1:8002/v1");
        let mut body = payload.as_object().cloned().unwrap_or_default();
        body.insert("model".into(), json!(model_name()));
        (format!("{}/systemone", base.trim_end_matches('/')), body)
    } else {
        let mut body = payload.as_object().cloned().unwrap_or_default();
        body.insert("model".into(), json!(model_name()));
        (format!("{}/v1/systemone", crate::probes::litellm_url().trim_end_matches('/')), body)
    };
    let sent = if has_files {
        crate::sidecars::http().post(url).json(&Value::Object(request)).timeout(Duration::from_secs(120)).send().await
    } else {
        crate::sidecars::http().post(url).bearer_auth(&key).json(&Value::Object(request)).timeout(Duration::from_secs(120)).send().await
    };
    let r = match sent {
        Ok(r) => r,
        Err(e) => {
            // The internal address stays in the logs.
            eprintln!("[decisions] LiteLLM call failed: {e}");
            return Err((503, "La passerelle n'a pas répondu.".into()));
        }
    };
    let status = r.status().as_u16();
    let data: Value = r.json().await.unwrap_or_else(|_| json!({}));
    if !(200..300).contains(&status) {
        let detail = data
            .pointer("/error/message")
            .or_else(|| data.get("error"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let message = match status {
            429 => "Budget épuisé pour ta clé, ou trop de requêtes.".to_string(),
            401 | 403 => "Ta clé API a été refusée (révoquée ?) : crée une nouvelle clé dans Réglages ▸ Clés API.".to_string(),
            400 | 422 => {
                if detail.is_empty() {
                    "Requête refusée par la passerelle.".to_string()
                } else {
                    format!("Requête refusée : {detail}")
                }
            }
            503 => "Le service de décision n'est pas démarré.".to_string(),
            _ => format!("La passerelle a répondu {status}."),
        };
        // Never 401: the browser answers a 401 by logging the user out, and a
        // refused key is not an expired session.
        return Err((if status == 401 || status == 403 { 400 } else { status.max(400) }, message));
    }
    if data.get("answers").is_none() {
        return Err((502, "Réponse sans résultat : le modèle n'a renvoyé aucune réponse.".into()));
    }
    Ok(json!({
        "ok": true,
        "model": data.get("model").and_then(Value::as_str).unwrap_or(&model_name()),
        "answers": data.get("answers").cloned().unwrap_or(json!({})),
        "usage": data.get("usage").cloned().unwrap_or(json!(null)),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(kind: &str) -> Value {
        match kind {
            "noul" => json!({"type": "noul", "instructions": "Is it urgent?"}),
            "choice" => json!({"type": "choice", "instructions": "Which team?", "criteria": {"billing": null, "shipping": "parcels"}}),
            "score" => json!({"type": "score", "instructions": "How urgent?", "criteria": ["can wait", "this week", "today"]}),
            _ => unreachable!(),
        }
    }

    #[test]
    fn accepts_the_three_question_types() {
        let body = json!({"state": "Customer: charged twice.", "questions": {"urgent": q("noul"), "team": q("choice"), "urgency": q("score")}});
        let out = validate(&body).unwrap();
        assert_eq!(out["questions"]["urgent"]["type"], "noul");
        assert_eq!(out["questions"]["team"]["type"], "choice");
        assert_eq!(out["questions"]["urgency"]["criteria"].as_array().unwrap().len(), 3);
        assert_eq!(out["state"], "Customer: charged twice.");
    }

    #[test]
    fn accepts_a_json_state() {
        let body = json!({"state": {"order": 4411, "duplicate": true}, "questions": {"x": q("noul")}});
        assert!(validate(&body).is_ok());
    }

    #[test]
    fn refuses_an_unknown_type() {
        let body = json!({"state": "s", "questions": {"x": {"type": "predicate", "instructions": "?"}}});
        let err = validate(&body).unwrap_err();
        assert!(err.contains("predicate"), "{err}");
    }

    #[test]
    fn refuses_a_choice_with_one_option() {
        let body = json!({"state": "s", "questions": {"x": {"type": "choice", "instructions": "?", "criteria": {"a": null}}}});
        assert!(validate(&body).unwrap_err().contains("2 à"));
    }

    #[test]
    fn refuses_a_score_outside_two_to_ten_levels() {
        let one = json!({"state": "s", "questions": {"x": {"type": "score", "instructions": "?", "criteria": ["only"]}}});
        assert!(validate(&one).is_err());
        let eleven: Vec<String> = (0..11).map(|i| format!("l{i}")).collect();
        let many = json!({"state": "s", "questions": {"x": {"type": "score", "instructions": "?", "criteria": eleven}}});
        assert!(validate(&many).unwrap_err().contains("2 à 10"));
    }

    #[test]
    fn refuses_a_question_without_instructions() {
        let body = json!({"state": "s", "questions": {"x": {"type": "noul"}}});
        assert!(validate(&body).unwrap_err().contains("instructions"));
    }

    #[test]
    fn refuses_an_unknown_field_instead_of_dropping_it() {
        let body = json!({"state": "s", "questions": {"x": q("noul")}, "temperature": 0.2});
        assert!(validate(&body).unwrap_err().contains("temperature"));
    }

    #[test]
    fn refuses_an_oversized_state() {
        let body = json!({"state": "x".repeat(MAX_STATE_BYTES), "questions": {"x": q("noul")}});
        assert!(validate(&body).unwrap_err().contains("octets"));
    }

    #[test]
    fn refuses_too_many_questions() {
        let mut questions = Map::new();
        for i in 0..(MAX_QUESTIONS + 1) {
            questions.insert(format!("q{i}"), q("noul"));
        }
        let body = json!({"state": "s", "questions": questions});
        assert!(validate(&body).unwrap_err().contains("au maximum"));
    }

    #[test]
    fn refuses_a_null_state_and_a_non_object_body() {
        assert!(validate(&json!({"state": null, "questions": {"x": q("noul")}})).is_err());
        assert!(validate(&json!([1, 2])).is_err());
    }

    #[test]
    fn keeps_only_the_documented_fields() {
        let body = json!({"state": "s", "questions": {"x": q("noul")}, "files": ["data:image/png;base64,AA"]});
        let out = validate(&body).unwrap();
        assert_eq!(out["files"].as_array().unwrap().len(), 1);
        assert!(out.get("temperature").is_none());
    }
}
