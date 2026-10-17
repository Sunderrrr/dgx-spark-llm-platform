//! The ASR idle reaper, parity with dgx-portal sidecars.py ("ASR on-demand":
//! `_asr_watcher_loop`, `_asr_watcher_pass`, `asr_stop_allowed`).
//!
//! The dictation sidecar (`asr`) starts on the first transcription and must not
//! keep its GPU memory for nothing: every 30 s, if no call is in flight, the
//! sidecar is not pinned by an admin, and no transcription happened for 10 min,
//! it is stopped. All state is FILE state in the directory shared with the Flask
//! backend (`asr_ondemand::state_dir`), so both portals agreed on the idle clock and on
//! the stop lock while the Flask copy was still running.
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::asr_ondemand::{note_activity, sidecar_action, state_dir};
use crate::db::Database;
use crate::sidecars;

/// `_ASR_IDLE_S`: no transcription for this long means stop the container.
pub const IDLE_S: f64 = 600.0;
/// `_ASR_WATCH_S`: the reaper's wake-up period.
pub const WATCH_S: u64 = 30;
/// `_ASR_CALL_MAX_S`: an in-flight marker older than this is a dead worker's.
pub const CALL_MAX_S: u64 = 300;
/// `_ASR_LOCK_MAX_S`: a stop lock older than this is abandoned.
pub const LOCK_MAX_S: u64 = 60;

/// What the gate says about a stop, before any runner call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Gate {
    /// A call is in flight, or the sidecar is pinned, or it is not idle long enough.
    Keep,
    /// Nothing recorded yet (fresh start): arm the clock instead of stopping.
    Arm,
    /// Idle long enough and free: a stop may be attempted.
    Stop,
}

/// `asr_stop_allowed`'s decision, pure: in-flight calls and the pin win over
/// everything, then an absent clock is armed, then the idle threshold decides.
pub fn gate(calls_in_flight: usize, pinned: bool, idle_s: Option<f64>) -> Gate {
    if calls_in_flight > 0 || pinned {
        return Gate::Keep;
    }
    match idle_s {
        None => Gate::Arm,
        Some(idle) if idle >= IDLE_S => Gate::Stop,
        Some(_) => Gate::Keep,
    }
}

fn now_epoch() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// Age of a file, from its mtime. None when it cannot be read.
fn age_of(p: &Path) -> Option<Duration> {
    std::fs::metadata(p).ok()?.modified().ok()?.elapsed().ok()
}

/// `asr_calls_in_flight`: markers younger than `max_age`; the stale ones
/// (worker killed mid-call) are removed on the way.
pub fn calls_in_flight(dir: &Path, max_age: Duration) -> usize {
    let Ok(entries) = std::fs::read_dir(dir.join("inflight")) else { return 0 };
    let mut n = 0;
    for entry in entries.flatten() {
        let p = entry.path();
        let Some(age) = age_of(&p) else { continue };
        if age < max_age {
            n += 1;
        } else {
            let _ = std::fs::remove_file(&p);
        }
    }
    n
}

/// `_asr_pinned`: the pin file holds the epoch until which the sidecar is
/// protected from the reaper (an admin start pins it for an hour).
pub fn pinned(dir: &Path, now: f64) -> bool {
    std::fs::read_to_string(dir.join("pinned"))
        .ok()
        .and_then(|s| s.trim().parse::<f64>().ok())
        .is_some_and(|until| now < until)
}

/// `asr_idle_s`: seconds since the last call ended, None if never recorded.
pub fn idle_s(dir: &Path) -> Option<f64> {
    age_of(&dir.join("activity")).map(|a| a.as_secs_f64())
}

/// The gate read from the shared state directory. An absent clock is armed
/// here, as Flask does, so the next check has a reference point.
fn decide() -> Gate {
    let dir = state_dir();
    let g = gate(calls_in_flight(&dir, Duration::from_secs(CALL_MAX_S)), pinned(&dir, now_epoch()), idle_s(&dir));
    if g == Gate::Arm {
        note_activity();
    }
    g
}

/// Spawns the watcher. Called from main.rs unless `DGX_NO_REAPER=1`, the test
/// seam shared with the Flask reapers.
pub fn spawn(db: Arc<Database>) {
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(WATCH_S)).await;
            pass(&db).await;
        }
    });
}

/// `_asr_watcher_pass`: one reaper pass. Returns true only when a stop was
/// ordered. A lock file keeps the workers (the Rust reaper and, until its removal, the Flask one) from stopping
/// at the same instant; the stop itself is re-checked under the lock.
pub async fn pass(db: &Database) -> bool {
    if decide() != Gate::Stop {
        return false;
    }
    let lock = state_dir().join("stop-lock");
    match std::fs::OpenOptions::new().write(true).create_new(true).open(&lock) {
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            if age_of(&lock).is_some_and(|a| a > Duration::from_secs(LOCK_MAX_S)) {
                let _ = std::fs::remove_file(&lock);
            }
            return false;
        }
        Err(_) => return false,
    }
    let stopped = if decide() == Gate::Stop { stop_idle_sidecar(db).await } else { false };
    let _ = std::fs::remove_file(&lock);
    stopped
}

/// The stop itself: skipped (clock re-armed) when the sidecar is already down,
/// so a stopped container is not probed every 30 s.
async fn stop_idle_sidecar(db: &Database) -> bool {
    if sidecars::proc_status("asr").await != "running" {
        note_activity();
        return false;
    }
    let note = format!("arrêt automatique après {} min d'inactivité", (IDLE_S / 60.0) as i64);
    let (ok, _detail) = sidecar_action(db, "asr", "stop", "système", &note).await;
    // Rearm the clock whatever the outcome: no docker hammering.
    note_activity();
    ok
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_inflight_call_or_a_pin_always_keeps_the_sidecar() {
        assert_eq!(gate(1, false, Some(9_999.0)), Gate::Keep);
        assert_eq!(gate(0, true, Some(9_999.0)), Gate::Keep);
        assert_eq!(gate(2, true, None), Gate::Keep);
    }

    #[test]
    fn a_missing_clock_is_armed_not_treated_as_idle() {
        assert_eq!(gate(0, false, None), Gate::Arm);
    }

    #[test]
    fn idle_threshold_is_ten_minutes() {
        assert_eq!(gate(0, false, Some(599.0)), Gate::Keep);
        assert_eq!(gate(0, false, Some(600.0)), Gate::Stop);
        assert_eq!(gate(0, false, Some(3_600.0)), Gate::Stop);
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("asr-reaper-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("inflight")).unwrap();
        d
    }

    #[test]
    fn fresh_markers_count_and_stale_ones_are_swept() {
        let dir = scratch("inflight");
        std::fs::write(dir.join("inflight").join("a"), b"").unwrap();
        std::fs::write(dir.join("inflight").join("b"), b"").unwrap();
        assert_eq!(calls_in_flight(&dir, Duration::from_secs(300)), 2);
        // A zero window makes every marker stale: they are removed.
        assert_eq!(calls_in_flight(&dir, Duration::ZERO), 0);
        assert_eq!(std::fs::read_dir(dir.join("inflight")).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn pin_holds_until_its_epoch() {
        let dir = scratch("pin");
        std::fs::write(dir.join("pinned"), (now_epoch() + 100.0).to_string()).unwrap();
        assert!(pinned(&dir, now_epoch()));
        std::fs::write(dir.join("pinned"), "1000.0").unwrap();
        assert!(!pinned(&dir, now_epoch()));
        std::fs::write(dir.join("pinned"), "garbage").unwrap();
        assert!(!pinned(&dir, now_epoch()));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
