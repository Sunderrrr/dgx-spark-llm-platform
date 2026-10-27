//! The process lifecycle of runner.py: the engine process in its own session,
//! its output read into the log buffer and the journal, the readiness detection
//! (the startup line, and a live probe of :8000), the memory guard that stops
//! our own process before the kernel's OOM would stop everything, and the stop
//! that takes the whole process group down (SIGTERM, 30 s, then SIGKILL).
use crate::state::{LogBuffer, RunLog};
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

/// One engine process: its id, its generation (which launch it belongs to), and its end.
pub struct Proc {
    pub pid: u32,
    pub gen: u64,
    exited: AtomicBool,
    code: Mutex<Option<i32>>,
}

impl Proc {
    /// `proc.poll() is None`
    pub fn alive(&self) -> bool {
        !self.exited.load(Ordering::SeqCst)
    }
    pub fn code(&self) -> Option<i32> {
        *self.code.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// The shared state under `_lock`.
pub struct Shared {
    pub logs: LogBuffer,
    pub proc: Option<Arc<Proc>>,
    pub model: Option<String>,
    pub engine: Option<String>,
    pub status: String,
    pub auto_retries: i64,
    pub running_since: Option<Instant>,
    pub generation: u64,
}

impl Default for Shared {
    fn default() -> Self {
        Shared { logs: LogBuffer::default(), proc: None, model: None, engine: None, status: "stopped".into(), auto_retries: 0, running_since: None, generation: 0 }
    }
}

pub struct Supervisor {
    pub inner: Mutex<Shared>,
    next_gen: AtomicU64,
    /// Where the readiness probe asks for /v1/models (127.0.0.1:8000 in service).
    pub probe_addr: String,
}

/// `_CRITICAL_FREE_GIB` default, `STABLE_SECONDS` default: see runner.py.
pub const CRITICAL_FREE_GIB: f64 = 2.0;

impl Default for Supervisor {
    fn default() -> Self {
        Supervisor { inner: Mutex::new(Shared::default()), next_gen: AtomicU64::new(1), probe_addr: "127.0.0.1:8000".into() }
    }
}

impl Supervisor {
    /// A supervisor whose readiness probe targets `addr`. The decision service
    /// is a second engine on its own port: same lifecycle, same guards, other
    /// socket. `next_gen` stays private, so this is the only way in.
    pub fn with_probe(addr: &str) -> Supervisor {
        Supervisor { probe_addr: addr.to_string(), ..Supervisor::default() }
    }

    pub fn lock(&self) -> MutexGuard<'_, Shared> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `_start_process`: spawn the command, then its readers. The caller holds
    /// the lock; the previous process must already be stopped.
    pub fn spawn(self: &Arc<Self>, cmd: &[String], model_env: &BTreeMap<String, String>, env: &BTreeMap<String, String>, cwd: Option<&str>, run_log: Option<RunLog>) -> std::io::Result<Arc<Proc>> {
        let (rd, wr) = std::io::pipe()?;
        let wr2 = wr.try_clone()?;
        let mut command = Command::new(&cmd[0]);
        command.args(&cmd[1..]).stdin(Stdio::null()).stdout(Stdio::from(wr)).stderr(Stdio::from(wr2)).env_clear().envs(env);
        if let Some(d) = cwd {
            command.current_dir(d);
        }
        // start_new_session=True: a session of its own, so its group can be killed as one.
        unsafe {
            command.pre_exec(|| {
                nix::unistd::setsid().map(|_| ()).map_err(std::io::Error::other)
            });
        }
        let child = command.spawn()?;
        let _ = model_env;
        let gen = self.next_gen.fetch_add(1, Ordering::SeqCst);
        let proc = Arc::new(Proc { pid: child.id(), gen, exited: AtomicBool::new(false), code: Mutex::new(None) });
        {
            // _start_process: 'starting' from the moment the process exists
            let mut s = self.lock();
            s.generation = gen;
            s.proc = Some(proc.clone());
            s.status = "starting".into();
        }
        let run_log = Arc::new(Mutex::new(run_log));
        {
            let this = self.clone();
            let p = proc.clone();
            let log = run_log.clone();
            thread::spawn(move || this.read_output(p, child, rd, log));
        }
        {
            let this = self.clone();
            let p = proc.clone();
            thread::spawn(move || this.health_watch(p));
        }
        {
            let this = self.clone();
            let p = proc.clone();
            thread::spawn(move || this.memory_guard(p));
        }
        Ok(proc)
    }

    /// `_reader`: the engine's output, line by line, into the buffer and the
    /// journal; the startup line marks it running. At its end the exit is recorded.
    fn read_output(self: Arc<Self>, proc: Arc<Proc>, mut child: std::process::Child, rd: std::io::PipeReader, log: Arc<Mutex<Option<RunLog>>>) {
        let reader = BufReader::new(rd);
        let mut failure: Option<String> = None;
        for raw in reader.lines() {
            match raw {
                Ok(line) => {
                    let line = line.trim_end().to_string();
                    {
                        let mut s = self.lock();
                        s.logs.append(&line);
                    }
                    if let Ok(mut l) = log.lock() {
                        if let Some(j) = l.as_mut() {
                            j.write(&line);
                        }
                    }
                    if line.contains("Application startup complete") {
                        let mut s = self.lock();
                        if s.proc.as_ref().is_some_and(|c| c.gen == proc.gen) {
                            s.status = "running".into();
                            s.running_since = Some(Instant::now());
                            s.auto_retries = 0;
                        }
                    }
                }
                Err(e) => {
                    failure = Some(e.to_string());
                    break;
                }
            }
        }
        if let Some(msg) = failure {
            let text = format!("[runner] read interrupted: {msg}");
            self.lock().logs.append(&text);
            if let Ok(mut l) = log.lock() {
                if let Some(j) = l.as_mut() {
                    j.write(&text);
                }
            }
        }
        let status = child.wait().ok();
        let code = status.and_then(|s| s.code().or_else(|| s.signal().map(|n| -n)));
        *proc.code.lock().unwrap_or_else(|e| e.into_inner()) = code;
        proc.exited.store(true, Ordering::SeqCst);
        let mut s = self.lock();
        if s.proc.as_ref().is_some_and(|c| c.gen == proc.gen) && s.status != "stopped" {
            s.status = if matches!(code, Some(0) | Some(-15) | Some(-9)) { "stopped".into() } else { "error".into() };
        }
        s.logs.append(&format!("[runner] Process exited (code {})", code.map(|c| c.to_string()).unwrap_or_else(|| "None".into())));
    }

    /// `_health_watch`: 'running' as soon as :8000 answers, whatever the logs say.
    fn health_watch(self: Arc<Self>, proc: Arc<Proc>) {
        while proc.alive() {
            if probe_models(&self.probe_addr) {
                let mut s = self.lock();
                if s.proc.as_ref().is_some_and(|c| c.gen == proc.gen) {
                    s.status = "running".into();
                    s.running_since = Some(Instant::now());
                    s.auto_retries = 0;
                }
                return;
            }
            thread::sleep(Duration::from_secs(3));
        }
    }

    /// `_memory_guard`: while OUR process runs, kill it before MemAvailable falls
    /// under the critical floor (the kernel's OOM cannot see GPU memory).
    fn memory_guard(self: Arc<Self>, proc: Arc<Proc>) {
        while proc.alive() {
            if let Some(free) = crate::state::mem_available_gib() {
                if free < CRITICAL_FREE_GIB {
                    self.lock().logs.append(&format!(
                        "[runner] CRITICAL MEMORY: {free:.1} GiB available — force-stopping the model to avoid a machine-wide OOM."
                    ));
                    kill_group(proc.pid, libc::SIGKILL);
                    return;
                }
            }
            thread::sleep(Duration::from_millis(500));
        }
    }
}

/// `urllib.request.urlopen(http://127.0.0.1:8000/v1/models, timeout=2)` answering 200.
pub fn probe_models(address: &str) -> bool {
    let Ok(addr) = address.parse() else { return false };
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_secs(2)) else { return false };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(2)));
    if s.write_all(b"GET /v1/models HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n").is_err() {
        return false;
    }
    let mut head = [0u8; 12];
    if s.read_exact(&mut head).is_err() {
        return false;
    }
    head.starts_with(b"HTTP/1.") && &head[9..12] == b"200"
}

