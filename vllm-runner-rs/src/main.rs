//! The vllm-runner daemon (runner.py's HTTP surface, port 8001). Every route
//! needs `Authorization: Bearer <RUNNER_TOKEN>`; the answers and their keys are
//! those of the Flask service it replaces.
use axum::{
    body::{Body, Bytes},
    extract::{Extension, Query, Request, State},
    http::{header, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use runner_rs::decisions::{Decisions, DecisionsCfg};
use runner_rs::service::{self, App, Config};
use runner_rs::state;
use runner_rs::supervisor::Supervisor;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::Path as FsPath;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

/// `_LOGS_TAIL`
const LOGS_TAIL: usize = 400;

fn json_or_empty(body: &Bytes) -> Value {
    serde_json::from_slice::<Value>(body).ok().filter(Value::is_object).unwrap_or_else(|| json!({}))
}

/// `_check_auth`: every route, before anything else.
async fn auth(State(app): State<Arc<App>>, req: Request, next: Next) -> Response {
    let header = req.headers().get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
    if !runner_rs::token_ok(&header, &app.cfg.runner_token) {
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": "unauthorized"}))).into_response();
    }
    next.run(req).await
}

/// `_sudo(*cmd, timeout)`: (ok, stdout or stderr, stripped).
fn sudo(cmd: &[&str], timeout: Duration) -> (bool, String) {
    let argv: Vec<String> = std::iter::once("sudo".to_string()).chain(["-n".to_string()]).chain(cmd.iter().map(|s| s.to_string())).collect();
    match run_timed(&argv, timeout) {
        Ok((code, out, err)) => {
            let text = if out.is_empty() { err } else { out };
            (code == 0, text.trim().to_string())
        }
        Err(e) => (false, e),
    }
}

/// `_combined_sudo`: stdout and stderr together.
fn combined_sudo(cmd: &[&str], timeout: Duration) -> String {
    let argv: Vec<String> = std::iter::once("sudo".to_string()).chain(["-n".to_string()]).chain(cmd.iter().map(|s| s.to_string())).collect();
    match run_timed(&argv, timeout) {
        Ok((_, out, err)) => format!("{out}{err}").trim().to_string(),
        Err(e) => e,
    }
}

/// Run with a time bound; on expiry, the message subprocess.TimeoutExpired gives.
fn run_timed(argv: &[String], timeout: Duration) -> Result<(i32, String, String), String> {
    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let start = Instant::now();
    loop {
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(_) => break,
            None if start.elapsed() >= timeout => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("Command {:?} timed out after {} seconds", argv, timeout.as_secs()));
            }
            None => thread::sleep(Duration::from_millis(20)),
        }
    }
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    Ok((out.status.code().unwrap_or(-1), String::from_utf8_lossy(&out.stdout).into_owned(), String::from_utf8_lossy(&out.stderr).into_owned()))
}

/// `docker inspect <c>` → {"status": running|stopped} or unknown.
fn container_status(c: &str) -> Value {
    let (ok, out) = sudo(&["/usr/bin/docker", "inspect", c], Duration::from_secs(20));
    if !ok {
        return json!({"status": "unknown", "detail": out});
    }
    match serde_json::from_str::<Value>(&out).ok().and_then(|v| v.get(0).and_then(|s| s.get("State")).cloned()) {
        Some(state) => {
            let running = state.get("Running").is_some_and(|r| r.as_bool().unwrap_or(false));
            json!({"status": if running { "running" } else { "stopped" }})
        }
        None => json!({"status": "unknown", "detail": "list index out of range"}),
    }
}

fn ok_detail(ok: bool, detail: String) -> Json<Value> {
    Json(json!({"ok": ok, "detail": detail}))
}

async fn status(State(app): State<Arc<App>>) -> Json<Value> {
    let s = app.sup.lock();
    let pid = s.proc.as_ref().map(|p| p.pid);
    let mut available = serde_json::Map::new();
    for e in ["vllm", "llamacpp", "ds4", "exllamav3", "vllm-docker"] {
        let here = e == "vllm" || service::engine_bin(&app.cfg, e).is_some_and(|b| FsPath::new(&b).exists());
        available.insert(e.into(), json!(here));
    }
    Json(json!({"status": s.status, "model": s.model, "engine": s.engine, "pid": pid, "engines_available": available}))
}

