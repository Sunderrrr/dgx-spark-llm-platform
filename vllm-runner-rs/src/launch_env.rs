//! The environment of a model process (launch_env.py): an explicit minimal
//! environment, each variable a measured workaround for one engine or GB10 quirk.
use std::collections::BTreeMap;

/// `launch_env.build(engine, model_env, hf_home)`. `root_env` is the runner's
/// own environment (the PATH, HOME, HF_TOKEN it passes on).
pub fn build(engine: &str, model_env: &BTreeMap<String, String>, hf_home: &str, root_env: &dyn Fn(&str) -> Option<String>) -> BTreeMap<String, String> {
    let mut env: BTreeMap<String, String> = BTreeMap::new();
    env.insert("PATH".into(), root_env("PATH").unwrap_or_else(|| "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin".into()));
    env.insert("HOME".into(), root_env("HOME").unwrap_or_else(|| "/root".into()));
    env.insert("HF_HOME".into(), hf_home.into());
    env.insert("PYTHONUNBUFFERED".into(), "1".into());
    env.insert("FLASHINFER_DISABLE_VERSION_CHECK".into(), "1".into());
    env.insert("VLLM_USE_DEEP_GEMM".into(), "0".into());
    env.insert("MAX_JOBS".into(), root_env("MAX_JOBS").unwrap_or_else(|| "4".into()));
    env.insert("VLLM_HOST_IP".into(), "127.0.0.1".into());
    env.insert("GLOO_SOCKET_IFNAME".into(), "lo".into());
    for (k, v) in model_env {
        env.insert(k.clone(), v.clone());
    }
    if let Some(t) = root_env("HF_TOKEN").filter(|t| !t.is_empty()) {
        env.insert("HF_TOKEN".into(), t);
    }
    if engine == "ds4" {
        env.insert("DS4_KV_TURBO".into(), "1".into());
    }
    env
}
