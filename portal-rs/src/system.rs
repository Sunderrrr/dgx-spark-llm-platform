//! Host and runner state, parity with dgx-portal: the Prometheus exposition
//! (app.py `prom_metrics`), the runner's status (sidecars.runner_status), the
//! platform logo (app.py `api_branding_logo`).
use std::time::Duration;

use base64::Engine;
use serde_json::Value;

use crate::{models, probes};

/// A Python-style scalar for the exposition: an integral float keeps its
/// `.0`, a missing value is `NaN`, a null prints as `None` (f-string of None).
fn py_value(v: Option<&Value>) -> String {
    match v {
        None => "NaN".to_string(),
        Some(Value::Null) => "None".to_string(),
        Some(Value::Bool(b)) => (if *b { "1" } else { "0" }).to_string(),
        Some(Value::Number(n)) => match n.as_f64() {
            Some(f) if n.is_f64() && f.fract() == 0.0 && f.is_finite() => format!("{f:.1}"),
            _ => n.to_string(),
        },
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

/// app.prom_metrics' body from the runner's metrics payload. Pure.
pub fn prometheus_text(m: &Value) -> String {
    let ram = &m["ram"];
    let gpu = &m["gpu"];
    let online = if m["model_status"] == "running" { 1 } else { 0 };
    let gauge = |name: &str, doc: &str, value: String| {
        format!("# HELP dgx_{name} {doc}\n# TYPE dgx_{name} gauge\ndgx_{name} {value}")
    };
    let parts = [
        // Flask: `m.get('cpu_pct') if m.get('cpu_pct') is not None else 'NaN'`.
        gauge("cpu_pct", "CPU usage (%)", py_value(m.get("cpu_pct").filter(|v| !v.is_null()))),
        gauge("ram_used_gb", "Host RAM used (GB)", py_value(ram.get("used_gb"))),
        gauge("ram_total_gb", "Host RAM total (GB)", py_value(ram.get("total_gb"))),
        gauge("gpu_util_pct", "GPU utilisation (%)", py_value(gpu.get("util"))),
        gauge("gpu_power_w", "GPU power draw (W)", py_value(gpu.get("power"))),
        gauge("gpu_temp_c", "GPU temperature (C)", py_value(gpu.get("temp"))),
        gauge("model_online", "Served chat model online (1/0)", online.to_string()),
    ];
    format!("{}\n", parts.join("\n"))
}

/// The runner's payload, or None when it does not answer (Flask: `runner_metrics()`).
pub async fn runner_metrics() -> Option<Value> {
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let c = reqwest::Client::builder().timeout(Duration::from_secs(5)).build().ok()?;
    let r = c.get(format!("{}/metrics", probes::runner_url())).bearer_auth(token).send().await.ok()?;
    if !r.status().is_success() {
        return None;
    }
    r.json::<Value>().await.ok()
}

/// sidecars.runner_status: the runner's state; it reads « running » once the
/// served model answers (the runner only logs that, hidden by its log level).
pub async fn runner_status() -> Value {
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let fetched = async {
        let c = reqwest::Client::builder().timeout(Duration::from_secs(3)).build().ok()?;
        let r = c.get(format!("{}/status", probes::runner_url())).bearer_auth(token).send().await.ok()?;
        if !r.status().is_success() {
            return None;
        }
        r.json::<Value>().await.ok()
    }
    .await;
    match fetched {
        Some(mut st) => {
            if st["status"] == "starting" {
                let served = models::running_models().await;
                if st["model"].as_str().is_some_and(|m| served.iter().any(|s| s == m)) {
                    st["status"] = Value::String("running".into());
                }
            }
            st
        }
        None => serde_json::json!({"status": "unreachable", "model": null, "pid": null}),
    }
}

/// The stored logo: `data:image/<type>;base64,<payload>` (or a bare payload,
/// as Flask splits on the first comma). None when absent or undecodable. Pure.
pub fn logo_bytes(value: &str) -> Option<(String, Vec<u8>)> {
    if value.is_empty() {
        return None;
    }
    let payload = value.split_once(',').map(|(_, p)| p).unwrap_or("");
    // base64.b64decode without validate=True: characters outside the alphabet are dropped.
    let cleaned: String = payload.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '+' || *c == '/' || *c == '=').collect();
    let bytes = base64::engine::general_purpose::STANDARD_NO_PAD
        .decode(cleaned.trim_end_matches('='))
        .ok()?;
    let mime = match value.strip_prefix("data:image/") {
        Some(rest) => format!("image/{}", rest.split(';').next().unwrap_or("png")),
        None => "image/png".to_string(),
    };
    Some((mime, bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn prometheus_text_follows_the_flask_exposition() {
        let m = json!({"cpu_pct": 12.5, "ram": {"used_gb": 40.0, "total_gb": 121.0},
                       "gpu": {"util": 77, "power": 60.25, "temp": 55}, "model_status": "running"});
        let text = prometheus_text(&m);
        assert!(text.contains("dgx_cpu_pct 12.5\n"));
        assert!(text.contains("dgx_ram_total_gb 121.0\n"));
        assert!(text.contains("dgx_gpu_util_pct 77\n"));
        assert!(text.contains("dgx_model_online 1\n"));
        assert!(text.ends_with('\n'));
    }

    #[test]
    fn missing_values_are_nan_and_an_offline_model_is_zero() {
        let text = prometheus_text(&json!({}));
        assert!(text.contains("dgx_cpu_pct NaN\n"));
        assert!(text.contains("dgx_gpu_temp_c NaN\n"));
        assert!(text.contains("dgx_model_online 0\n"));
    }

    #[test]
    fn logo_is_decoded_with_its_type() {
        let (mime, bytes) = logo_bytes("data:image/png;base64,aGVsbG8=").unwrap();
        assert_eq!(mime, "image/png");
        assert_eq!(bytes, b"hello");
        assert_eq!(logo_bytes("data:image/svg+xml;base64,PHN2Zz4=").unwrap().0, "image/svg+xml");
        assert!(logo_bytes("").is_none());
    }
}
