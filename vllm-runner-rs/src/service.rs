//! The runner's service (runner.py): the launches, the stops, the watchdog that
//! resumes the last model, the side services through the scoped sudo, and the
//! metrics. Operations are serialised by `ops` (runner.py's `_lock` around the
//! process lifecycle); the state itself is in the supervisor, under its own lock,
//! so the reader threads never wait on a launch.
use crate::build::{build_cmd, Paths};
use crate::flags;
use crate::launch_env;
use crate::state;
use crate::supervisor::{kill, Proc, Supervisor};
use crate::validate::validate_args;
use regex::Regex;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::Duration;

/// The settings runner.py reads from its environment, with its defaults.
#[derive(Clone, Debug)]
pub struct Config {
    pub paths: Paths,
    pub runner_token: String,
    pub log_dir: PathBuf,
    pub state_file: PathBuf,
    pub resume_file: PathBuf,
    pub min_free_gib: f64,
    pub critical_free_gib: f64,
    pub stable_seconds: u64,
    pub max_auto_retries: i64,
    pub hf_home: String,
    pub sudo: bool,
}

impl Config {
    pub fn from_env() -> Result<Config, String> {
        let env = |k: &str, d: &str| std::env::var(k).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| d.to_string());
        let token = std::env::var("RUNNER_TOKEN").map_err(|_| "RUNNER_TOKEN absent ou trop court (16 caracteres minimum)".to_string())?;
        if token.chars().count() < 16 {
            return Err("RUNNER_TOKEN absent ou trop court (16 caracteres minimum)".into());
        }
        let home = env("HOME", "/var/lib/vllm-runner");
        Ok(Config {
            paths: Paths::from_env(),
            runner_token: token,
            log_dir: PathBuf::from(env("RUNNER_LOG_DIR", "/var/lib/vllm-runner/logs")),
            state_file: PathBuf::from(format!("{home}/last_model.json")),
            resume_file: PathBuf::from(format!("{home}/resume_failures")),
            min_free_gib: env("MIN_FREE_TO_LAUNCH_GIB", "30").parse().unwrap_or(30.0),
            critical_free_gib: env("CRITICAL_FREE_GIB", "2").parse().unwrap_or(2.0),
            stable_seconds: env("RESUME_STABLE_SECONDS", "600").parse().unwrap_or(600),
            max_auto_retries: 3,
            hf_home: env("HF_HOME", "/root/.cache/huggingface"),
            sudo: true,
        })
    }
}

pub struct App {
    pub cfg: Config,
    pub sup: Arc<Supervisor>,
    /// runner.py's `_lock`, around the process lifecycle.
    pub ops: Mutex<()>,
    pub log_stamp_clock: fn() -> String,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// The `_ENGINE_BIN` table: the binary each engine needs (vLLM is always there).
pub fn engine_bin(cfg: &Config, engine: &str) -> Option<String> {
    match engine {
        "vllm" => Some(cfg.paths.vllm_bin.clone()),
        "llamacpp" => Some(cfg.paths.llama_bin.clone()),
        "ds4" => Some(cfg.paths.ds4_bin.clone()),
        "exllamav3" => Some(format!("{}/main.py", cfg.paths.tabby_dir)),
        "vllm-docker" => Some("/usr/bin/docker".to_string()),
        _ => None,
    }
}

/// The binary of a `--vllm-*` / `--llama-*` pseudo-flag: its env var, or its default.
pub fn bin_flag_path(flag: &str) -> Option<String> {
    flags::BIN_FLAG_PATHS.iter().find(|(f, _, _)| *f == flag).map(|(_, var, default)| std::env::var(var).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string()))
}

/// Python's repr of a dict of strings, for the journal line.
pub fn py_dict_repr(items: &BTreeMap<String, String>) -> String {
    let parts: Vec<String> = items.iter().map(|(k, v)| format!("'{k}': '{v}'")).collect();
    format!("{{{}}}", parts.join(", "))
}

fn regex(p: &str) -> Regex {
    Regex::new(p).expect("static regex")
}

fn hf_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| regex(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,80}$"))
}
fn local_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| regex(r"^local:[A-Za-z0-9][A-Za-z0-9._-]{0,60}$"))
}
fn abs_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| regex(r"^/[A-Za-z0-9._/@+-]+(/[A-Za-z0-9._/@+-]+)*$"))
}

/// `_HF_ID_RE` / `_LOCAL_ID_RE` / `_ABS_PATH_RE`: the shape of a model id.
pub fn model_id_shape_ok(id: &str) -> bool {
    hf_re().is_match(id) || local_re().is_match(id) || abs_re().is_match(id)
}

impl App {
    /// `_append`, for the callers outside the reader threads.
    pub fn log(&self, line: &str) {
        self.sup.lock().logs.append(line);
    }

