//! The Rust validation against the answers the former runner.py gave (tests/fixtures,
//! recorded then): every case must agree, the refusal wording included.
//!
//! `validate.json` is a recorded oracle, not a generated one — runner.py is gone
//! (only `vllm-runner/patches/` remains), so the file cannot be regenerated.
//! On 2026-10-29, 44 of its cases were rewritten by hand: they exercised the
//! engine-version pseudo-flags (`--vllm-025/027/028/nightly`, `--llama-k2`,
//! `--llama-next`, `--llama-prism`, `--llama-atomic`) whose binaries were
//! deleted, so "python accepted them" became "the runner refuses them". Every
//! other case still carries the Python answer untouched — that is what makes it
//! a parity test. Changing a case here must be a deliberate decision, not a way
//! to make the suite green.
use runner_rs::validate::{repo_id_of, validate_args};
use serde_json::Value;

#[test]
fn validation_matches_runner_py_on_every_case() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/validate.json")).unwrap();
    let mut bad = Vec::new();
    for c in &cases {
        let engine = c["engine"].as_str().unwrap();
        let hf = c["hf_id"].as_str().unwrap();
        let args = c["args"].as_str().unwrap();
        let got = validate_args(args, engine, hf);
        match (c["ok"].as_bool().unwrap(), got) {
            (true, Ok(tokens)) => {
                let want: Vec<&str> = c["result"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
                if want != tokens.iter().map(String::as_str).collect::<Vec<_>>() {
                    bad.push(format!("{engine} {hf:?} {args:?}: tokens differ"));
                }
            }
            (false, Err(msg)) => {
                if c["result"].as_str().unwrap() != msg {
                    bad.push(format!("{engine} {hf:?} {args:?}: want {:?}, got {msg:?}", c["result"]));
                }
            }
            (ok, got) => bad.push(format!("{engine} {hf:?} {args:?}: python ok={ok}, rust {got:?}")),
        }
    }
    assert!(bad.is_empty(), "{} differing cases, first: {:?}", bad.len(), bad.first());
}

#[test]
fn repo_identity_matches_runner_py() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/repo_id.json")).unwrap();
    for c in cases {
        assert_eq!(repo_id_of(c["hf_id"].as_str().unwrap()), c["repo"].as_str().unwrap());
    }
}
