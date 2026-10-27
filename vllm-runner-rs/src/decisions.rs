//! The decision model (Cloudflare Clef, GGUF): a SECOND `llama-server`, on its
//! own port, serving `/v1/systemone` and nothing else.
//!
//! Why it cannot be the chat model:
//!  - a decision model has NO chat endpoint at all. llama.cpp answers it on
//!    `/v1/systemone` only (measured against the model card and llama-server's
//!    README), so it cannot replace the chat model on :8000;
//!  - the chat model owns most of the 121.6 GiB of unified memory. Clef in
//!    Q4_K_M is 17.65 GB: it fits NEXT TO the chat model or it does not start,
//!    never instead of it.
//!
//! Same guards as the chat model, because the failure mode is the same: the
//! kernel's OOM killer cannot see GPU memory, so the memory floor is checked
//! before the spawn (a refusal, never a swap) and the runtime guard kills OUR
//! process under `CRITICAL_FREE_GIB`.

use crate::service::Config;
use crate::state::RunLog;
use crate::supervisor::{kill, Supervisor};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

/// The port its own `llama-server` listens on. 8000 is the chat model's, 8001
/// the runner's: this one is never published.
pub const DEFAULT_PORT: u16 = 8002;
/// The public name used by LiteLLM and the page: explicit, and no collision
/// with `auto-model`.
pub const DEFAULT_NAME: &str = "clef-q4km";
/// A Clef prompt is a state plus a schema of questions, not a conversation.
/// llama.cpp evaluates the whole prompt in ONE batch (`--ubatch-size`), so a
/// large context buys nothing and costs memory: 8k is ample, and the batch is
/// sized so a long state still fits.
pub const DEFAULT_ARGS: &str = "--ctx-size 8192 --ubatch-size 2048 --n-gpu-layers 999 --flash-attn on";

/// Where the decision service listens, what it loads, and how.
#[derive(Clone, Debug)]
pub struct DecisionsCfg {
    /// `local:<slug>` under MODELS_DIR, or an HF repository id (resolved by
    /// `build::resolve_gguf`, the same resolver the chat models use).
    pub hf_id: String,
    pub name: String,
    pub port: u16,
    /// The engine arguments, already tokenised and validated by the caller.
    pub args: Vec<String>,
    pub bin: String,
    pub log_dir: PathBuf,
    pub hf_home: String,
    pub min_free_gib: f64,
}

impl DecisionsCfg {
    /// `DECISIONS_MODEL` empty (the default) leaves the service disabled: no
    /// memory, no port, and the page says so instead of failing.
    pub fn from_env(cfg: &Config) -> DecisionsCfg {
        let env = |k: &str, d: &str| std::env::var(k).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| d.to_string());
        DecisionsCfg {
            hf_id: env("DECISIONS_MODEL", ""),
            name: env("DECISIONS_NAME", DEFAULT_NAME),
            port: env("DECISIONS_PORT", &DEFAULT_PORT.to_string()).parse().unwrap_or(DEFAULT_PORT),
            args: shell_words(&env("DECISIONS_ARGS", DEFAULT_ARGS)),
            // The chat binary may be older than the model's architecture
            // (Clef needs llama.cpp b11430+): a dedicated variable lets the two
            // engines live side by side instead of forcing an upgrade of the
            // one serving chat.
            bin: env("LLAMA_BIN_DECISIONS", &cfg.paths.llama_bin),
            log_dir: cfg.log_dir.clone(),
            hf_home: cfg.hf_home.clone(),
            min_free_gib: cfg.min_free_gib,
        }
    }
}

/// A small `shlex.split`: the arguments come from one environment variable, so
/// quoted values must survive (`--chat-template-kwargs '{"a": 1}'`).
pub fn shell_words(raw: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut quote: Option<char> = None;
    let mut any = false;
    for c in raw.chars() {
        match quote {
            Some(q) if c == q => quote = None,
            Some(_) => {
                cur.push(c);
                any = true;
            }
            None if c == '\'' || c == '"' => {
                quote = Some(c);
                any = true;
            }
            None if c.is_whitespace() => {
                if any || !cur.is_empty() {
                    out.push(std::mem::take(&mut cur));
                    any = false;
                }
            }
            None => {
                cur.push(c);
                any = true;
            }
        }
    }
    if any || !cur.is_empty() {
        out.push(cur);
    }
    out
}

