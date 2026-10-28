//! Launching and stopping the served models through the runner
//! (sidecars.py): the chat model (its LiteLLM route and `auto-model` follow
//! an accepted launch; a background follow-up announces it once it serves,
//! or alerts when it never does), and the OCR, voice and image models.
use serde_json::{json, Value};
use sqlx::SqlitePool;
use std::time::Duration;

/// `_LAUNCH_TIMEOUT_PATTERN`: no answer is not a failure, the load may be under way.
pub const LAUNCH_TIMEOUT: &str = "Le runner n'a pas répondu dans le délai imparti (150 s) : le lancement est peut-être toujours en cours. Vérifie l'état du modèle AVANT de relancer — relancer tuerait un modèle en cours de chargement.";
/// `_STOP_TIMEOUT_PATTERN`.
pub const STOP_TIMEOUT: &str = "Le runner n'a pas répondu dans le délai imparti (45 s) : l'arrêt est peut-être toujours en cours. Vérifie l'état du modèle avant de réessayer.";

/// `_START_WINDOW_S` / `_FOLLOW_INTERVAL_S`.
const START_WINDOW_S: u64 = 900;
const FOLLOW_INTERVAL_S: u64 = 5;

fn token() -> String {
    std::env::var("RUNNER_TOKEN").unwrap_or_default()
}

/// The Python exception name a request failure would carry.
fn error_name(e: &reqwest::Error) -> &'static str {
    if e.is_timeout() {
        "ReadTimeout"
    } else if e.is_connect() {
        "ConnectionError"
    } else {
        "RequestException"
    }
}

/// The runner's POST, for the modules that need it (`admin_catalog`'s model
/// download). Kept next to the private one so there is ONE place that knows how
/// the runner is addressed and authenticated.
pub(crate) async fn runner_post_public(path: &str, body: Option<&Value>, secs: u64) -> Result<reqwest::Response, reqwest::Error> {
    runner_post(path, body, secs).await
}

/// The runner's GET, same reason.
pub(crate) async fn runner_get_public(path: &str, secs: u64) -> Result<Value, reqwest::Error> {
    let url = format!("{}{path}", crate::probes::runner_url());
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let r = reqwest::Client::builder()
        .timeout(Duration::from_secs(secs))
        .build()?
        .get(url)
        .bearer_auth(token)
        .send()
        .await?;
    r.json::<Value>().await
}

async fn runner_post(path: &str, body: Option<&Value>, secs: u64) -> Result<reqwest::Response, reqwest::Error> {
    let mut req = crate::sidecars::http().post(format!("{}{path}", crate::probes::runner_url())).bearer_auth(token()).timeout(Duration::from_secs(secs));
    if let Some(b) = body {
        req = req.json(b);
    }
    req.send().await
}

/// `refusal_reason(r)`: the runner's `detail`, else nothing.
async fn refusal_reason(r: reqwest::Response) -> String {
    r.json::<Value>().await.ok().and_then(|v| v.get("detail").filter(|d| crate::keys::truthy(d)).and_then(Value::as_str).map(str::to_string)).unwrap_or_default()
}

/// `runner_launch(...)`: (ok, reason, uncertain). A refusal carries the
/// runner's reason; a timeout is a doubt, never retried (relaunching a model
/// being loaded kills it); only a connection failure is retried, once.
pub async fn launch_chat(pool: &SqlitePool, hf_model_id: &str, name: &str, args: &str, engine: &str) -> (bool, String, bool) {
    let engine = if engine.is_empty() { "vllm" } else { engine };
    let body = json!({"hf_model_id": hf_model_id, "model_name": name, "vllm_args": args, "engine": engine});
    let mut sent = runner_post("/launch", Some(&body), 150).await;
    if let Err(e) = &sent {
        if e.is_connect() {
            tokio::time::sleep(Duration::from_secs(1)).await;
            sent = runner_post("/launch", Some(&body), 150).await;
            if let Err(e) = &sent {
                return if e.is_timeout() { (false, LAUNCH_TIMEOUT.into(), true) } else { (false, "runner injoignable".into(), false) };
            }
        }
    }
    let r = match sent {
        Ok(r) => r,
        Err(e) if e.is_timeout() => return (false, LAUNCH_TIMEOUT.into(), true),
        Err(e) => return (false, format!("runner injoignable ({})", error_name(&e)), false),
    };
    if r.status().is_success() {
        crate::litellm_models::register(name, args, engine).await;
        crate::litellm_models::point_auto_model(name, args, engine).await;
        follow_launch(pool.clone(), name.to_string());
        return (true, String::new(), false);
    }
    let status = r.status().as_u16();
    let text = r.text().await.unwrap_or_default();
    let reason = match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(o)) => o.get("error").filter(|v| crate::keys::truthy(v)).map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())),
        _ => None,
    }
    .unwrap_or_else(|| text.chars().take(200).collect());
    (false, format!("{reason} (HTTP {status})"), false)
}

