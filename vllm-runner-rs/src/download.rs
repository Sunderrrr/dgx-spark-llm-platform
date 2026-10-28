//! Downloading a model's weights from Hugging Face, on demand, from the console.
//!
//! Why here and not in the portal: this process already owns the paths
//! (`MODELS_DIR`, `HF_HOME`) and the token (`HF_TOKEN` from the platform's
//! `.env`, which the runner's unit loads), and it runs on the host where the
//! weights must land. The portal is a container with neither.
//!
//! One job at a time, deliberately: these are tens of gigabytes on a box that
//! is also serving a model, and two parallel downloads would fight for the same
//! disk and bandwidth. A second start is refused with the running one's repo.
//!
//! Progress is measured, not parsed: `du -sb` on the destination and the
//! repository's total size from the HF API. Parsing the CLI's progress bars
//! would break at the next huggingface_hub release.
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::SystemTime;
use std::time::UNIX_EPOCH;

use serde_json::{json, Value};

use crate::build::Paths;

/// Where the weights go. `local` mirrors `resolve_gguf`'s `local:<slug>`
/// convention (a directory under MODELS_DIR), `cache` is the HF cache that a
/// bare `repo/id` resolves from.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Target {
    Cache,
    Local,
}

impl Target {
    pub fn as_str(self) -> &'static str {
        match self {
            Target::Cache => "cache",
            Target::Local => "local",
        }
    }
}

struct Job {
    repo: String,
    target: Target,
    dest: PathBuf,
    started_s: f64,
    log: PathBuf,
    error: Option<String>,
    running: bool,
}

static JOB: OnceLock<Mutex<Option<Job>>> = OnceLock::new();

fn slot() -> &'static Mutex<Option<Job>> {
    JOB.get_or_init(|| Mutex::new(None))
}

