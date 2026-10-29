//! The runner's state on disk and in memory (runner.py): the in-memory log
//! buffer, the per-startup journal, the auto-resume state and its failure
//! counter, and the memory reading the guards rely on.
use std::collections::VecDeque;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// `_append` / `_logs`: 2 000 lines; past it the oldest 500 go.
#[derive(Default)]
pub struct LogBuffer {
    lines: VecDeque<String>,
}

impl LogBuffer {
    pub fn append(&mut self, line: &str) {
        self.lines.push_back(line.to_string());
        if self.lines.len() > 2000 {
            self.lines.drain(..500);
        }
    }
    pub fn clear(&mut self) {
        self.lines.clear();
    }
    pub fn len(&self) -> usize {
        self.lines.len()
    }
    pub fn is_empty(&self) -> bool {
        self.lines.is_empty()
    }
    /// `_logs[-n:]`
    pub fn tail(&self, n: usize) -> Vec<String> {
        let skip = self.lines.len().saturating_sub(n);
        self.lines.iter().skip(skip).cloned().collect()
    }
    /// `_logs[last:]`
    pub fn from(&self, index: usize) -> Vec<String> {
        self.lines.iter().skip(index).cloned().collect()
    }
    pub fn snapshot(&self) -> Vec<String> {
        self.lines.iter().cloned().collect()
    }
}

/// `_LOG_MAX_BYTES` / `_LOG_KEEP`.
pub const LOG_MAX_CHARS: usize = 20 * 1024 * 1024;
pub const LOG_KEEP: usize = 12;

/// `_RunLog`: a size-capped startup file, silent on failure: after the cap or
/// the first error it stops writing for good and never raises.
pub struct RunLog {
    file: Option<File>,
    written: usize,
}

impl RunLog {
    pub fn create(path: &Path) -> std::io::Result<RunLog> {
        Ok(RunLog { file: Some(File::create(path)?), written: 0 })
    }

    pub fn write(&mut self, line: &str) {
        let Some(f) = self.file.as_mut() else { return };
        if writeln!(f, "{line}").is_err() {
            self.file = None;
            return;
        }
        self.written += line.chars().count() + 1;
        if self.written > LOG_MAX_CHARS {
            let _ = writeln!(f, "[runner] log truncated at {} MB", LOG_MAX_CHARS / 1024 / 1024);
            self.file = None;
        }
    }

    pub fn is_open(&self) -> bool {
        self.file.is_some()
    }
}

/// `_prune_logs`: keep the newest startups only.
pub fn prune_logs(dir: &Path) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    let mut files: Vec<(std::time::SystemTime, PathBuf)> = rd
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().ends_with(".log"))
        .filter_map(|e| Some((e.metadata().ok()?.modified().ok()?, e.path())))
        .collect();
    // Newest first: Reverse keeps the intent visible (clippy: sort_by_key).
    files.sort_by_key(|f| std::cmp::Reverse(f.0));
    for (_, old) in files.into_iter().skip(LOG_KEEP) {
        let _ = fs::remove_file(old);
    }
}

/// `_open_run_log(name, engine)`: the journal of THIS startup, or None.
pub fn open_run_log(dir: &Path, name: &str, engine: &str, stamp: &str) -> Option<RunLog> {
    fs::create_dir_all(dir).ok()?;
    let clean: String = name.chars().map(|c| if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') { c } else { '_' }).take(60).collect();
    let clean = if clean.is_empty() { "modele".to_string() } else { clean };
    let path = dir.join(format!("{stamp}-{engine}-{clean}.log"));
    let log = RunLog::create(&path).ok()?;
    prune_logs(dir);
    Some(log)
}

/// `_save_last_launch`: the launch to resume after a restart.
pub fn save_last_launch(path: &Path, hf_id: &str, name: &str, tokens: &[String], engine: &str) -> std::io::Result<()> {
    let body = serde_json::json!({
        "hf_model_id": hf_id,
        "model_name": name,
        "vllm_args": tokens.join(" "),
        "engine": engine,
    });
    fs::write(path, body.to_string())
}

/// `_load_last_launch`: None when there is nothing (or it is unreadable).
pub fn load_last_launch(path: &Path) -> Option<serde_json::Value> {
    serde_json::from_str(&fs::read_to_string(path).ok()?).ok()
}

/// `_clear_last_launch`: a deliberate stop forgets the launch.
pub fn clear_last_launch(path: &Path) {
    let _ = fs::remove_file(path);
}

/// `_read_resume_fails` / `_write_resume_fails`: the counter persisted to disk.
pub fn read_resume_fails(path: &Path) -> i64 {
    // int(f.read().strip() or 0), and 0 on any error
    let text = fs::read_to_string(path).unwrap_or_default();
    let t = text.trim();
    if t.is_empty() { 0 } else { t.parse::<i64>().unwrap_or(0) }
}

pub fn write_resume_fails(path: &Path, n: i64) {
    let _ = fs::write(path, n.to_string());
}

/// `_mem_available_gib`: MemAvailable of a meminfo text, in GiB.
pub fn mem_available_gib_from(meminfo: &str) -> Option<f64> {
    meminfo.lines().find(|l| l.starts_with("MemAvailable:")).and_then(|l| l.split_whitespace().nth(1)).and_then(|v| v.parse::<f64>().ok()).map(|kb| kb / 1024.0 / 1024.0)
}

pub fn mem_available_gib() -> Option<f64> {
    mem_available_gib_from(&fs::read_to_string("/proc/meminfo").ok()?)
}

/// `_wait_mem_release(timeout, settle)`: wait until MemAvailable stops rising,
/// then a settle margin. Returns (the seconds waited, the reading after).
pub fn wait_mem_release<F: FnMut() -> Option<f64>, S: FnMut(Duration)>(mut read: F, mut sleep: S, timeout: Duration, interval: Duration, settle: Duration) -> (Duration, Option<f64>) {
    let start = Instant::now();
    let Some(mut prev) = read() else {
        sleep(settle);
        return (start.elapsed(), None);
    };
    let mut stable = 0;
    while start.elapsed() < timeout {
        sleep(interval);
        let Some(cur) = read() else { break };
        if cur - prev < 0.5 {
            stable += 1;
            if stable >= 2 {
                break;
            }
        } else {
            stable = 0;
        }
        prev = cur;
    }
    let free = read();
    sleep(settle);
    (start.elapsed(), free)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn buffer_trims_like_runner_py() {
        let mut b = LogBuffer::default();
        for i in 0..2001 {
            b.append(&i.to_string());
        }
        assert_eq!(b.len(), 1501);
        assert_eq!(b.tail(1)[0], "2000");
        assert_eq!(b.from(1500)[0], "2000");
    }

    #[test]
    fn meminfo_in_gib() {
        let text = "MemTotal:       124000000 kB\nMemAvailable:   1048576 kB\n";
        assert_eq!(mem_available_gib_from(text), Some(1.0));
        assert_eq!(mem_available_gib_from("MemFree: 1 kB"), None);
    }

    #[test]
    fn wait_stops_on_a_plateau() {
        // memory rises, then flattens: two quiet readings end the wait
        let mut values = vec![10.0, 20.0, 20.1, 20.2, 20.2, 20.2, 20.2].into_iter();
        let mut reads = 0;
        let (_, free) = wait_mem_release(|| { reads += 1; values.next() }, |_| {}, Duration::from_secs(60), Duration::from_millis(1), Duration::from_millis(1));
        assert_eq!(free, Some(20.2));
        assert!(reads < 7, "must stop on the plateau, not run to the end");
    }
}