/// `runner_stop()`: (ok, reason, uncertain).
pub async fn stop_chat() -> (bool, String, bool) {
    match runner_post("/stop", None, 45).await {
        Ok(r) if r.status().is_success() => (true, String::new(), false),
        Ok(r) => (false, format!("le runner a refusé l'arrêt (HTTP {})", r.status().as_u16()), false),
        Err(e) if e.is_timeout() => (false, STOP_TIMEOUT.into(), true),
        Err(e) => (false, format!("runner injoignable ({})", error_name(&e)), false),
    }
}

/// `_announce_launch(name)`: « X replaces Y », unless X was the last announced.
async fn announce_launch(pool: &SqlitePool, name: &str) {
    let prev: Option<String> = sqlx::query_scalar("SELECT a FROM announcements WHERE kind='model_launch' ORDER BY id DESC LIMIT 1").fetch_optional(pool).await.ok().flatten();
    let prev = prev.unwrap_or_default();
    if prev == name {
        return;
    }
    crate::handlers::add_announcement_pub(pool, "model_launch", name, &prev).await;
}

/// `_follow_launch(name)`: accepting is not serving. In the background, the
/// model is announced once the runner says it serves, or an alert goes out
/// when it never does within the window (another launch ends the watch).
fn follow_launch(pool: SqlitePool, name: String) {
    if std::env::var("DGX_NO_REAPER").as_deref() == Ok("1") {
        return;
    }
    tokio::spawn(async move {
        let start = std::time::Instant::now();
        while start.elapsed() < Duration::from_secs(START_WINDOW_S) {
            tokio::time::sleep(Duration::from_secs(FOLLOW_INTERVAL_S)).await;
            let st = crate::system::runner_status().await;
            let model = st.get("model").and_then(Value::as_str).unwrap_or("");
            let status = st.get("status").and_then(Value::as_str).unwrap_or("");
            if !model.is_empty() && model != name && (status == "running" || status == "starting") {
                return;
            }
            if model == name && status == "running" {
                announce_launch(&pool, &name).await;
                crate::sessions::log_audit(&pool, "system", "model.served", &format!("{name} chargé et en service")).await;
                // json.dumps's default layout, as Flask writes it.
                let since = chrono::Utc::now().timestamp();
                let served = format!("{{\"name\": {}, \"since\": {since}}}", serde_json::to_string(&name).unwrap_or_default());
                let _ = sqlx::query("INSERT INTO settings (key, value) VALUES ('served_model', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
                    .bind(served)
                    .execute(&pool)
                    .await;
                return;
            }
            if model == name && status == "error" {
                break;
            }
        }
        crate::sessions::log_audit(&pool, "system", "model.failed_after_launch", &format!("{name} accepté par le runner mais jamais en service dans les {START_WINDOW_S} s")).await;
        crate::mailer::infra_alert(
            &pool,
            "Chat model never came up",
            &format!("{name}: le runner a accepté le lancement mais le modèle n'est pas en service après {START_WINDOW_S} s. La plateforme n'a probablement AUCUN modèle servi — vérifie les logs du runner."),
        )
        .await;
    });
}

/// `_ocr_launch` / `_voice_launch` / `_image_launch`: recreate a media
/// container with another model; (ok, the runner's reason).
pub async fn launch_media(kind: &str, body: Value, secs: u64) -> (bool, String) {
    match runner_post(&format!("/{kind}/launch"), Some(&body), secs).await {
        Ok(r) => {
            let ok = r.status().is_success();
            (ok, refusal_reason(r).await)
        }
        Err(e) => (false, e.to_string()),
    }
}

/// `_sidecar_action(kind, action)`: (ok, detail). The detail carries the
/// runner's reason, so an admin sees more than « Échec du démarrage » — the
/// same text Flask gives: ` (reason)`, ` (HTTP 503)` or ` (ExceptionName)`.
pub async fn sidecar_action(kind: &str, action: &str) -> (bool, String) {
    match runner_post(&format!("/{kind}/{action}"), None, 30).await {
        Ok(r) if r.status().is_success() => (true, String::new()),
        Ok(r) => {
            let status = r.status().as_u16();
            let reason = r.json::<Value>().await.ok().and_then(|v| v.get("detail").or(v.get("error")).filter(|d| crate::keys::truthy(d)).and_then(Value::as_str).map(str::to_string)).unwrap_or_default();
            (false, if reason.is_empty() { format!(" (HTTP {status})") } else { format!(" ({})", reason.trim()) })
        }
        Err(e) => (false, format!(" ({})", if e.is_timeout() { "Timeout" } else if e.is_connect() { "ConnectionError" } else { "RequestException" })),
    }
}
