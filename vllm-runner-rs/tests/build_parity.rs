//! The command construction against the former runner.py (tests/fixtures/build_cmd.json,
//! recorded then): the same argv, the same refusals, on a copy
//! of the fixture tree in a scratch directory.
use runner_rs::build::{build_cmd, Paths};
use serde_json::Value;
use std::fs;
use std::path::Path;

fn copy_tree(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for e in fs::read_dir(src).unwrap() {
        let e = e.unwrap();
        let (from, to) = (e.path(), dst.join(e.file_name()));
        let ft = e.file_type().unwrap();
        if ft.is_symlink() {
            std::os::unix::fs::symlink(fs::read_link(&from).unwrap(), &to).unwrap();
        } else if ft.is_dir() {
            copy_tree(&from, &to);
        } else {
            fs::copy(&from, &to).unwrap();
        }
    }
}

#[test]
fn command_construction_matches_runner_py() {
    let scratch = std::env::temp_dir().join(format!("runner-build-{}", std::process::id()));
    let _ = fs::remove_dir_all(&scratch);
    let tree = scratch.join("root");
    copy_tree(&Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/tree"), &tree);
    let root = tree.to_string_lossy().to_string();
    // The same locations the former runner.py was run with, not the environment.
    let p = Paths {
        vllm_bin: "/root/.local/bin/vllm".into(),
        llama_bin: "/root/llama.cpp/build/bin/llama-server".into(),
        ds4_bin: "/root/ds4-nvfp4-spark/ds4-server".into(),
        hf_home: format!("{root}/hf"),
        models_dir: format!("{root}/models"),
        templates_dir: format!("{root}/templates"),
        tabby_dir: format!("{root}/tabby"),
        exl3_py: "/root/venvs/exl3/bin/python".into(),
        exl3_run_dir: format!("{root}/home/exl3"),
    };
    let cases: Vec<Value> = serde_json::from_str(&include_str!("fixtures/build_cmd.json").replace("@ROOT@", &root)).unwrap();
    let mut bad = Vec::new();
    for c in &cases {
        let extra: Vec<String> = c["args"].as_str().unwrap().split_whitespace().map(str::to_string).collect();
        let bin = c["bin"].as_str();
        let got = build_cmd(&p, c["hf_id"].as_str().unwrap(), c["name"].as_str().unwrap(), &extra, c["engine"].as_str().unwrap(), bin);
        let label = format!("{} {:?} {:?} bin={:?}", c["engine"], c["hf_id"], c["args"], bin);
        match (c["ok"].as_bool().unwrap(), got) {
            (true, Ok(cmd)) => {
                let want: Vec<String> = c["cmd"].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
                if want != cmd {
                    bad.push(format!("{label}: argv differs\n   want {want:?}\n   got  {cmd:?}"));
                }
            }
            (false, Err(e)) => {
                if c["error"].as_str().unwrap() != e.message() || c["kind"].as_str().unwrap() != e.kind() {
                    bad.push(format!("{label}: want {} {:?}, got {} {:?}", c["kind"], c["error"], e.kind(), e.message()));
                }
            }
            (ok, got) => bad.push(format!("{label}: python ok={ok}, rust {got:?}")),
        }
    }
    let _ = fs::remove_dir_all(&scratch);
    assert!(bad.is_empty(), "{} differing cases, first:\n{}", bad.len(), bad.first().cloned().unwrap_or_default());
}
