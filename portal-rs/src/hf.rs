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
    // Shared client (building one per search costs more than the round-trip),
    // with the same 8 s budget as before applied per request.
    let client = crate::sidecars::http();
    let mut req = client.get("https://huggingface.co/api/models").query(params).timeout(Duration::from_secs(8));
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
/// A search repeated inside this window is answered from memory: the catalog
/// page re-renders on every keystroke pause, and each miss costs a ~200 ms
/// round-trip to Hugging Face plus a per-call client build before 2026-11-16.
const SEARCH_TTL: Duration = Duration::from_secs(60);
static SEARCH_CACHE: std::sync::Mutex<Option<std::collections::HashMap<String, (std::time::Instant, (Vec<Value>, bool))>>> =
    std::sync::Mutex::new(None);

pub async fn search_page(query: &str, task: Option<&str>, gb10_only: bool, skip: u32) -> Result<(Vec<Value>, bool), HfError> {
    let key = format!("{query}|{}|{gb10_only}|{skip}", task.unwrap_or(""));
    {
        let guard = SEARCH_CACHE.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(map) = guard.as_ref() {
            if let Some((at, hit)) = map.get(&key) {
                if at.elapsed() < SEARCH_TTL {
                    return Ok(hit.clone());
                }
            }
        }
    }
    let out = search_page_uncached(query, task, gb10_only, skip).await?;
    {
        let mut guard = SEARCH_CACHE.lock().unwrap_or_else(|e| e.into_inner());
        let map = guard.get_or_insert_with(std::collections::HashMap::new);
        map.retain(|_, (at, _)| at.elapsed() < SEARCH_TTL * 4);
        map.insert(key, (std::time::Instant::now(), out.clone()));
    }
    Ok(out)
}

async fn search_page_uncached(query: &str, task: Option<&str>, gb10_only: bool, skip: u32) -> Result<(Vec<Value>, bool), HfError> {
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

// ── The admin preset: everything a catalog entry needs, without guessing ──

/// The catalog name derived from a repository id: `Org/Qwen3-8B-GGUF` →
/// `qwen3-8b-gguf`. Lower-cased, anything that is not alphanumeric folded to a
/// single '-', bounded to 60 characters.
pub fn preset_name(repo: &str) -> String {
    let last = repo.rsplit('/').next().unwrap_or(repo);
    let mut out = String::new();
    for c in last.chars() {
        let c = c.to_ascii_lowercase();
        if c.is_ascii_alphanumeric() {
            out.push(c);
        } else if !out.ends_with('-') && !out.is_empty() {
            out.push('-');
        }
    }
    out.trim_matches('-').chars().take(60).collect()
}

pub fn rd_u32(b: &[u8], o: usize) -> Option<u32> {
    b.get(o..o + 4).map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]]))
}

pub fn rd_u64(b: &[u8], o: usize) -> Option<u64> {
    b.get(o..o + 8).map(|s| u64::from_le_bytes([s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7]]))
}

pub fn rd_str(b: &[u8], pos: &mut usize) -> Option<String> {
    let len = rd_u64(b, *pos)? as usize;
    *pos += 8;
    if len > 8192 {
        return None;
    }
    let s = String::from_utf8_lossy(b.get(*pos..*pos + len)?).to_string();
    *pos += len;
    Some(s)
}

/// The scalar size of a GGUF value type, or None for the variable ones.
fn gguf_scalar_size(t: u32) -> Option<usize> {
    match t {
        0 | 1 | 7 => Some(1),
        2 | 3 => Some(2),
        4 | 5 | 6 => Some(4),
        10 | 11 | 12 => Some(8),
        _ => None,
    }
}

/// The GGUF metadata the launch plan needs, read from the header only.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct GgufMeta {
    pub context: Option<u32>,
    pub layers: Option<u32>,
    pub heads: Option<u32>,
    pub kv_heads: Option<u32>,
    pub embedding: Option<u32>,
}