    /// `_start_process`: the command is built BEFORE the running model is stopped,
    /// so a refusal leaves the service as it is. Holds `ops`.
    pub fn start_process(&self, hf_id: &str, name: &str, extra: &[String], engine: &str) -> Result<Arc<Proc>, String> {
        let original: Vec<String> = extra.to_vec();
        let mut tokens: Vec<String> = extra.to_vec();
        let mut model_env: BTreeMap<String, String> = BTreeMap::new();
        for (flag, var, val) in flags::ENV_FLAG_VARS {
            if tokens.iter().any(|t| t == flag) {
                tokens.retain(|t| t != flag);
                model_env.insert(var.to_string(), val.to_string());
            }
        }
        let mut bin_override: Option<String> = None;
        for (flag, _, _) in flags::BIN_FLAG_PATHS {
            if tokens.iter().any(|t| t == flag) {
                tokens.retain(|t| t != flag);
                bin_override = bin_flag_path(flag);
            }
        }
        let cmd = build_cmd(&self.cfg.paths, hf_id, name, &tokens, engine, bin_override.as_deref()).map_err(|e| e.message().to_string())?;

        let previous = self.sup.lock().proc.clone();
        let killed = previous.as_ref().is_some_and(|p| p.alive());
        if let Some(p) = previous.filter(|p| p.alive()) {
            self.log("[runner] Stopping previous model…");
            kill(&p);
        }
        {
            let mut s = self.sup.lock();
            s.logs.clear();
            s.model = Some(name.to_string());
            s.engine = Some(engine.to_string());
            s.status = "starting".into();
        }
        let stamp = (self.log_stamp_clock)();
        let mut run_log = state::open_run_log(&self.cfg.log_dir, name, engine, &stamp);
        if let Some(j) = run_log.as_mut() {
            j.write(&format!("[runner] {} start {engine} — {name} ({hf_id})", iso_now()));
        }
        if killed {
            self.wait_mem_release_logged();
        }
        let shown = cmd.join(" ");
        self.log(&format!("[runner] ({engine}) $ {shown}"));
        if let Some(j) = run_log.as_mut() {
            j.write(&format!("$ {shown}"));
        }
        if !model_env.is_empty() {
            let line = format!("[runner] model-specific env: {}", py_dict_repr(&model_env));
            self.log(&line);
            if let Some(j) = run_log.as_mut() {
                j.write(&line);
            }
        }
        let env = launch_env::build(engine, &model_env, &self.cfg.hf_home, &|k| std::env::var(k).ok());
        let cwd = if engine == "exllamav3" { Some(self.cfg.paths.tabby_dir.as_str()) } else { None };
        let proc = self.sup.spawn(&cmd, &model_env, &env, cwd, run_log).map_err(|e| e.to_string())?;
        if let Err(e) = state::save_last_launch(&self.cfg.state_file, hf_id, name, &original, engine) {
            self.log(&format!("[runner] could not save state for auto-resume: {e}"));
        }
        Ok(proc)
    }

    /// `_wait_mem_release`, with its journal line.
    pub fn wait_mem_release_logged(&self) {
        let (waited, free) = state::wait_mem_release(state::mem_available_gib, thread::sleep, Duration::from_secs(60), Duration::from_millis(1500), Duration::from_secs_f64(4.0));
        let free_text = free.map(|f| format!("{f:.1}")).unwrap_or_else(|| "None".into());
        self.log(&format!("[runner] Memory released: {free_text} GiB free (waited {:.0}s) — relaunching the model", waited.as_secs_f64()));
    }

    /// `_launch_memory_ok`: wait for the memory, then refuse under the floor.
    pub fn launch_memory_ok(&self, context: &str) -> bool {
        self.wait_mem_release_logged();
        let free = state::mem_available_gib();
        if let Some(f) = free.filter(|f| *f < self.cfg.min_free_gib) {
            self.log(&format!(
                "[runner] {context} REFUSED: only {f:.1} GiB available (floor {:.0}). A previous model's memory has not been released — launching now would crash the machine.",
                self.cfg.min_free_gib
            ));
            return false;
        }
        true
    }

