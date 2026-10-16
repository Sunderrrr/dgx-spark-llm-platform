//! The Rust validation against runner.py's own answers (tests/fixtures, written
//! by tools/gen.py): every case must agree, the refusal wording included.
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
