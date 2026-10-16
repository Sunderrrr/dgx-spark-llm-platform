//! The launch environment against launch_env.py (tests/fixtures/launch_env.json).
use runner_rs::launch_env::build;
use std::collections::BTreeMap;

#[test]
fn environment_matches_launch_env_py() {
    let cases: serde_json::Value = serde_json::from_str(include_str!("fixtures/launch_env.json")).unwrap();
    for c in cases.as_array().unwrap() {
        let root: BTreeMap<String, String> = c["root"].as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect();
        let model_env: BTreeMap<String, String> = c["model_env"].as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect();
        let got = build(c["engine"].as_str().unwrap(), &model_env, "/cache/hf", &|k| root.get(k).cloned());
        let want: BTreeMap<String, String> = c["env"].as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect();
        assert_eq!(got, want, "case {c}");
    }
}