/// The decision service: one process, started and stopped explicitly.
pub struct Decisions {
    pub cfg: DecisionsCfg,
    sup: Arc<Supervisor>,
    /// Serialises start/stop, like the chat model's `ops` lock.
    ops: Mutex<()>,
}

impl Decisions {
    pub fn new(cfg: DecisionsCfg) -> Decisions {
        Decisions {
            sup: Arc::new(Supervisor::with_probe(&format!("127.0.0.1:{}", cfg.port))),
            cfg,
            ops: Mutex::new(()),
        }
    }

    pub fn enabled(&self) -> bool {
        !self.cfg.hf_id.is_empty()
    }

    fn state(&self) -> MutexGuard<'_, crate::supervisor::Shared> {
        self.sup.lock()
    }

    /// `GET /decisions/status`: what the page shows. Never a guess: `status`
    /// comes from the process and its readiness probe, not from a flag.
    pub fn status(&self) -> Value {
        let s = self.state();
        json!({
            "enabled": self.enabled(),
            "name": self.cfg.name,
            // What the catalog shows: the weights reference and the arguments
            // actually used (they come from the environment, not from a form).
            "ref": self.cfg.hf_id,
            "args": self.cfg.args.join(" "),
            "port": self.cfg.port,
            "status": s.status,
            "pid": s.proc.as_ref().map(|p| p.pid),
            "uptime_s": s.running_since.map(|t| t.elapsed().as_secs()),
            "libre_gib": crate::state::mem_available_gib(),
            "endpoint": format!("http://127.0.0.1:{}/v1/systemone", self.cfg.port),
        })
    }

    /// `POST /decisions/start`: the memory floor first — a refusal leaves the
    /// box (and the chat model) exactly as it was.
    pub fn start(&self) -> Result<Value, (u16, Value)> {
        if !self.enabled() {
            return Err((400, json!({"error": "Service de décision non configuré (DECISIONS_MODEL vide)."})));
        }
        let _ops = self.ops.lock().unwrap_or_else(|e| e.into_inner());
        {
            let s = self.state();
            if s.proc.as_ref().is_some_and(|p| p.alive()) {
                return Ok(json!({"ok": true, "status": s.status.clone(), "already": true}));
            }
        }
        let free = crate::state::mem_available_gib().unwrap_or(0.0);
        if free < self.cfg.min_free_gib {
            return Err((
                409,
                json!({
                    "error": format!(
                        "Mémoire insuffisante pour lancer le modèle de décision : {free:.1} GiB disponibles, plancher {:.0} GiB. Le modèle de chat occupe le reste — arrête un service annexe plutôt que de le remplacer.",
                        self.cfg.min_free_gib
                    ),
                    "libre_gib": free,
                    "floor_gib": self.cfg.min_free_gib,
                }),
            ));
        }
        if !Path::new(&self.cfg.bin).exists() {
            return Err((400, json!({"error": format!("Binaire llama.cpp introuvable : {}", self.cfg.bin)})));
        }
        let gguf = crate::build::resolve_gguf(&self.cfg.paths(), &self.cfg.hf_id).map_err(|e| (400, json!({"error": e.message()})))?;

        let mut cmd: Vec<String> = vec![
            self.cfg.bin.clone(),
            "-m".into(),
            gguf.clone(),
            "--host".into(),
            "0.0.0.0".into(),
            "--port".into(),
            self.cfg.port.to_string(),
            "--alias".into(),
            self.cfg.name.clone(),
            "--metrics".into(),
        ];
        cmd.extend(self.cfg.args.iter().cloned());

        let stamp = crate::service::file_stamp();
        let mut run_log = crate::state::open_run_log(&self.cfg.log_dir, &self.cfg.name, "decisions", &stamp);
        if let Some(j) = run_log.as_mut() {
            j.write(&format!("[runner] {} start decisions — {} ({})", crate::service::iso_now(), self.cfg.name, self.cfg.hf_id));
            j.write(&format!("$ {}", cmd.join(" ")));
        }
        {
            let mut s = self.state();
            s.logs.clear();
            s.model = Some(self.cfg.name.clone());
            s.engine = Some("decisions".into());
            s.status = "starting".into();
        }
        let env = crate::launch_env::build("llamacpp", &Default::default(), &self.cfg.hf_home, &|k| std::env::var(k).ok());
        self.sup.spawn(&cmd, &Default::default(), &env, None, run_log).map_err(|e| (500, json!({"error": format!("Lancement impossible : {e}")})))?;
        Ok(json!({"ok": true, "status": "starting", "port": self.cfg.port, "model": self.cfg.name}))
    }

    /// `POST /decisions/stop`: only our own process; the chat model is never
    /// touched by this path.
    pub fn stop(&self) -> Result<Value, (u16, Value)> {
        let _ops = self.ops.lock().unwrap_or_else(|e| e.into_inner());
        let proc = {
            let mut s = self.state();
            s.status = "stopped".into();
            s.running_since = None;
            s.proc.take()
        };
        match proc {
            Some(p) if p.alive() => {
                kill(&p);
                Ok(json!({"ok": true, "status": "stopped"}))
            }
            _ => Ok(json!({"ok": true, "status": "stopped", "already": true})),
        }
    }

    /// `GET /decisions/logs`: the ring buffer, plus the process state.
    pub fn logs(&self, tail: usize) -> Value {
        let s = self.state();
        json!({"status": s.status, "logs": s.logs.tail(tail)})
    }
}

