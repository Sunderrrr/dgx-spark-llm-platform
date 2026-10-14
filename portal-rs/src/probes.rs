use std::time::Duration;
use reqwest::Client;

/// Probe runner service on :8001
pub async fn probe_runner() -> bool {
    let client = Client::builder()
        .timeout(Duration::from_secs(2))
        .build();

    match client {
        Ok(client) => {
            match client
                .get("http://host.docker.internal:8001/status")
                .bearer_auth("") // Runner token would go here if we had it
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

/// Probe LiteLLM service on :4001
pub async fn probe_litellm() -> bool {
    let client = Client::builder()
        .timeout(Duration::from_secs(2))
        .build();

    match client {
        Ok(client) => {
            match client
                .get("http://host.docker.internal:4001/health")
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
