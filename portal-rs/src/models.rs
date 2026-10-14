//! What the engine serves right now, parity with vllm_health.get_running_models.
//!
//! A FAILED probe is not proof that nothing runs: the engine's control plane
//! freezes during a large prefill (measured 2026-10-02, /v1/models silent for
//! ~15 s slices, minutes for the biggest prompts). The last list seen is kept
//! for `GRACE` after the last successful probe; a probe that answers with an
//! empty list clears it at once — that one IS proof of absence.
use std::sync::Mutex;
use std::time::{Duration, Instant};

const CACHE: Duration = Duration::from_secs(5);
const GRACE: Duration = Duration::from_secs(600);

struct State {
    checked: Option<Instant>,
    ok: Option<Instant>,
    models: Vec<String>,
}

static STATE: Mutex<State> = Mutex::new(State { checked: None, ok: None, models: Vec::new() });

/// Engine API base (`VLLM_API_URL`, as for the Flask portal).
pub fn engine_api() -> String {
    std::env::var("VLLM_API_URL")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "http://host.docker.internal:8000/v1".to_string())
        .trim_end_matches('/')
        .to_string()
}

/// What a probe outcome leaves in the state, at `now`. Pure: tested below.
fn apply(state: &mut State, now: Instant, probe: Option<Vec<String>>) -> Vec<String> {
    state.checked = Some(now);
    match probe {
        Some(models) => {
            state.ok = Some(now);
            state.models = models.clone();
            models
        }
        None => match state.ok {
            Some(ok) if now.duration_since(ok) < GRACE => state.models.clone(),
            _ => Vec::new(),
        },
    }
}

async fn probe() -> Option<Vec<String>> {
    let client = reqwest::Client::builder().timeout(Duration::from_secs(3)).build().ok()?;
    let resp = client.get(format!("{}/models", engine_api())).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let body: serde_json::Value = resp.json().await.ok()?;
    Some(
        body.get("data")?
            .as_array()?
            .iter()
            .filter_map(|m| m.get("id").and_then(|v| v.as_str()).map(str::to_string))
            .collect(),
    )
}

/// The served model ids (cached 5 s, last known list kept 10 min on failure).
pub async fn running_models() -> Vec<String> {
    {
        let state = STATE.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(t) = state.checked {
            if t.elapsed() < CACHE {
                return state.models_if_fresh();
            }
        }
    }
    let outcome = probe().await;
    let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    apply(&mut state, Instant::now(), outcome)
}

impl State {
    /// Within the 5 s cache: the list, unless its grace has run out.
    fn models_if_fresh(&self) -> Vec<String> {
        match self.ok {
            Some(ok) if ok.elapsed() < GRACE => self.models.clone(),
            _ => Vec::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh() -> State {
        State { checked: None, ok: None, models: Vec::new() }
    }

    #[test]
    fn a_successful_probe_is_returned_and_remembered() {
        let mut s = fresh();
        let t = Instant::now();
        assert_eq!(apply(&mut s, t, Some(vec!["m".into()])), vec!["m".to_string()]);
        assert_eq!(s.models, vec!["m".to_string()]);
    }

    #[test]
    fn a_failed_probe_keeps_the_last_list_within_the_grace() {
        let mut s = fresh();
        let t = Instant::now();
        apply(&mut s, t, Some(vec!["m".into()]));
        assert_eq!(apply(&mut s, t + Duration::from_secs(60), None), vec!["m".to_string()]);
    }

    #[test]
    fn past_the_grace_a_failed_probe_means_no_model() {
        let mut s = fresh();
        let t = Instant::now();
        apply(&mut s, t, Some(vec!["m".into()]));
        assert!(apply(&mut s, t + GRACE + Duration::from_secs(1), None).is_empty());
    }

    #[test]
    fn an_empty_answer_clears_at_once() {
        let mut s = fresh();
        let t = Instant::now();
        apply(&mut s, t, Some(vec!["m".into()]));
        assert!(apply(&mut s, t + Duration::from_secs(1), Some(vec![])).is_empty());
    }

    #[test]
    fn never_seen_and_failing_means_no_model() {
        let mut s = fresh();
        assert!(apply(&mut s, Instant::now(), None).is_empty());
    }
}
