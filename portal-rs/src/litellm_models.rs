//! A catalog model's route in LiteLLM (litellm_client.py): created or
//! refreshed when the admin adds or edits it, removed with it. The context
//! advertised to clients comes from the engine args (health::ctx_split).
use serde_json::{json, Value};
use std::time::Duration;

fn base() -> String {
    crate::probes::litellm_url().trim_end_matches('/').to_string()
}

fn key() -> String {
    std::env::var("LITELLM_MASTER_KEY").unwrap_or_default()
}

/// `config.VLLM_API_BASE`: where LiteLLM reaches the served engine.
fn engine_base() -> String {
    std::env::var("VLLM_API_BASE").unwrap_or_else(|_| "http://host.docker.internal:8000/v1".into())
}

async fn post(path: &str, body: &Value, secs: u64) -> Option<reqwest::Response> {
    crate::sidecars::http().post(format!("{}{path}", base())).bearer_auth(key()).json(body).timeout(Duration::from_secs(secs)).send().await.ok()
}

/// `_litellm_model_entry(name)`: (id, full entry) of the route of that name.
async fn entry(name: &str) -> (Option<String>, Option<Value>) {
    let got = async {
        let r = crate::sidecars::http().get(format!("{}/model/info", base())).bearer_auth(key()).timeout(Duration::from_secs(5)).send().await.ok()?;
        r.json::<Value>().await.ok()
    }
    .await;
    for m in got.as_ref().and_then(|v| v.get("data")).and_then(Value::as_array).into_iter().flatten() {
        if m.get("model_name").and_then(Value::as_str) == Some(name) {
            let id = m.get("model_info").and_then(|i| i.get("id")).and_then(|v| v.as_str().map(str::to_string).or_else(|| (!v.is_null()).then(|| v.to_string())));
            return (id, Some(m.clone()));
        }
    }
    (None, None)
}

/// `_restore_model(entry)`: put a route back as it was (best effort).
async fn restore(entry: &Value) -> bool {
    let mut info = entry.get("model_info").and_then(Value::as_object).cloned().unwrap_or_default();
    info.remove("id");
    info.remove("db_model");
    let body = json!({
        "model_name": entry.get("model_name"),
        "litellm_params": entry.get("litellm_params").filter(|v| crate::keys::truthy(v)).cloned().unwrap_or_else(|| json!({})),
        "model_info": info,
    });
    post("/model/new", &body, 8).await.is_some_and(|r| r.status().as_u16() < 300)
}

/// `_model_upstream(name, engine)`: ds4 is asked for its non-thinking mode.
fn upstream(name: &str, engine: &str) -> String {
    if engine == "ds4" { "deepseek-chat".into() } else { name.to_string() }
}

/// The engine's own token, when it needs one. Edge0 (edge0-strata) refuses to
/// listen beyond loopback without `--api-key-file`, so its route cannot use the
/// placeholder the other engines accept.
fn engine_api_key(engine: &str) -> String {
    if engine != "edge0" {
        return "dummy".into();
    }
    let path = std::env::var("EDGE0_KEY_FILE").unwrap_or_else(|_| "/etc/edge0/api-key".into());
    std::fs::read_to_string(path).map(|s| s.trim().to_string()).unwrap_or_else(|_| "dummy".into())
}

/// `_litellm_upsert`: (re)create the route `public_name` → `upstream` on the
/// engine; the previous entry is put back when the creation is refused.
async fn upsert(public_name: &str, upstream: &str, max_input: i64, max_output: i64, api_key: &str, kwargs: serde_json::Value) -> bool {
    if key().is_empty() {
        return false;
    }
    let body = json!({
        "model_name": public_name,
        "litellm_params": {
            "model": format!("openai/{upstream}"),
            "api_base": engine_base(),
            "api_key": api_key,
            "input_cost_per_token": 1,
            "output_cost_per_token": 1,
            // Thinking models: reasoning off by default (a request that asks wins).
            "chat_template_kwargs": kwargs,
            // The same, for the Anthropic endpoint, which only forwards extra_body.
            "extra_body": {"chat_template_kwargs": kwargs},
        },
        "model_info": {
            "mode": "chat",
            "supports_function_calling": true,
            "max_input_tokens": max_input,
            "max_output_tokens": max_output,
        },
    });
    let (existing, before) = entry(public_name).await;
    if let Some(id) = &existing {
        let _ = post("/model/delete", &json!({"id": id}), 5).await;
    }
    let Some(r) = post("/model/new", &body, 8).await else { return false };
    if r.status().as_u16() < 300 {
        return true;
    }
    if let (Some(_), Some(prev)) = (existing, before) {
        let restored = restore(&prev).await;
        eprintln!(
            "LiteLLM: creation of « {public_name} » refused (HTTP {}) — previous entry {}",
            r.status().as_u16(),
            if restored { "restored" } else { "LOST" }
        );
    }
    false
}

/// Is the model name known to LiteLLM right now? A launch outside the portal
/// (or a registration that failed) leaves the chat answering 400 « Invalid model
/// name » while the model is serving (2026-11-23).
pub async fn registered(name: &str) -> bool {
    let Ok(r) = crate::sidecars::http()
        .get(format!("{}/v1/models", base()))
        .bearer_auth(key())
        .timeout(std::time::Duration::from_secs(10))
        .send()
        .await
    else {
        return false;
    };
    let Ok(v) = r.json::<serde_json::Value>().await else { return false };
    v.get("data")
        .and_then(serde_json::Value::as_array)
        .is_some_and(|a| a.iter().any(|m| m.get("id").and_then(serde_json::Value::as_str) == Some(name)))
}