    /// `/stop`: a deliberate stop forgets the launch.
    pub fn stop(&self) -> &'static str {
        state::clear_last_launch(&self.cfg.state_file);
        let _ops = lock(&self.ops);
        let proc = self.sup.lock().proc.clone();
        if let Some(p) = proc.filter(|p| p.alive()) {
            self.log("[runner] Stop requested.");
            kill(&p);
            let mut s = self.sup.lock();
            s.status = "stopped".into();
            s.model = None;
            return "stopped";
        }
        "already_stopped"
    }

    /// `/launch`: the request checks, then the start, under `ops`.
    pub fn launch(&self, data: &serde_json::Value) -> Result<(u16, serde_json::Value), (u16, serde_json::Value)> {
        let s = |k: &str| data.get(k).and_then(|v| v.as_str()).map(str::to_string);
        let hf_id = s("hf_model_id").unwrap_or_default().trim().to_string();
        let name = s("model_name").unwrap_or_else(|| hf_id.clone()).trim().to_string();
        let extra = s("vllm_args").unwrap_or_default().trim().to_string();
        let engine = data.get("engine").and_then(|v| v.as_str()).filter(|e| !e.is_empty()).unwrap_or("vllm").trim().to_lowercase();
        let err = |code: u16, msg: serde_json::Value| Err((code, msg));
        if hf_id.is_empty() {
            return err(400, serde_json::json!({"error": "hf_model_id required"}));
        }
        if !model_id_shape_ok(&hf_id) {
            return err(400, serde_json::json!({"error": "invalid hf_model_id"}));
        }
        if hf_id.split('/').any(|p| p == "..") {
            return err(400, serde_json::json!({"error": "invalid hf_model_id (.. refused)"}));
        }
        if !["vllm", "llamacpp", "ds4", "exllamav3", "vllm-docker"].contains(&engine.as_str()) {
            return err(400, serde_json::json!({"error": format!("unknown engine: {engine}")}));
        }
        if engine != "vllm" && !engine_bin(&self.cfg, &engine).is_some_and(|b| std::path::Path::new(&b).exists()) {
            return err(400, serde_json::json!({"error": format!("engine {engine} not installed on this machine")}));
        }
        let tokens = match validate_args(&extra, &engine, &hf_id) {
            Ok(t) => t,
            Err(e) => return err(400, serde_json::json!({"error": e})),
        };
        let _ops = lock(&self.ops);
        match self.start_process(&hf_id, &name, &tokens, &engine) {
            Ok(proc) => {
                self.sup.lock().auto_retries = 0;
                Ok((200, serde_json::json!({"status": "starting", "model": name, "engine": engine, "pid": proc.pid})))
            }
            Err(e) => err(500, serde_json::json!({"error": e})),
        }
    }
}

/// Seconds-precision UTC timestamp for the journal (`%Y-%m-%dT%H:%M:%S`).
pub fn iso_now() -> String {
    chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string()
}

/// `time.strftime('%Y%m%d-%H%M%S')` for the journal's file name.
pub fn file_stamp() -> String {
    chrono::Local::now().format("%Y%m%d-%H%M%S").to_string()
}

/// `_watchdog`: resume the last model when it stopped on its own, with the
/// memory wait, the floor and the persisted failure counter; a model that holds
/// `stable_seconds` resets the counter.
pub fn watchdog(app: Arc<App>) {
    let mut disabled_announced = false;
    loop {
        thread::sleep(Duration::from_secs(10));
        {
            let s = app.sup.lock();
            let stable = s.status == "running" && s.running_since.is_some_and(|t| t.elapsed() >= Duration::from_secs(app.cfg.stable_seconds));
            drop(s);
            if stable && state::read_resume_fails(&app.cfg.resume_file) != 0 {
                state::write_resume_fails(&app.cfg.resume_file, 0);
                app.sup.lock().auto_retries = 0;
                disabled_announced = false;
                app.log("[runner] model stable — auto-resume failure counter reset");
            }
        }
        let Some(last) = state::load_last_launch(&app.cfg.state_file) else { continue };
        {
            let s = app.sup.lock();
            if s.proc.as_ref().is_some_and(|p| p.alive()) || s.status == "starting" {
                continue;
            }
        }
        let fails = state::read_resume_fails(&app.cfg.resume_file);
        if fails >= app.cfg.max_auto_retries {
            if !disabled_announced {
                app.log(&format!("[runner] auto-resume DISABLED after {fails} consecutive failures — relaunch manually from Admin."));
                disabled_announced = true;
            }
            continue;
        }
        let eng = last.get("engine").and_then(|v| v.as_str()).unwrap_or("vllm").to_string();
        let args = last.get("vllm_args").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let hf = last.get("hf_model_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let tokens = match validate_args(&args, &eng, &hf) {
            Ok(t) => t,
            Err(e) => {
                app.log(&format!("[runner] auto-resume impossible, invalid args: {e}"));
                state::write_resume_fails(&app.cfg.resume_file, app.cfg.max_auto_retries);
                continue;
            }
        };
        // the memory wait and the floor happen outside the lifecycle lock
        if !app.launch_memory_ok("auto-resume") {
            state::write_resume_fails(&app.cfg.resume_file, fails + 1);
            continue;
        }
        let _ops = lock(&app.ops);
        if app.sup.lock().proc.as_ref().is_some_and(|p| p.alive()) {
            continue;
        }
        state::write_resume_fails(&app.cfg.resume_file, fails + 1);
        app.sup.lock().auto_retries = fails + 1;
        let attempt = format!("[runner] model stopped unexpectedly — auto-resume (attempt {}/{})…", fails + 1, app.cfg.max_auto_retries);
        let model_name = last.get("model_name").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let _ = app.start_process(&hf, &model_name, &tokens, &eng);
        app.log(&attempt);
    }
}

/// `_load_last_launch` at start: the watchdog takes the resume from there.
pub fn announce_start(app: &App) {
    if state::load_last_launch(&app.cfg.state_file).is_some() {
        app.log("[runner] service started — the watchdog will resume the last model once memory is confirmed free");
    }
}