/// Flask's own page for an unhandled error (werkzeug's wording).
const SERVER_ERROR_PAGE: &str = "<!doctype html>\n<html lang=en>\n<title>500 Internal Server Error</title>\n<h1>Internal Server Error</h1>\n<p>The server encountered an internal error and was unable to complete your request. Either the server is overloaded or there is an error in the application.</p>\n";
/// Flask's own page for an unknown route.
const NOT_FOUND_PAGE: &str = "<!doctype html>\n<html lang=en>\n<title>404 Not Found</title>\n<h1>Not Found</h1>\n<p>The requested URL was not found on the server. If you entered the URL manually please check your spelling and try again.</p>\n";

fn html(code: StatusCode, page: &'static str) -> Response {
    (code, [(header::CONTENT_TYPE, "text/html; charset=utf-8")], page).into_response()
}

async fn not_found() -> Response {
    html(StatusCode::NOT_FOUND, NOT_FOUND_PAGE)
}

async fn logs(State(app): State<Arc<App>>, Query(q): Query<HashMap<String, String>>) -> Response {
    let raw = q.get("n").map(String::as_str).unwrap_or("200");
    let Ok(n) = raw.trim().parse::<i64>() else {
        return html(StatusCode::INTERNAL_SERVER_ERROR, SERVER_ERROR_PAGE);
    };
    let n = n.min(2000);
    let s = app.sup.lock();
    let lines = if n == 0 {
        s.logs.snapshot()
    } else if n > 0 {
        s.logs.tail(n as usize)
    } else {
        s.logs.from((-n) as usize)
    };
    Json(json!({"logs": lines})).into_response()
}

/// `/stream`: the buffer as server-sent events, new lines as they come, a clear
/// when the buffer restarts, a keep-alive comment while idle.
async fn stream(State(app): State<Arc<App>>) -> Response {
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Bytes, std::io::Error>>(256);
    tokio::spawn(async move {
        // the lock is never held across an await
        let snap = app.sup.lock().logs.snapshot();
        let mut last = snap.len();
        for line in snap {
            if tx.send(Ok(Bytes::from(format!("data: {line}\n\n")))).await.is_err() {
                return;
            }
        }
        loop {
            tokio::time::sleep(Duration::from_millis(50)).await;
            let (events, now) = {
                let s = app.sup.lock();
                let cur = s.logs.len();
                if cur < last {
                    let mut ev = String::from("event: clear\ndata: \n\n");
                    for line in s.logs.snapshot() {
                        ev.push_str(&format!("data: {line}\n\n"));
                    }
                    (ev, cur)
                } else if cur > last {
                    let mut ev = String::new();
                    for line in s.logs.from(last) {
                        ev.push_str(&format!("data: {line}\n\n"));
                    }
                    (ev, cur)
                } else {
                    (": ping\n\n".to_string(), cur)
                }
            };
            last = now;
            if tx.send(Ok(Bytes::from(events))).await.is_err() {
                return;
            }
        }
    });
    (
        [(header::CACHE_CONTROL, "no-cache"), (header::HeaderName::from_static("x-accel-buffering"), "no"), (header::CONTENT_TYPE, "text/event-stream")],
        Body::from_stream(tokio_stream::wrappers::ReceiverStream::new(rx)),
    )
        .into_response()
}

