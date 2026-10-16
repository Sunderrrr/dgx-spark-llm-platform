//! Read-only answers for the host monitor (monitoring/monitor.py). Its unit has
//! no capability left and the data volume is 0700, so it cannot open the
//! database itself: it runs `docker exec portal-rs portal monitor-<what>`,
//! as it did with Python inside the Flask container. The database is opened
//! read-only; nothing here writes.
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Row};
use std::str::FromStr;

/// The settings keys the monitor needs to send its alerts (Admin → Config).
const SMTP_KEYS: [&str; 6] = ["cfg_smtp_host", "cfg_smtp_port", "cfg_smtp_user", "cfg_smtp_pass", "cfg_smtp_from", "cfg_admin_email"];

async fn connect(db_url: &str) -> anyhow::Result<sqlx::SqliteConnection> {
    Ok(SqliteConnectOptions::from_str(db_url)?.read_only(true).connect().await?)
}

/// `<conversations> <image and video jobs>`, the data-integrity watermark.
pub async fn counters(db_url: &str) -> anyhow::Result<String> {
    let mut c = connect(db_url).await?;
    let conv: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM conversations").fetch_one(&mut c).await?;
    let mut jobs = 0i64;
    for t in ["image_jobs", "video_jobs"] {
        jobs += sqlx::query_scalar::<_, i64>(&format!("SELECT COUNT(*) FROM {t}")).fetch_one(&mut c).await?;
    }
    Ok(format!("{conv} {jobs}"))
}

/// The SMTP settings as a JSON object of the keys present.
pub async fn smtp(db_url: &str) -> anyhow::Result<String> {
    let mut c = connect(db_url).await?;
    let marks = vec!["?"; SMTP_KEYS.len()].join(",");
    let sql = format!("SELECT key, value FROM settings WHERE key IN ({marks})");
    let mut q = sqlx::query(&sql);
    for k in SMTP_KEYS {
        q = q.bind(k);
    }
    let mut out = serde_json::Map::new();
    for row in q.fetch_all(&mut c).await? {
        let k: String = row.try_get(0)?;
        let v: Option<String> = row.try_get(1).ok();
        out.insert(k, v.map(serde_json::Value::String).unwrap_or(serde_json::Value::Null));
    }
    Ok(serde_json::Value::Object(out).to_string())
}

/// `portal monitor-counters` / `portal monitor-smtp`: Some(exit code) when the
/// command line asked for one of them, None to start the server.
pub async fn run_from_args(db_url: &str) -> Option<i32> {
    let arg = std::env::args().nth(1)?;
    let res = match arg.as_str() {
        "monitor-counters" => counters(db_url).await,
        "monitor-smtp" => smtp(db_url).await,
        _ => return None,
    };
    Some(match res {
        Ok(line) => {
            println!("{line}");
            0
        }
        Err(e) => {
            eprintln!("{e}");
            1
        }
    })
}