/// `os.killpg(pgid, sig)`: the whole group of an engine (it is its own session).
pub fn kill_group(pid: u32, sig: i32) {
    unsafe {
        libc::killpg(pid as i32, sig);
    }
}

/// `_kill(proc)`: SIGTERM to the group, 30 s, then SIGKILL and 5 s more.
/// Returns once the process is gone (or the wait gave up).
pub fn kill(proc: &Proc) {
    if proc.alive() {
        kill_group(proc.pid, libc::SIGTERM);
    }
    if wait_exit(proc, Duration::from_secs(30)) {
        return;
    }
    kill_group(proc.pid, libc::SIGKILL);
    wait_exit(proc, Duration::from_secs(5));
}

fn wait_exit(proc: &Proc, timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if !proc.alive() {
            return true;
        }
        thread::sleep(Duration::from_millis(50));
    }
    !proc.alive()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cmd(script: &str) -> Vec<String> {
        vec!["/bin/sh".into(), "-c".into(), script.into()]
    }

    /// A probe that can never answer: the tests must not depend on the real
    /// engine that may serve :8000 on this machine.
    fn quiet() -> Arc<Supervisor> {
        Arc::new(Supervisor { probe_addr: "127.0.0.1:9".into(), ..Supervisor::default() })
    }

    fn env() -> BTreeMap<String, String> {
        BTreeMap::from([("PATH".into(), "/usr/bin:/bin".into())])
    }

    #[test]
    fn output_is_read_and_the_exit_recorded() {
        let sup = quiet();
        let proc = sup.lock().proc.clone();
        assert!(proc.is_none());
        let p = sup.spawn(&cmd("echo one; echo two; exit 3"), &BTreeMap::new(), &env(), None, None).unwrap();
        assert!(wait_exit(&p, Duration::from_secs(10)));
        assert_eq!(p.code(), Some(3));
        let s = sup.lock();
        let logs = s.logs.snapshot();
        assert!(logs.iter().any(|l| l == "one") && logs.iter().any(|l| l == "two"));
        assert_eq!(s.status, "error", "a non-zero exit while not stopping is an error");
        assert!(logs.last().unwrap().contains("Process exited (code 3)"));
    }

    #[test]
    fn startup_line_marks_running() {
        let sup = quiet();
        let p = sup.spawn(&cmd("echo Application startup complete; sleep 5"), &BTreeMap::new(), &env(), None, None).unwrap();
        let start = Instant::now();
        while sup.lock().status != "running" && start.elapsed() < Duration::from_secs(10) {
            thread::sleep(Duration::from_millis(20));
        }
        assert_eq!(sup.lock().status, "running");
        kill(&p);
    }

    #[test]
    fn stop_takes_the_whole_group_down() {
        let sup = quiet();
        // the engine forks a child that outlives a plain terminate of the shell
        let p = sup.spawn(&cmd("sleep 300 & sleep 300; wait"), &BTreeMap::new(), &env(), None, None).unwrap();
        thread::sleep(Duration::from_millis(200));
        let started = Instant::now();
        kill(&p);
        assert!(!p.alive());
        assert!(started.elapsed() < Duration::from_secs(35));
        let children = Command::new("pgrep").arg("-g").arg(p.pid.to_string()).output().map(|o| o.stdout.len()).unwrap_or(0);
        assert_eq!(children, 0, "no process of the group left");
    }
}