impl DecisionsCfg {
    /// The locations `resolve_gguf` needs; the decision model lives in the same
    /// MODELS_DIR / HF cache as the chat models.
    fn paths(&self) -> crate::build::Paths {
        crate::build::Paths::from_env()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shell_words_keeps_quoted_values() {
        assert_eq!(shell_words("--ctx-size 8192 --flash-attn on"), vec!["--ctx-size", "8192", "--flash-attn", "on"]);
        assert_eq!(
            shell_words("--chat-template-kwargs '{\"enable_thinking\": false}'"),
            vec!["--chat-template-kwargs", "{\"enable_thinking\": false}"]
        );
        assert_eq!(shell_words("  "), Vec::<String>::new());
    }

    #[test]
    fn a_disabled_service_refuses_to_start() {
        let cfg = DecisionsCfg {
            hf_id: String::new(),
            name: DEFAULT_NAME.into(),
            port: DEFAULT_PORT,
            args: vec![],
            bin: "/bin/false".into(),
            log_dir: std::env::temp_dir(),
            hf_home: String::new(),
            min_free_gib: 30.0,
        };
        let d = Decisions::new(cfg);
        assert!(!d.enabled());
        let (code, body) = d.start().unwrap_err();
        assert_eq!(code, 400);
        assert!(body["error"].as_str().unwrap().contains("DECISIONS_MODEL"));
    }

    #[test]
    fn status_names_the_port_and_the_endpoint() {
        let cfg = DecisionsCfg {
            hf_id: "local:clef".into(),
            name: "clef-q4km".into(),
            port: 8123,
            args: vec![],
            bin: "/bin/false".into(),
            log_dir: std::env::temp_dir(),
            hf_home: String::new(),
            min_free_gib: 30.0,
        };
        let d = Decisions::new(cfg);
        let s = d.status();
        assert_eq!(s["enabled"], true);
        assert_eq!(s["port"], 8123);
        assert_eq!(s["status"], "stopped");
        assert!(s["endpoint"].as_str().unwrap().ends_with(":8123/v1/systemone"));
    }

    #[test]
    fn stopping_an_idle_service_is_not_an_error() {
        let cfg = DecisionsCfg {
            hf_id: "local:clef".into(),
            name: "clef-q4km".into(),
            port: 8124,
            args: vec![],
            bin: "/bin/false".into(),
            log_dir: std::env::temp_dir(),
            hf_home: String::new(),
            min_free_gib: 30.0,
        };
        let d = Decisions::new(cfg);
        let v = d.stop().unwrap();
        assert_eq!(v["status"], "stopped");
        assert_eq!(v["already"], true);
    }
}