impl GgufMeta {
    /// Bytes of K/V for `ctx` tokens, in f16 (llama.cpp's default cache).
    ///
    /// Two tensors (K and V), `layers` of them, `kv_heads x head_dim` each, two
    /// bytes per value — llama.cpp's own arithmetic. None when the header did not
    /// carry the shapes, so the caller shows nothing rather than a made-up figure.
    pub fn kv_bytes(&self, ctx: u64) -> Option<u64> {
        let layers = self.layers? as u64;
        let kv_heads = self.kv_heads? as u64;
        let heads = self.heads.filter(|h| *h > 0).map(u64::from).unwrap_or(kv_heads) as u64;
        let embed = self.embedding? as u64;
        if kv_heads == 0 || heads == 0 || embed == 0 {
            return None;
        }
        let head_dim = embed / heads;
        Some(2 * layers * kv_heads * head_dim * ctx * 2)
    }
}

/// Parse a GGUF header (the first ~512 KiB of the file, tensors excluded).
///
/// Unknown value types stop the walk: the caller keeps what it already has
/// instead of failing — a wrong shape would be worse than no shape.
pub fn gguf_meta(bytes: &[u8]) -> GgufMeta {
    let mut meta = GgufMeta::default();
    if bytes.len() < 24 || bytes.get(0..4) != Some(b"GGUF") {
        return meta;
    }
    let Some(n_kv) = rd_u64(bytes, 16) else { return meta };
    let mut pos = 24;
    let mut arch = String::new();
    for _ in 0..(n_kv as usize).min(512) {
        let Some(key) = rd_str(bytes, &mut pos) else { return meta };
        let Some(vtype) = rd_u32(bytes, pos) else { return meta };
        pos += 4;
        if vtype == 4 {
            let Some(value) = rd_u32(bytes, pos) else { return meta };
            pos += 4;
            if key == "general.architecture" {
                continue;
            }
            // Suffixes, not the last segment: the keys are
            // `<arch>.attention.head_count` and `<arch>.attention.head_count_kv`,
            // and taking `rsplit('.')` matched neither (2026-11-17 — the plan
            // silently reported no shapes at all).
            let mine = arch.is_empty() || key.starts_with(&format!("{arch}."));
            if mine {
                if key.ends_with(".attention.head_count_kv") || key == "attention.head_count_kv" {
                    meta.kv_heads = Some(value);
                } else if key.ends_with(".attention.head_count") || key == "attention.head_count" {
                    meta.heads = Some(value);
                } else if key.ends_with(".context_length") || key == "context_length" {
                    meta.context = Some(value);
                } else if key.ends_with(".block_count") || key == "block_count" {
                    meta.layers = Some(value);
                } else if key.ends_with(".embedding_length") || key == "embedding_length" {
                    meta.embedding = Some(value);
                }
            }
        } else if vtype == 8 {
            let Some(s) = rd_str(bytes, &mut pos) else { return meta };
            if key == "general.architecture" {
                arch = s;
            }
        } else if vtype == 9 {
            let (Some(elem), Some(n)) = (rd_u32(bytes, pos), rd_u64(bytes, pos + 4)) else { return meta };
            pos += 12;
            if n > 1_000_000 {
                return meta;
            }
            if elem == 8 {
                for _ in 0..n {
                    if rd_str(bytes, &mut pos).is_none() {
                        return meta;
                    }
                }
            } else {
                let Some(size) = gguf_scalar_size(elem) else { return meta };
                match size.checked_mul(n as usize) {
                    Some(b) => pos += b,
                    None => return meta,
                }
            }
        } else {
            let Some(size) = gguf_scalar_size(vtype) else { return meta };
            pos += size;
        }
        if pos > bytes.len() {
            return meta;
        }
        if meta.context.is_some() && meta.layers.is_some() && meta.kv_heads.is_some() && meta.embedding.is_some() {
            break;
        }
    }
    meta
}

