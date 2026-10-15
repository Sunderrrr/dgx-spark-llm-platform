//! Request guards shared by the media routes, parity with dgx-portal guards.py:
//! maintenance first, then the per-account rate bucket (in SQL, so Flask and
//! Rust count against the same window), and the in-flight job bound.
use std::collections::HashMap;
use std::sync::Mutex;

use sqlx::SqlitePool;

/// Media calls allowed per window and account (`CHAT_RATE_MAX` / `_WINDOW`).
pub const MEDIA_RATE_MAX: i64 = 20;
pub const MEDIA_RATE_WINDOW_S: f64 = 60.0;
/// In-flight async jobs per account (`MEDIA_MAX_CONCURRENT`).
pub const MEDIA_MAX_CONCURRENT: u32 = 3;

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `_chat_rate_limited`: seconds to wait, or 0 when the request is counted.
/// The acquisition is one conditional UPSERT: the database decides.
pub async fn rate_limited(pool: &SqlitePool, username: &str, bucket: &str, max: i64, window: f64) -> Result<i64, sqlx::Error> {
    let now = now();
    let key = format!("{bucket}|{username}");
    let counted = sqlx::query(
        "INSERT INTO login_attempts (key, fails, first_at, locked_until) VALUES (?,1,?,0) \
         ON CONFLICT(key) DO UPDATE SET fails=fails+1 \
         WHERE login_attempts.first_at > ? AND login_attempts.fails < ?",
    )
    .bind(&key)
    .bind(now)
    .bind(now - window)
    .bind(max)
    .execute(pool)
    .await?
    .rows_affected();
    if counted > 0 {
        return Ok(0);
    }
    let first: Option<f64> = sqlx::query_scalar("SELECT first_at FROM login_attempts WHERE key = ?").bind(&key).fetch_optional(pool).await?;
    if let Some(first) = first {
        if now - first <= window {
            return Ok(((window - (now - first)) as i64).max(1));
        }
    }
    // Expired window: reopen it.
    sqlx::query("UPDATE login_attempts SET fails=1, first_at=? WHERE key=?").bind(now).bind(&key).execute(pool).await?;
    Ok(0)
}

static SLOTS: Mutex<Option<HashMap<String, u32>>> = Mutex::new(None);

/// `media_job_slot`: true when accepted (release with `job_done`).
pub fn job_slot(username: &str) -> bool {
    let mut g = SLOTS.lock().unwrap_or_else(|e| e.into_inner());
    let m = g.get_or_insert_with(HashMap::new);
    let n = m.entry(username.to_string()).or_insert(0);
    if *n >= MEDIA_MAX_CONCURRENT {
        return false;
    }
    *n += 1;
    true
}

/// `media_job_done`.
pub fn job_done(username: &str) {
    let mut g = SLOTS.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(m) = g.as_mut() {
        match m.get(username).copied() {
            Some(n) if n > 1 => {
                m.insert(username.to_string(), n - 1);
            }
            _ => {
                m.remove(username);
            }
        }
    }
}

/// `shared_memory_note`: the free memory, since a stopped service is only half the story.
pub fn shared_memory_note() -> String {
    let avail = std::fs::read_to_string("/proc/meminfo").ok().and_then(|s| {
        s.lines()
            .find(|l| l.starts_with("MemAvailable:"))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse::<f64>().ok())
    });
    match avail.map(|kb| kb / 1024.0 / 1024.0).filter(|g| *g != 0.0) {
        Some(g) => format!(" Le modèle de chat partage la mémoire : {g:.1} Go libres — arrête-le depuis Admin pour en libérer."),
        None => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slots_are_bounded_per_account() {
        let u = "slot-test-user";
        assert!(job_slot(u) && job_slot(u) && job_slot(u));
        assert!(!job_slot(u));
        job_done(u);
        assert!(job_slot(u));
        for _ in 0..3 {
            job_done(u);
        }
        assert!(job_slot(u));
        job_done(u);
    }
}
