use std::time::Duration;
use reqwest::Client;

/// Probe runner service on :8001
/// 401 = unauthorized but reachable (no RUNNER_TOKEN in env); treat as success for health
pub async fn probe_runner() -> bool {
    let client = Client::builder()
        .timeout(Duration::from_secs(2))
        .build();

    match client {
        Ok(client) => {
            match client
                .get("http://host.docker.internal:8001/status")
                .bearer_auth("") // Will return 401 if runner requires auth; that's OK
                .send()
                .await
            {
                Ok(resp) => {
                    // 2xx = success; 401 = unauthorized but reachable
                    resp.status().is_success() || resp.status().as_u16() == 401
                },
                Err(_) => false,
            }
        }
        Err(_) => false,
    }
}

/// Probe LiteLLM service on :4000 (Docker network alias, not :4001 from host)
pub async fn probe_litellm() -> bool {
    let client = Client::builder()
        .timeout(Duration::from_secs(2))
        .build();

    match client {
        Ok(client) => {
            match client
                .get("http://litellm:4000/health")
                .send()
                .await
            {
                Ok(resp) => resp.status().is_success(),
                Err(_) => false,
            }
        }
        Err(_) => false,
    }
}