/// `gguf_meta` over the first bytes of a file: what a header-only reader wants.
pub fn gguf_context(bytes: &[u8]) -> Option<u32> {
    gguf_meta(bytes).context
}

/// One repository, as the admin dialog needs it: is it a GGUF (llama.cpp) or a
/// transformers checkpoint (vLLM), how big, how much context, and the engine
/// arguments that fit.
pub async fn repo_preset(repo: &str) -> Result<Value, HfError> {
    let repo = repo.trim().trim_matches('/');
    if !repo.contains('/') || repo.len() > 200 || repo.contains("..") {
        return Err(HfError::Unavailable("identifiant de modèle invalide".into()));
    }
    let info = repo_json(repo, true).await?;
    let files: Vec<(String, i64)> = info
        .get("siblings")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|s| {
                    let name = s.get("rfilename").and_then(Value::as_str)?.to_string();
                    Some((name, s.get("size").and_then(Value::as_i64).unwrap_or(0)))
                })
                .collect()
        })
        .unwrap_or_default();
    let total_bytes: i64 = files.iter().map(|(_, n)| *n).sum();
    let gguf: Vec<&(String, i64)> = files.iter().filter(|(n, _)| n.to_ascii_lowercase().ends_with(".gguf")).collect();
    let is_gguf = !gguf.is_empty();
    let has_safetensors = files.iter().any(|(n, _)| n.ends_with(".safetensors"));
    let engine = if is_gguf { "llamacpp" } else { "vllm" };

    // The context: the GGUF header when we have one, `config.json` otherwise.
    let mut context = None;
    if is_gguf {
        // The smallest qualifying file is the model itself, not a projection.
        if let Some((name, _)) = gguf
            .iter()
            .filter(|(n, _)| !n.to_ascii_lowercase().contains("mmproj"))
            .min_by_key(|(_, size)| *size)
        {
            context = gguf_header_context(repo, name).await;
        }
    } else if has_safetensors {
        if let Ok(cfg) = repo_json_file(repo, "config.json").await {
            context = cfg.get("max_position_embeddings").and_then(Value::as_i64);
        }
    }
    let ctx = context.filter(|c| *c >= 1024).unwrap_or(32768);
    // Half the window is generation: the catalogue advertises input + output.
    let args = match engine {
        "llamacpp" => format!(
            "--ctx-size {} --parallel 4 --n-predict {} --n-gpu-layers 999 --flash-attn on --jinja",
            ctx,
            (ctx / 4).max(1024)
        ),
        _ => format!("--max-model-len {}", ctx),
    };
    Ok(json!({
        "ok": true,
        "hf_model_id": repo,
        "name": preset_name(repo),
        "engine": engine,
        "vllm_args": args,
        "context": ctx,
        "size_gb": (total_bytes as f64 / 1e9 * 10.0).round() / 10.0,
        "gguf": is_gguf,
        "safetensors": has_safetensors,
        "gated": info.get("gated").cloned().unwrap_or(json!(false)),
        "downloads": info.get("downloads").cloned().unwrap_or(json!(0)),
        "pipeline_tag": info.get("pipeline_tag").cloned().unwrap_or(json!(null)),
        "context_source": if is_gguf { "gguf" } else { "config.json" },
    }))
}

/// `GET /api/models/{repo}?blobs=true` (sizes included), with the token.
async fn repo_json(repo: &str, blobs: bool) -> Result<Value, HfError> {
    let url = if blobs {
        format!("https://huggingface.co/api/models/{repo}?blobs=true")
    } else {
        format!("https://huggingface.co/api/models/{repo}")
    };
    get_json(&url, Duration::from_secs(15)).await
}

/// One file of a repository, as JSON (`config.json`).
async fn repo_json_file(repo: &str, file: &str) -> Result<Value, HfError> {
    get_json(&format!("https://huggingface.co/{repo}/resolve/main/{file}"), Duration::from_secs(15)).await
}

