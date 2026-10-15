//! The dictation sidecar's on-demand state, parity with dgx-portal sidecars.py
//! (« ASR on-demand »): FILE state in a directory both portals share
//! (`ASR_ONDEMAND_DIR`, on the data volume), so Flask's idle reaper sees the
//! activity and the in-flight calls Rust records. The reaper itself stays in
//! Flask; Rust only writes the clock and the markers, and starts the sidecar.
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::db::Database;

/// MemAvailable headroom required for an AUTOMATIC start (`_ASR_AUTO_MIN_GIB`).
pub const AUTO_MIN_GIB: f64 = 8.0;
/// A successful start silences the next attempts this long (`_ASR_RESTART_S`).
const RESTART_S: u64 = 60;

pub fn state_dir() -> PathBuf {
    let d = PathBuf::from(std::env::var("ASR_ONDEMAND_DIR").unwrap_or_else(|_| "/tmp/dgx-asr-ondemand".to_string()));
    let _ = std::fs::create_dir_all(d.join("inflight"));
    d
}

/// `asr_note_activity`: reset the idle clock (the file's mtime).
pub fn note_activity() {
    let p = state_dir().join("activity");
    if let Ok(f) = std::fs::OpenOptions::new().create(true).append(true).open(&p) {
        let _ = f.set_modified(std::time::SystemTime::now());
    }
}

/// `asr_call_in_flight`: a marker file while a transcription runs; on drop the
/// marker goes and the idle clock restarts (idle = since the last call ENDED).
pub struct InFlight(Option<PathBuf>);

impl InFlight {
    pub fn start() -> InFlight {
        let mut rnd = [0u8; 8];
        let _ = getrandom::getrandom(&mut rnd);
        let p = state_dir().join("inflight").join(format!("tmp{}", hex::encode(rnd)));
        InFlight(std::fs::File::create(&p).ok().map(|_| p))
    }
}

impl Drop for InFlight {
    fn drop(&mut self) {
        if let Some(p) = self.0.take() {
            let _ = std::fs::remove_file(p);
        }
        note_activity();
    }
}

/// MemAvailable in GiB (`_mem_available_gb`).
pub fn mem_available_gb() -> Option<f64> {
    let s = std::fs::read_to_string("/proc/meminfo").ok()?;
    let kb: f64 = s.lines().find(|l| l.starts_with("MemAvailable:"))?.split_whitespace().nth(1)?.parse().ok()?;
    Some(kb / 1024.0 / 1024.0)
}

/// `asr_memory_ok`: the refusal text when an automatic start is not safe.
pub fn memory_refusal() -> Option<String> {
    match mem_available_gb() {
        None => Some("Mémoire disponible illisible : la dictée ne démarre pas toute seule sans ce garde-fou. Réessaie plus tard.".to_string()),
        Some(a) if a < AUTO_MIN_GIB => Some(format!(
            "Mémoire insuffisante pour démarrer la dictée ({a:.1} Go disponibles, ~{} Go nécessaires) : le modèle de chat en a besoin. Libère de la mémoire, puis réessaie.",
            AUTO_MIN_GIB as i64
        )),
        _ => None,
    }
}

/// `_sidecar_action`: ask the runner, audit the outcome; (ok, detail).
pub async fn sidecar_action(db: &Database, kind: &str, action: &str, actor: &str, note: &str) -> (bool, String) {
    let token = std::env::var("RUNNER_TOKEN").unwrap_or_default();
    let url = format!("{}/{kind}/{action}", crate::probes::runner_url());
    let sent = async {
        let c = reqwest::Client::builder().timeout(Duration::from_secs(30)).build().map_err(|e| e.to_string())?;
        c.post(url).bearer_auth(token).send().await.map_err(|e| if e.is_timeout() { "ReadTimeout".to_string() } else { "ConnectionError".to_string() })
    }
    .await;
    let (ok, detail) = match sent {
        Err(kind_of) => (false, format!(" ({kind_of})")),
        Ok(r) if r.status().is_success() => (true, String::new()),
        Ok(r) => {
            let status = r.status().as_u16();
            let text = r.text().await.unwrap_or_default();
            let reason = match serde_json::from_str::<Value>(&text) {
                Ok(b) => b.get("detail").or_else(|| b.get("error")).filter(|v| crate::keys::truthy(v)).map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())).unwrap_or_default(),
                Err(_) => text.chars().take(200).collect(),
            };
            let reason = reason.trim().to_string();
            (false, if reason.is_empty() { format!(" (HTTP {status})") } else { format!(" ({reason})") })
        }
    };
    let outcome = if ok { "OK".to_string() } else { format!("échec{detail}") };
    let line = if note.is_empty() { format!("{kind} : {outcome}") } else { format!("{kind} : {outcome} — {note}") };
    crate::sessions::log_audit(db.pool(), actor, &format!("sidecar.{action}"), &line).await;
    (ok, detail)
}

static LAST_ASR_START: Mutex<Option<Instant>> = Mutex::new(None);

/// `asr_autostart`: start `asr` for a dictation, throttled after a success.
pub async fn autostart(db: &Database, actor: &str) -> (bool, String) {
    if LAST_ASR_START.lock().unwrap_or_else(|e| e.into_inner()).is_some_and(|t| t.elapsed() < Duration::from_secs(RESTART_S)) {
        return (true, String::new());
    }
    let (ok, detail) = sidecar_action(db, "asr", "start", actor, "").await;
    if ok {
        *LAST_ASR_START.lock().unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
    }
    (ok, detail)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markers_and_clock_live_in_the_shared_dir() {
        let dir = std::env::temp_dir().join(format!("asr-od-{}", std::process::id()));
        std::env::set_var("ASR_ONDEMAND_DIR", &dir);
        {
            let _call = InFlight::start();
            assert_eq!(std::fs::read_dir(dir.join("inflight")).unwrap().count(), 1);
        }
        assert_eq!(std::fs::read_dir(dir.join("inflight")).unwrap().count(), 0);
        let age = std::fs::metadata(dir.join("activity")).unwrap().modified().unwrap().elapsed().unwrap();
        assert!(age < Duration::from_secs(5));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
