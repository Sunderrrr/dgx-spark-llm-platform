//! How long the API takes (latency.py): a bounded window of request durations
//! for /api and /admin, and the percentiles the operator reads. Measured on the
//! backend that serves the traffic: since every route is on portal-rs, the
//! numbers are its own, not Flask's (the Flask probe is retired with it).
use crate::AppState;
use axum::extract::{Request, State};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Instant;

/// `latency.WINDOW`: about a busy day of API calls.
const WINDOW: usize = 10_000;

/// (unix time, seconds, route)
static SAMPLES: Mutex<Vec<(f64, f64, String)>> = Mutex::new(Vec::new());

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `latency.record(route, duration)`.
pub fn record(route: &str, secs: f64) {
    let mut s = SAMPLES.lock().unwrap_or_else(|e| e.into_inner());
    s.push((now(), secs, route.to_string()));
    if s.len() > WINDOW {
        let extra = s.len() - WINDOW;
        s.drain(..extra);
    }
}

/// Only the API and the administration are measured: pages and assets would
/// drown the signal. The probe does not measure itself.
pub fn measured(path: &str) -> bool {
    (path.starts_with("/api/") || path.starts_with("/admin")) && path != "/api/latency"
}

/// Middleware: the duration of each measured request, from arrival to answer.
pub async fn layer(req: Request, next: Next) -> Response {
    let path = req.uri().path().to_string();
    let t0 = Instant::now();
    let resp = next.run(req).await;
    if measured(&path) {
        record(&path, t0.elapsed().as_secs_f64());
    }
    resp
}

/// `latency_summary(window_s)`: percentiles over the window, the slowest route.
pub fn summary(window_s: Option<f64>) -> Value {
    let t = now();
    let taken: Vec<(f64, f64, String)> = {
        let s = SAMPLES.lock().unwrap_or_else(|e| e.into_inner());
        s.iter().filter(|e| window_s.is_none_or(|w| t - e.0 <= w)).cloned().collect()
    };
    if taken.is_empty() {
        return json!({"n": 0, "p50_ms": null, "p95_ms": null, "max_ms": null, "slowest": null, "window_min": 0});
    }
    let mut sorted: Vec<f64> = taken.iter().map(|e| e.1).collect();
    sorted.sort_by(f64::total_cmp);
    let pct = |q: f64| -> i64 {
        let i = ((sorted.len() as f64 * q) as usize).min(sorted.len() - 1);
        (sorted[i] * 1000.0).round_ties_even() as i64
    };
    let mut slow: HashMap<&str, f64> = HashMap::new();
    for (_, d, r) in &taken {
        let e = slow.entry(r.as_str()).or_insert(0.0);
        if *d > *e {
            *e = *d;
        }
    }
    let slowest = slow.iter().max_by(|a, b| a.1.total_cmp(b.1)).map(|(k, _)| k.to_string());
    json!({
        "n": taken.len(),
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "max_ms": (sorted[sorted.len() - 1] * 1000.0).round_ties_even() as i64,
        "slowest": slowest,
        "window_min": ((t - taken[0].0) / 60.0).round_ties_even() as i64,
    })
}

/// GET /api/latency - login required: the platform's own telemetry.
pub async fn api_latency(State(state): State<AppState>, headers: axum::http::HeaderMap) -> Response {
    match crate::auth::require_session(&headers, &state.db).await {
        Ok(_) => Json(summary(None)).into_response(),
        Err(e) => e.into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn what_is_measured() {
        assert!(measured("/api/home"));
        assert!(measured("/admin/platform"));
        assert!(!measured("/api/latency"));
        assert!(!measured("/image"));
    }

    #[test]
    fn percentiles_and_slowest_route() {
        *SAMPLES.lock().unwrap() = vec![(now(), 0.010, "/api/a".into()), (now(), 0.300, "/api/b".into()), (now(), 0.020, "/api/a".into())];
        let s = summary(None);
        assert_eq!(s["n"], 3);
        assert_eq!(s["max_ms"], 300);
        assert_eq!(s["slowest"], "/api/b");
        assert_eq!(s["p50_ms"], 20);
        SAMPLES.lock().unwrap().clear();
    }
}