fn now_s() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `safe_name` as `build.rs` builds it: the slug of a `local:` id.
fn slug(raw: &str) -> String {
    raw.chars().map(|c| if c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-' { c } else { '-' }).collect()
}

/// A Hugging Face repo id: `owner/name`, nothing that can escape a path.
pub fn valid_repo(repo: &str) -> bool {
    if repo.len() < 3 || repo.len() > 160 || repo.starts_with('/') || repo.ends_with('/') {
        return false;
    }
    if repo.contains("..") || repo.contains('\\') || repo.split('/').count() != 2 {
        return false;
    }
    repo.split('/').all(|part| !part.is_empty() && part.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')))
}

fn du_bytes(p: &Path) -> u64 {
    let mut total = 0u64;
    let Ok(entries) = std::fs::read_dir(p) else { return 0 };
    for e in entries.flatten() {
        let path = e.path();
        let Ok(meta) = std::fs::symlink_metadata(&path) else { continue };
        if meta.is_dir() {
            total += du_bytes(&path);
        } else if meta.is_file() {
            total += meta.len();
        }
    }
    total
}

/// The repository's total size, from the public API (`?blobs=true` carries the
/// per-file sizes). None when the answer does not carry them: a bar without a
/// denominator is better than a wrong denominator.
///
/// `curl` rather than an HTTP crate: the runner has no client dependency and
/// this is one GET every few seconds. The token goes through curl's stdin
/// config, never argv — `ps` is readable by every account on the box.
fn repo_total(repo: &str, token: Option<&str>) -> Option<u64> {
    let url = format!("https://huggingface.co/api/models/{repo}?blobs=true");
    let mut cmd = std::process::Command::new("curl");
    cmd.args(["-sS", "-m", "15", "--config", "-", &url])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null());
    let mut child = cmd.spawn().ok()?;
    if let (Some(t), Some(mut stdin)) = (token, child.stdin.take()) {
        let _ = stdin.write_all(format!("header = \"Authorization: Bearer {t}\"\n").as_bytes());
    }
    let out = child.wait_with_output().ok()?;
    if !out.status.success() {
        return None;
    }
    let body: Value = serde_json::from_slice(&out.stdout).ok()?;
    let mut total = 0u64;
    for s in body.get("siblings")?.as_array()? {
        total += s.get("size").and_then(Value::as_u64).unwrap_or(0);
    }
    (total > 0).then_some(total)
}

/// Starts a download. Returns the refusal message when one is already running.
pub fn start(paths: &Paths, log_dir: &Path, repo: &str, target: Target, hf_cli: &str) -> Result<Value, String> {
    if !valid_repo(repo) {
        return Err(format!("« {repo} » is not a Hugging Face repo id (owner/name expected)."));
    }
    {
        let guard = slot().lock().unwrap_or_else(|e| e.into_inner());
        if let Some(j) = guard.as_ref().filter(|j| j.running) {
            return Err(format!("A download is already running: {}.", j.repo));
        }
    }
    // `local:` lands under MODELS_DIR, which belongs to root: the daemon cannot
    // write there. Rather than fail mid-download, say so — new models use the
    // cache (a bare `owner/name` in the catalog), which is writable and is what
    // the launcher resolves too.
    if target == Target::Local {
        return Err(format!(
            "MODELS_DIR ({}) belongs to root: a « local: » model cannot be fetched from the console. \
             Use the repository id (owner/name) instead — the weights go to the HF cache and the \
             launch finds them.",
            paths.models_dir
        ));
    }
    let dest = PathBuf::from(&paths.hf_home).join("hub");
    if let Err(e) = std::fs::create_dir_all(&dest) {
        return Err(format!("cannot create {}: {e}", dest.display()));
    }
    // MODELS_DIR belongs to root: the daemon runs as vllmrunner, so a log
    // written next to the weights is a permission error, not a download.
    let log = log_dir.join(format!("download-{}.log", slug(repo)));
    let mut cmd = vec![hf_cli.to_string(), "download".to_string(), repo.to_string(), "--quiet".to_string()];
    if target == Target::Local {
        cmd.push("--local-dir".to_string());
        cmd.push(dest.display().to_string());
    }
    let log_for_child = log.clone();
    let repo_s = repo.to_string();
    let job = Job { repo: repo_s.clone(), target, dest: dest.clone(), started_s: now_s(), log: log.clone(), error: None, running: true };
    // The token comes from the runner's own environment (the unit loads the
    // platform's .env), never from the request: the console must not be able to
    // choose which credential downloads. It also stays OUT of argv — `ps` is
    // readable by every account on this box — and travels through the child's
    // environment, which only it can read.
    let answer = json!({"ok": true, "repo": repo, "target": target.as_str(), "dest": dest.display().to_string()});
    *slot().lock().unwrap_or_else(|e| e.into_inner()) = Some(job);
    std::thread::spawn(move || {
        let outcome = run(&cmd, &log_for_child);
        let mut guard = slot().lock().unwrap_or_else(|e| e.into_inner());
        if let Some(j) = guard.as_mut() {
            if j.repo == repo_s {
                j.running = false;
                j.error = outcome.err();
            }
        }
    });
    Ok(answer)
}

fn run(cmd: &[String], log: &Path) -> Result<(), String> {
    let mut file = std::fs::File::create(log).map_err(|e| format!("cannot write the log: {e}"))?;
    let _ = writeln!(file, "$ {}", cmd.join(" "));
    let mut proc = std::process::Command::new(&cmd[0]);
    if let Some(t) = std::env::var("HF_TOKEN").ok().filter(|t| !t.is_empty()) {
        proc.env("HF_TOKEN", t);
    }
    let child = proc
        .args(&cmd[1..])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn();
    let child = child.map_err(|e| format!("cannot start {}: {e}", cmd[0]))?;
    let out = child.wait_with_output().map_err(|e| format!("download failed: {e}"))?;
    let _ = file.write_all(&out.stdout);
    let _ = file.write_all(&out.stderr);
    if out.status.success() {
        Ok(())
    } else {
        let tail = String::from_utf8_lossy(&out.stderr);
        Err(tail.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("download failed").trim().chars().take(300).collect())
    }
}

/// The current job's state, with measured progress.
pub fn status() -> Value {
    let guard = slot().lock().unwrap_or_else(|e| e.into_inner());
    let Some(j) = guard.as_ref() else {
        return json!({"running": false, "repo": null, "target": null, "bytes": 0, "total": null, "done": false});
    };
    let dest = match j.target {
        Target::Local => j.dest.clone(),
        // The cache destination is per-repo inside `hub/`: measuring all of it
        // would add every model ever downloaded to this one's bar.
        Target::Cache => j.dest.join(format!("models--{}", j.repo.replace('/', "--"))),
    };
    let bytes = du_bytes(&dest);
    let total = repo_total(&j.repo, std::env::var("HF_TOKEN").ok().as_deref());
    json!({
        "running": j.running,
        "repo": j.repo,
        "target": j.target.as_str(),
        "bytes": bytes,
        "total": total,
        "done": !j.running && j.error.is_none(),
        "error": j.error,
        "elapsed_s": (now_s() - j.started_s).round() as i64,
        "log": j.log.display().to_string(),
    })
}

/// The log's last lines, for the console's "why did it stop" question.
pub fn log_tail(path: &Path) -> String {
    let Ok(text) = std::fs::read_to_string(path) else { return String::new() };
    let lines: Vec<&str> = text.lines().rev().take(12).collect();
    lines.into_iter().rev().collect::<Vec<_>>().join("\n")
}

/// True while a job exists and has not finished.
pub fn busy() -> bool {
    slot().lock().unwrap_or_else(|e| e.into_inner()).as_ref().is_some_and(|j| j.running)
}

/// Forgets the finished job (the console's "dismiss"): the log stays on disk.
pub fn clear() {
    let mut guard = slot().lock().unwrap_or_else(|e| e.into_inner());
    if guard.as_ref().is_some_and(|j| !j.running) {
        *guard = None;
    }
}

/// How long a download may take before the API calls it stuck: generous by
/// design (a 90 GiB model on a slow link), only used for the console's wording.
pub const STALE_HINT_S: i64 = 6 * 3600;

/// True when the job looks stuck (no new bytes for a long time is the console's
/// call to make; this only reports the elapsed time past the hint).
pub fn stale(elapsed_s: i64) -> bool {
    elapsed_s > STALE_HINT_S
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_real_repo_ids_are_accepted() {
        assert!(valid_repo("unsloth/Qwen3-8B-GGUF"));
        assert!(valid_repo("black-forest-labs/FLUX.2-klein-4B"));
        assert!(!valid_repo("Qwen3-8B"));
        assert!(!valid_repo("../etc/passwd"));
        assert!(!valid_repo("owner/../../x"));
        assert!(!valid_repo("owner/name/extra"));
        assert!(!valid_repo(""));
        assert!(!valid_repo("owner/na me"));
    }

    #[test]
    fn the_slug_matches_the_one_the_launcher_resolves() {
        assert_eq!(slug("Qwen3-8B-GGUF"), "Qwen3-8B-GGUF");
        assert_eq!(slug("Qwen 3/x.y"), "Qwen-3-x.y");
    }
}