async fn launch(State(app): State<Arc<App>>, body: Bytes) -> Response {
    let data = json_or_empty(&body);
    let app2 = app.clone();
    match tokio::task::spawn_blocking(move || app2.launch(&data)).await {
        Ok(Ok((code, v))) => (StatusCode::from_u16(code).unwrap_or(StatusCode::OK), Json(v)).into_response(),
        Ok(Err((code, v))) => (StatusCode::from_u16(code).unwrap_or(StatusCode::BAD_REQUEST), Json(v)).into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn stop(State(app): State<Arc<App>>) -> Json<Value> {
    let app2 = app.clone();
    let st = tokio::task::spawn_blocking(move || app2.stop()).await.unwrap_or("already_stopped");
    Json(json!({"status": st}))
}

fn hf_shape(id: &str) -> bool {
    let re = regex::Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,80}$").expect("static");
    re.is_match(id)
}

async fn delete_files(State(app): State<Arc<App>>, body: Bytes) -> Response {
    let data = json_or_empty(&body);
    let hf = data.get("hf_model_id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    let local = hf.strip_prefix("local:").filter(|r| {
        let re = regex::Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$").expect("static");
        re.is_match(r)
    });
    let (kind, ident) = if let Some(rest) = local {
        ("local", rest.to_string())
    } else if hf_shape(&hf) {
        ("hf", hf.clone())
    } else {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "invalid hf_model_id (local:<folder> or org/name)"}))).into_response();
    };
    if ident.contains("..") {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "invalid hf_model_id (.. refused)"}))).into_response();
    }
    let _ops = app.ops.lock().unwrap_or_else(|e| e.into_inner());
    let running = state::load_last_launch(&app.cfg.state_file).and_then(|v| v.get("hf_model_id").and_then(|h| h.as_str().map(str::to_string)));
    if running.as_deref() == Some(hf.as_str()) {
        return (StatusCode::CONFLICT, Json(json!({"error": "model being served (or waiting to resume): stop it before deleting its files"}))).into_response();
    }
    let (ok, out) = sudo(&["/usr/local/sbin/model-files-rm.sh", kind, &ident], Duration::from_secs(600));
    if !ok {
        let absent = out.contains("absent");
        app.log(&format!("[runner] Deleting the files of {hf}: {} ({})", if absent { "absent" } else { "FAILED" }, tail(&out, 200)));
        let msg = if out.is_empty() { "failed".to_string() } else { tail(&out, 300) };
        let code = if absent { StatusCode::NOT_FOUND } else { StatusCode::INTERNAL_SERVER_ERROR };
        return (code, Json(json!({"error": msg, "absent": absent}))).into_response();
    }
    let size: u64 = out.lines().last().and_then(|l| l.trim().parse().ok()).unwrap_or(0);
    app.log(&format!("[runner] Files of {hf} deleted ({:.1} GiB freed).", size as f64 / 2f64.powi(30)));
    Json(json!({"status": "deleted", "bytes": size})).into_response()
}

/// The last `n` characters (Python's `out[-n:]`).
fn tail(s: &str, n: usize) -> String {
    let count = s.chars().count();
    s.chars().skip(count.saturating_sub(n)).collect()
}

async fn container_status_route(kind: &'static str) -> Json<Value> {
    Json(container_status(kind))
}

/// `docker start|stop <kind>`.
async fn container_action_route(kind: &'static str, action: &'static str) -> Json<Value> {
    let timeout = if action == "start" { 60 } else { 30 };
    let (ok, out) = sudo(&["/usr/bin/docker", action, kind], Duration::from_secs(timeout));
    ok_detail(ok, out)
}

async fn ocr_launch(State(app): State<Arc<App>>, body: Bytes) -> Response {
    let data = json_or_empty(&body);
    let hf = data.get("hf_model_id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    if hf.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "detail": "hf_model_id missing"}))).into_response();
    }
    if !hf_shape(&hf) {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "detail": "invalid hf_model_id"}))).into_response();
    }
    let args = data.get("vllm_args").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let tokens = match runner_rs::validate::validate_args(&args, "ocr", "") {
        Ok(t) => t,
        Err(e) => return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "detail": e}))).into_response(),
    };
    let _ = app;
    let mut argv: Vec<&str> = vec!["/usr/local/sbin/ocr-recreate.sh", &hf];
    argv.extend(tokens.iter().map(String::as_str));
    let (ok, out) = sudo(&argv, Duration::from_secs(120));
    ok_detail(ok, out).into_response()
}

async fn voice_launch(body: Bytes) -> Response {
    let data = json_or_empty(&body);
    let repo = data.get("repo_id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    let script = match repo.as_str() {
        "chatterbox" | "chatterbox-turbo" | "chatterbox-multilingual" => "/usr/local/sbin/voice-recreate.sh",
        "Qwen3-TTS-12Hz-1.7B-Base" | "Qwen3-TTS-12Hz-0.6B-Base" => "/usr/local/sbin/voice-qwen-recreate.sh",
        "kyutai/tts-1.6b-en_fr" => "/usr/local/sbin/voice-kyutai-recreate.sh",
        _ => return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "detail": "invalid repo_id"}))).into_response(),
    };
    let (ok, out) = sudo(&[script, &repo], Duration::from_secs(120));
    ok_detail(ok, out).into_response()
}

async fn asr_launch(body: Bytes) -> Response {
    let data = json_or_empty(&body);
    let model = data.get("model_id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    let allowed = ["openai/whisper-large-v3-turbo", "openai/whisper-large-v3", "openai/whisper-medium", "openai/whisper-small"];
    if !allowed.contains(&model.as_str()) {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "detail": "invalid model_id"}))).into_response();
    }
    let (ok, out) = sudo(&["/usr/local/sbin/asr-recreate.sh", &model], Duration::from_secs(120));
    ok_detail(ok, out).into_response()
}