/// Register the model if LiteLLM does not know it yet, and make sure the chat
/// alias still designates it. Called once the engine reports the model running,
/// so "a new model answers in the chat and through the API" holds even when the
/// registration at launch time was lost.
pub async fn ensure_served(name: &str, args: &str, engine: &str) -> bool {
    if !registered(name).await && !register(name, args, engine).await {
        return false;
    }
    point_auto_model(name, args, engine).await
}

/// The `--chat-template-kwargs` a model was launched with, if any. LiteLLM must
/// forward the SAME kwargs, otherwise the API and the playground behave
/// differently for one model (a thinking template that needs `reasoning_effort`
/// ignores `enable_thinking`, and every API client gets an empty answer at a
/// small max_tokens — 2026-11-23).
fn template_kwargs(args: &str) -> Option<serde_json::Value> {
    let tokens: Vec<&str> = args.split_whitespace().collect();
    let i = tokens.iter().position(|t| *t == "--chat-template-kwargs")?;
    let raw = tokens.get(i + 1)?;
    serde_json::from_str(raw).ok()
}

/// `_register_litellm_model(name, vllm_args, engine)`.
pub async fn register(name: &str, args: &str, engine: &str) -> bool {
    let (max_input, max_output) = crate::health::ctx_split(args, engine);
    // Whatever the engine runs with, the API sends: a template kwarg the launch
    // set must not be lost on the proxy path.
    let kwargs = template_kwargs(args).unwrap_or_else(|| json!({"enable_thinking": false}));
    upsert(name, &upstream(name, engine), max_input, max_output, &engine_api_key(engine), kwargs).await
}

/// Where LiteLLM reaches the decision engine: its own port, never :8000.
fn decisions_base() -> String {
    std::env::var("DECISIONS_API_BASE").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "http://172.19.0.1:8002/v1".into())
}

/// `_register_decision_model`: the route LiteLLM serves on `/v1/systemone`.
///
/// `mode: "decisions"` is what makes the proxy answer it on the decision routes
/// instead of the chat ones; the provider is `hosted_vllm`, the only
/// self-hosted decision provider LiteLLM has, and its transport IS the System
/// One API (`POST {api_base}/v1/systemone`) that llama.cpp implements for Clef.
/// Costs are stamped at 1 like the chat models: the platform bills tokens, not
/// a vendor price. No `chat_template_kwargs` here — there is no chat template.
pub async fn register_decisions(name: &str) -> bool {
    if key().is_empty() {
        return false;
    }
    let body = json!({
        "model_name": name,
        "litellm_params": {
            "model": format!("hosted_vllm/{name}"),
            "api_base": decisions_base(),
            "api_key": "dummy",
            "input_cost_per_token": 1,
            "output_cost_per_token": 1,
        },
        "model_info": {"mode": "decisions"},
    });
    let (existing, before) = entry(name).await;
    if let Some(id) = &existing {
        let _ = post("/model/delete", &json!({"id": id}), 5).await;
    }
    let Some(r) = post("/model/new", &body, 8).await else { return false };
    if r.status().as_u16() < 300 {
        return true;
    }
    if let (Some(_), Some(prev)) = (existing, before) {
        let _ = restore(&prev).await;
    }
    false
}

/// `_unregister_litellm_model(name)`: true when no route of that name is left.
pub async fn unregister(name: &str) -> bool {
    if key().is_empty() {
        return false;
    }
    let (Some(id), _) = entry(name).await else { return true };
    post("/model/delete", &json!({"id": id}), 5).await.is_some_and(|r| r.status().as_u16() < 300)
}

/// `_point_auto_model(name, vllm_args, engine)`: the virtual `auto-model`
/// follows the chat model just launched.
/// Whether `name` is the decision model's public name.
///
/// One place for the rule: the alias points at a chat engine, the decision model
/// answers on its own route, and the two must not be confused by a launch.
pub fn is_decision_model(name: &str) -> bool {
    name == crate::decisions::model_name()
}

pub async fn point_auto_model(name: &str, args: &str, engine: &str) -> bool {
    // `auto-model` is the CHAT alias: it must never designate the decision
    // model. A decision engine has no chat endpoint (measured: the gateway
    // answers 501 and points at /v1/systemone), so pointing the alias at it
    // would break every client that wired `auto-model` once. The decision model
    // has its own name, registered on its own route.
    if crate::litellm_models::is_decision_model(name) {
        return false;
    }
    let (max_input, max_output) = crate::health::ctx_split(args, engine);
    let auto = std::env::var("AUTO_MODEL_NAME").unwrap_or_else(|_| "auto-model".into());
    let kwargs = template_kwargs(args).unwrap_or_else(|| json!({"enable_thinking": false}));
    upsert(&auto, &upstream(name, engine), max_input, max_output, &engine_api_key(engine), kwargs).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_auto_model_alias_never_designates_the_decision_model() {
        // Both directions matter: the decision model is refused, and an ordinary
        // chat model is not (a guard that refused everything would look green).
        assert!(is_decision_model(&crate::decisions::model_name()));
        assert!(!is_decision_model("qwen38-27b"));
        assert!(!is_decision_model(""));
    }
}
