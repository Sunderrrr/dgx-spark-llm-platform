//! `_validate_vllm_args`, `_flags_for` and `_repo_id_of` of runner.py.
use crate::flags;
use regex::Regex;
use std::sync::OnceLock;

/// The allow-lists of an engine: (bool flags, value flags).
pub fn flags_for(engine: &str) -> (&'static [&'static str], &'static [&'static str]) {
    match engine {
        "exllamav3" => (flags::EXL3_BOOL_FLAGS, flags::EXL3_VALUE_FLAGS),
        "llamacpp" => (flags::LLAMA_BOOL_FLAGS, flags::LLAMA_VALUE_FLAGS),
        "ds4" => (flags::DS4_BOOL_FLAGS, flags::DS4_VALUE_FLAGS),
        "ocr" => (flags::OCR_BOOL_FLAGS, flags::OCR_VALUE_FLAGS),
        _ => (flags::BOOL_FLAGS, flags::VALUE_FLAGS),
    }
}

/// `_repo_id_of`: the HF repository of an id or of a cache snapshot path.
pub fn repo_id_of(hf_id: &str) -> String {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"models--([^/]+?)--(.+?)/snapshots/").expect("static regex"));
    match re.captures(hf_id) {
        Some(c) => format!("{}/{}", &c[1], &c[2]),
        None => hf_id.to_string(),
    }
}

/// `_validate_vllm_args(extra, engine, hf_id)`: Ok(tokens) or the refusal.
pub fn validate_args(extra: &str, engine: &str, hf_id: &str) -> Result<Vec<String>, String> {
    let (bools, values) = flags_for(engine);
    let tokens: Vec<&str> = extra.split_whitespace().collect();
    let mut i = 0;
    while i < tokens.len() {
        let tok = tokens[i];
        if bools.contains(&tok) {
            i += 1;
        } else if tok == "--trust-remote-code" && flags::TRUST_RC_MODELS.contains(&repo_id_of(hf_id).as_str()) {
            i += 1;
        } else if values.contains(&tok) {
            if i + 1 >= tokens.len() || tokens[i + 1].starts_with("--") {
                return Err(format!("flag {tok} requires a value"));
            }
            i += 2;
        } else {
            return Err(format!("flag not allowed: {tok}"));
        }
    }
    Ok(tokens.into_iter().map(str::to_string).collect())
}