async fn image_launch(body: Bytes) -> Response {
    let data = json_or_empty(&body);
    let model = data.get("model_id").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    if model != "black-forest-labs/FLUX.2-klein-4B" {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "detail": "invalid model_id"}))).into_response();
    }
    let (ok, out) = sudo(&["/usr/local/sbin/image-recreate.sh", &model], Duration::from_secs(180));
    ok_detail(ok, out).into_response()
}

async fn video_status() -> Json<Value> {
    let (ok, out) = sudo(&["/usr/bin/systemctl", "is-active", "comfyui.service"], Duration::from_secs(20));
    Json(json!({"status": if ok && out.trim() == "active" { "running" } else { "stopped" }}))
}

async fn video_start() -> Json<Value> {
    let (ok, out) = sudo(&["/usr/bin/systemctl", "start", "comfyui.service"], Duration::from_secs(30));
    ok_detail(ok, out)
}

async fn video_stop() -> Json<Value> {
    let (ok, out) = sudo(&["/usr/bin/systemctl", "stop", "comfyui.service"], Duration::from_secs(30));
    ok_detail(ok, out)
}

async fn sidecar_logs_route(kind: &'static str) -> Json<Value> {
    let out = combined_sudo(&["/usr/bin/docker", "logs", "--tail", &LOGS_TAIL.to_string(), kind], Duration::from_secs(20));
    Json(json!({"logs": out.lines().collect::<Vec<_>>()}))
}

async fn video_logs() -> Json<Value> {
    let out = combined_sudo(&["/usr/bin/journalctl", "-u", "comfyui.service", "-n", &LOGS_TAIL.to_string(), "--no-pager"], Duration::from_secs(20));
    Json(json!({"logs": out.lines().collect::<Vec<_>>()}))
}

/// The host's CPU use between two calls (the first call reads 0.0).
fn cpu_pct(prev: &Mutex<Option<(u64, u64)>>) -> f64 {
    let Ok(stat) = std::fs::read_to_string("/proc/stat") else { return 0.0 };
    let first = stat.lines().next().unwrap_or("");
    let v: Vec<u64> = first.split_whitespace().skip(1).filter_map(|x| x.parse().ok()).collect();
    if v.len() < 4 {
        return 0.0;
    }
    let idle = v[3] + v.get(4).copied().unwrap_or(0);
    let total: u64 = v.iter().sum();
    let mut p = prev.lock().unwrap_or_else(|e| e.into_inner());
    let (di, dt) = match *p {
        Some((pi, pt)) => (idle.saturating_sub(pi), total.saturating_sub(pt)),
        None => (0, 0),
    };
    *p = Some((idle, total));
    if dt > 0 { round1((1.0 - di as f64 / dt as f64) * 100.0) } else { 0.0 }
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round_ties_even() / 10.0
}

fn ram() -> Value {
    let text = std::fs::read_to_string("/proc/meminfo").unwrap_or_default();
    let mut vals: HashMap<&str, f64> = HashMap::new();
    for line in text.lines() {
        if let Some((k, rest)) = line.split_once(':') {
            if let Some(kb) = rest.split_whitespace().next().and_then(|v| v.parse::<f64>().ok()) {
                vals.insert(k, kb);
            }
        }
    }
    let total = vals.get("MemTotal").copied().unwrap_or(0.0) / 1048576.0;
    let avail = vals.get("MemAvailable").copied().unwrap_or(0.0) / 1048576.0;
    let used = total - avail;
    json!({"used_gb": round1(used), "total_gb": round1(total), "pct": if total > 0.0 { round1(used / total * 100.0) } else { 0.0 }})
}

