//! Hugging Face model search, parity with dgx-portal/vllm_health.py
//! (`search_hf_models_page`, `_hf_page`, `hf_model_outside_gb10`).
//!
//! The contract matters more than the data: an outage answers as an outage
//! (503, `hf_unavailable`), never as an empty list that reads « the model does
//! not exist ». An unknown task is the caller's error (400), not an outage.
use std::time::Duration;

use serde_json::{json, Value};

pub const GB10_TAG: &str = "gb10";
pub const PAGE_SIZE: u32 = 48;
pub const TASKS: [&str; 7] = [
    "text-generation",
    "text2text-generation",
    "conversational",
    "feature-extraction",
    "text-to-image",
    "text-to-video",
    "image-to-text",
];
const TOKEN_FILE: &str = "/run/secrets/hf_token";

#[derive(Debug)]
pub enum HfError {
    /// Unreachable, an HTTP refusal, or an unreadable answer; the message is French.
    Unavailable(String),
}

/// The HF token, or "" — from HF_TOKEN / HUGGING_FACE_HUB_TOKEN, else the secret file.
/// Never returned by a route, never logged: only its presence is.
pub fn token() -> String {
    let from_env = std::env::var("HF_TOKEN")
        .or_else(|_| std::env::var("HUGGING_FACE_HUB_TOKEN"))
        .unwrap_or_default();
    let raw = if from_env.trim().is_empty() {
        std::fs::read_to_string(TOKEN_FILE).unwrap_or_default()
    } else {
        from_env
    };
    raw.trim().to_string()
}

/// Is there a token? The value never leaves the server.
pub fn token_present() -> bool {
    !token().is_empty()
}

/// Query parameters of one search. Pure: tested below.
pub fn search_params(query: &str, task: Option<&str>, gb10_only: bool, skip: u32) -> Vec<(&'static str, String)> {
    let mut params = vec![
        ("search", query.to_string()),
        ("limit", PAGE_SIZE.to_string()),
        ("skip", skip.to_string()),
        ("sort", "downloads".to_string()),
        ("direction", "-1".to_string()),
        ("full", "true".to_string()),
    ];
    // Several `filter` keys mean AND on the HF side: an empty one is omitted.
    if let Some(t) = task.filter(|t| !t.is_empty()) {
        params.push(("filter", t.to_string()));
    }
    if gb10_only {
        params.push(("filter", GB10_TAG.to_string()));
    }
    params
}

/// vllm_health.guess_engine: GGUF → llama.cpp, anything else → vLLM. Pure.
pub fn guess_engine(model: &Value) -> &'static str {
    let gguf = model["tags"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .any(|t| t.eq_ignore_ascii_case("gguf"));
    if gguf { "llamacpp" } else { "vllm" }
}

/// One HF call → (models, has more pages). Raises instead of returning empty.
async fn page(params: &[(&str, String)]) -> Result<(Vec<Value>, bool), HfError> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(8))
        .build()
        .map_err(|e| HfError::Unavailable(format!("HF injoignable ({})", e.without_url())))?;
    let mut req = client.get("https://huggingface.co/api/models").query(params);
    let t = token();
    if !t.is_empty() {
        req = req.bearer_auth(t);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| HfError::Unavailable(format!("HF injoignable ({})", if e.is_timeout() { "TimeoutError" } else { "ConnectionError" })))?;
    let status = resp.status();
    if !status.is_success() {
        // A refused token is not an outage: saying so tells the user to replace it.
        if matches!(status.as_u16(), 401 | 403) && token_present() {
            return Err(HfError::Unavailable("jeton Hugging Face refusé".into()));
        }
        return Err(HfError::Unavailable(format!("HF a répondu {}", status.as_u16())));
    }
    // `has_more` comes from HF's Link header, authoritative.
    let following = resp
        .headers()
        .get("link")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.contains("rel=\"next\""));
    let data: Value = resp
        .json()
        .await
        .map_err(|_| HfError::Unavailable("réponse HF illisible".into()))?;
    let models = data.as_array().cloned().ok_or_else(|| HfError::Unavailable("réponse HF illisible".into()))?;
    Ok((models, following))
}

/// search_hf_models_page: the models of one page (engine guessed, the file list
/// and the internal id dropped), and whether there is a next page.
pub async fn search_page(query: &str, task: Option<&str>, gb10_only: bool, skip: u32) -> Result<(Vec<Value>, bool), HfError> {
    let (mut models, more) = page(&search_params(query, task, gb10_only, skip)).await?;
    for m in models.iter_mut() {
        let engine = guess_engine(m);
        if let Some(obj) = m.as_object_mut() {
            obj.insert("engine".into(), json!(engine));
            // `siblings` weighed 58 % of the response and no screen reads it.
            obj.remove("siblings");
            obj.remove("_id");
        }
    }
    Ok((models, more))
}

/// hf_model_outside_gb10: do models exist WITHOUT the GB10 filter? True, False,
/// or None when unknown (a doubt is not a « no »).
pub async fn outside_gb10(query: &str, task: Option<&str>) -> Option<bool> {
    if query.is_empty() {
        return None;
    }
    let mut params = vec![("search", query.to_string()), ("limit", "1".to_string())];
    if let Some(t) = task.filter(|t| !t.is_empty()) {
        params.push(("filter", t.to_string()));
    }
    page(&params).await.ok().map(|(models, _)| !models.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn params_omit_empty_task_and_add_gb10_last() {
        let p = search_params("qwen", None, false, 0);
        assert!(!p.iter().any(|(k, _)| *k == "filter"));
        let p = search_params("qwen", Some("text-generation"), true, 48);
        let filters: Vec<&str> = p.iter().filter(|(k, _)| *k == "filter").map(|(_, v)| v.as_str()).collect();
        assert_eq!(filters, vec!["text-generation", "gb10"]);
        assert!(p.contains(&("skip", "48".to_string())));
        assert!(p.contains(&("full", "true".to_string())));
    }

    #[test]
    fn engine_follows_the_gguf_tag() {
        assert_eq!(guess_engine(&json!({"tags": ["GGUF", "x"]})), "llamacpp");
        assert_eq!(guess_engine(&json!({"tags": ["safetensors"]})), "vllm");
        assert_eq!(guess_engine(&json!({})), "vllm");
    }

    #[test]
    fn tasks_are_the_allow_list() {
        assert!(TASKS.contains(&"text-generation"));
        assert!(!TASKS.contains(&"something-else"));
    }
}