/// The header of one GGUF file, through an HTTP Range request.
async fn gguf_header_context(repo: &str, file: &str) -> Option<i64> {
    let url = format!("https://huggingface.co/{repo}/resolve/main/{file}");
    let mut req = crate::sidecars::http().get(url).timeout(Duration::from_secs(20)).header("Range", "bytes=0-524287");
    let t = token();
    if !t.is_empty() {
        req = req.bearer_auth(t);
    }
    let resp = req.send().await.ok()?;
    let bytes = resp.bytes().await.ok()?;
    gguf_context(&bytes).map(i64::from)
}

async fn get_json(url: &str, timeout: Duration) -> Result<Value, HfError> {
    let mut req = crate::sidecars::http().get(url).timeout(timeout);
    let t = token();
    if !t.is_empty() {
        req = req.bearer_auth(t);
    }
    let resp = req.send().await.map_err(|e| {
        HfError::Unavailable(format!("HF injoignable ({})", if e.is_timeout() { "TimeoutError" } else { "ConnectionError" }))
    })?;
    if !resp.status().is_success() {
        return Err(HfError::Unavailable(format!("HF a répondu {}", resp.status().as_u16())));
    }
    resp.json::<Value>().await.map_err(|_| HfError::Unavailable("réponse HF illisible".into()))
}

#[cfg(test)]
mod preset_tests {
    use super::*;

    #[test]
    fn a_catalog_name_follows_the_repository() {
        assert_eq!(preset_name("Qwen/Qwen3-8B-GGUF"), "qwen3-8b-gguf");
        assert_eq!(preset_name("unsloth/DeepSeek-R1.Q4"), "deepseek-r1-q4");
        assert_eq!(preset_name("Org/Model__x"), "model-x");
        assert_eq!(preset_name("a/.."), "");
        assert!(preset_name(&"z".repeat(200)).len() <= 60);
    }

    /// A minimal but real GGUF header: magic, version, counts, then the two keys
    /// llama.cpp writes for a model, with an array in front of them.
    fn synthetic_header(context: u32) -> Vec<u8> {
        let mut b = Vec::new();
        b.extend_from_slice(b"GGUF");
        b.extend_from_slice(&3u32.to_le_bytes());          // version
        b.extend_from_slice(&1u64.to_le_bytes());          // n_tensors
        b.extend_from_slice(&3u64.to_le_bytes());          // n_kv
        let mut push_str = |b: &mut Vec<u8>, s: &str| {
            b.extend_from_slice(&(s.len() as u64).to_le_bytes());
            b.extend_from_slice(s.as_bytes());
        };
        push_str(&mut b, "general.architecture");
        b.extend_from_slice(&8u32.to_le_bytes());          // string
        push_str(&mut b, "qwen3");
        push_str(&mut b, "tokenizer.ggml.tokens");
        b.extend_from_slice(&9u32.to_le_bytes());          // array
        b.extend_from_slice(&8u32.to_le_bytes());          // of strings
        b.extend_from_slice(&2u64.to_le_bytes());
        push_str(&mut b, "a");
        push_str(&mut b, "b");
        push_str(&mut b, "qwen3.context_length");
        b.extend_from_slice(&4u32.to_le_bytes());          // u32
        b.extend_from_slice(&context.to_le_bytes());
        b
    }

    #[test]
    fn the_context_is_read_from_the_header() {
        assert_eq!(gguf_context(&synthetic_header(262144)), Some(262144));
        assert_eq!(gguf_context(&synthetic_header(4096)), Some(4096));
    }

    #[test]
    fn a_non_gguf_answers_none() {
        assert_eq!(gguf_context(b"not a gguf file at all"), None);
        assert_eq!(gguf_context(&[]), None);
        let mut truncated = synthetic_header(8192);
        truncated.truncate(40);
        assert_eq!(gguf_context(&truncated), None);
    }
}