fn gpu() -> Value {
    let Some(exe) = std::env::var_os("PATH").and_then(|paths| std::env::split_paths(&paths).map(|d| d.join("nvidia-smi")).find(|p| p.is_file())) else {
        return Value::Null;
    };
    let out = Command::new(exe)
        .args(["--query-gpu=utilization.gpu,power.draw,temperature.gpu", "--format=csv,noheader,nounits"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output();
    let Ok(out) = out else { return Value::Null };
    let text = String::from_utf8_lossy(&out.stdout).to_string();
    let Some(row) = text.lines().next() else { return Value::Null };
    let parts: Vec<&str> = row.split(',').map(str::trim).collect();
    let num = |i: usize| parts.get(i).and_then(|x| x.parse::<f64>().ok());
    json!({"util": num(0), "power": num(1), "temp": num(2)})
}

async fn metrics(State(app): State<Arc<App>>, axum::extract::Extension(prev): axum::extract::Extension<Arc<Mutex<Option<(u64, u64)>>>>) -> Json<Value> {
    let (model, status_now) = {
        let s = app.sup.lock();
        (s.model.clone(), s.status.clone())
    };
    Json(json!({"cpu_pct": cpu_pct(&prev), "ram": ram(), "gpu": gpu(), "model": model, "model_status": status_now}))
}


/// `GET /decisions/status` — the decision model's own state (never the chat
/// model's): what the portal page shows.
async fn decisions_status(Extension(d): Extension<Arc<Decisions>>) -> Json<Value> {
    Json(d.status())
}

/// `POST /decisions/start` — the memory floor refusal is the answer that
/// matters: it leaves the chat model serving.
async fn decisions_start(Extension(d): Extension<Arc<Decisions>>) -> Response {
    match d.start() {
        Ok(v) => (StatusCode::OK, Json(v)).into_response(),
        Err((code, v)) => (StatusCode::from_u16(code).unwrap_or(StatusCode::BAD_REQUEST), Json(v)).into_response(),
    }
}

/// `POST /decisions/stop`.
async fn decisions_stop(Extension(d): Extension<Arc<Decisions>>) -> Response {
    match d.stop() {
        Ok(v) => (StatusCode::OK, Json(v)).into_response(),
        Err((code, v)) => (StatusCode::from_u16(code).unwrap_or(StatusCode::BAD_REQUEST), Json(v)).into_response(),
    }
}

/// `GET /decisions/logs` — the startup journal, capped like the chat model's.
async fn decisions_logs(Extension(d): Extension<Arc<Decisions>>, Query(q): Query<HashMap<String, String>>) -> Json<Value> {
    let tail = q.get("tail").and_then(|v| v.parse::<usize>().ok()).unwrap_or(LOGS_TAIL);
    Json(d.logs(tail))
}

fn config_or_exit() -> Config {
    match Config::from_env() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    }
}

#[tokio::main]
async fn main() {
    let cfg = config_or_exit();
    // Built before `cfg` is moved into the App: the decision service reads the
    // same paths, but keeps its own process, port and log buffer.
    let decisions = Arc::new(Decisions::new(DecisionsCfg::from_env(&cfg)));
    let app = Arc::new(App { cfg, sup: Arc::new(Supervisor::default()), ops: Mutex::new(()), log_stamp_clock: service::file_stamp });
    service::announce_start(&app);
    {
        let w = app.clone();
        thread::spawn(move || service::watchdog(w));
    }
    let prev: Arc<Mutex<Option<(u64, u64)>>> = Arc::new(Mutex::new(None));
    let mut router = Router::new()
        .route("/status", get(status))
        .route("/logs", get(logs))
        .route("/stream", get(stream))
        .route("/launch", post(launch))
        .route("/stop", post(stop))
        .route("/models/delete-files", post(delete_files))
        .route("/metrics", get(metrics))
        .route("/decisions/status", get(decisions_status))
        .route("/decisions/start", post(decisions_start))
        .route("/decisions/stop", post(decisions_stop))
        .route("/decisions/logs", get(decisions_logs))
        .route("/video/status", get(video_status))
        .route("/video/start", post(video_start))
        .route("/video/stop", post(video_stop))
        .route("/video/logs", get(video_logs))
        .route("/ocr/launch", post(ocr_launch))
        .route("/voice/launch", post(voice_launch))
        .route("/asr/launch", post(asr_launch))
        .route("/image/launch", post(image_launch));
    // the container sidecars, one set of routes each (no other name is served)
    for kind in ["ocr", "voice", "asr", "image"] {
        router = router
            .route(&format!("/{kind}/status"), get(move || container_status_route(kind)))
            .route(&format!("/{kind}/start"), post(move || container_action_route(kind, "start")))
            .route(&format!("/{kind}/stop"), post(move || container_action_route(kind, "stop")))
            .route(&format!("/{kind}/logs"), get(move || sidecar_logs_route(kind)));
    }
    let router = router
        .fallback(not_found)
        .layer(middleware::from_fn_with_state(app.clone(), auth))
        .layer(axum::Extension(prev))
        .with_state(app.clone());
    let listener = tokio::net::TcpListener::bind("0.0.0.0:8001").await.expect("bind :8001");
    axum::serve(listener, router).await.expect("serve");
}
