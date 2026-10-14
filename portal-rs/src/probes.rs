//! Service probes, parity with dgx-portal/app.py `_service_reachable`.
use std::time::Duration;

/// Runner base URL (`VLLM_RUNNER_URL`, as for the Flask portal).
pub fn runner_url() -> String {
    env_url("VLLM_RUNNER_URL", "http://host.docker.internal:8001")
}

/// LiteLLM base URL (`LITELLM_URL`): the proxy listens on 4001 inside the
/// Docker network (the same port Traefik and dgx-portal use).
pub fn litellm_url() -> String {
    env_url("LITELLM_URL", "http://litellm:4001")
}

fn env_url(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
        .trim_end_matches('/')
        .to_string()
}

/// True when the service answers with one of the `expect` statuses within 3 s.
pub async fn reachable(url: &str, expect: &[u16]) -> bool {
    let Ok(client) = reqwest::Client::builder().timeout(Duration::from_secs(3)).build() else {
        return false;
    };
    match client.get(url).send().await {
        Ok(resp) => expect.contains(&resp.status().as_u16()),
        Err(_) => false,
    }
}

/// The runner answers 401 without a token: that still proves it is up.
pub async fn probe_runner() -> bool {
    reachable(&format!("{}/status", runner_url()), &[200, 401]).await
}

/// `/health/liveliness`, not `/health`: since LiteLLM 1.102 `/health` needs an
/// API key and answers 401 without one.
pub async fn probe_litellm() -> bool {
    reachable(&format!("{}/health/liveliness", litellm_url()), &[200]).await
}
