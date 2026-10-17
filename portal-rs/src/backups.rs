//! Backups, parity with dgx-portal/backups.py: the settings the host reads
//! (Admin → Stockage) and the upload of an archive to the bucket, read back
//! and checked, then pruned to the most recent ones. The host reaches both
//! through `docker exec portal-rs portal backup-settings | backup-upload`
//! (scripts/backup.sh), so the S3 credentials never leave the portal.
use serde_json::{json, Value};
use sqlx::SqliteConnection;

use crate::s3;

const DESTINATIONS: [&str; 3] = ["local", "s3", "both"];
const DEFAULT_KEEP: i64 = 7;
const MAX_KEEP: i64 = 365;

/// A plain setting (not an instance one), raw as stored; None when absent or NULL.
async fn setting(con: &mut SqliteConnection, key: &str) -> Option<String> {
    sqlx::query_scalar::<_, Option<String>>("SELECT value FROM settings WHERE key = ?")
        .bind(key)
        .fetch_optional(&mut *con)
        .await
        .ok()
        .flatten()
        .flatten()
}

/// `_keep`: an integer clamped to [1, MAX_KEEP]; anything unreadable is the default.
fn keep_from(raw: Option<&str>) -> i64 {
    let raw = raw.unwrap_or("");
    let n = if raw.is_empty() { DEFAULT_KEEP } else { raw.trim().parse().unwrap_or(DEFAULT_KEEP) };
    n.clamp(1, MAX_KEEP)
}

/// `settings()`: destination, retentions and whether the bucket is ready. No secret.
pub async fn settings(con: &mut SqliteConnection) -> Value {
    let destination = match setting(con, "backup_destination").await.as_deref() {
        Some(v) if DESTINATIONS.contains(&v) => v.to_string(),
        _ => "local".to_string(),
    };
    let keep_local = keep_from(setting(con, "backup_keep_local").await.as_deref());
    let keep_s3 = keep_from(setting(con, "backup_keep_s3").await.as_deref());
    let s3_ready = s3::Config::load_conn(con).await.is_some();
    json!({
        "destination": destination,
        "keep_local": keep_local,
        "keep_s3": keep_s3,
        "s3_ready": s3_ready,
    })
}

/// The archive keys of the bucket, newest first.
async fn archives(cfg: &s3::Config) -> Result<Vec<String>, String> {
    let prefix = format!("{}/", cfg.key(&["backups"]));
    let mut found: Vec<String> = s3::list_keys(cfg, &prefix)
        .await?
        .into_iter()
        .map(|(k, _, _)| k)
        .filter(|k| k.ends_with(".tar.gz"))
        .collect();
    found.sort_by(|a, b| b.cmp(a));
    Ok(found)
}

/// `upload(name, keep, stream)`: stores one archive, checks it is in the bucket
/// with the size it was sent with, then keeps only the `keep` most recent ones.
pub async fn upload(con: &mut SqliteConnection, name: &str, keep: i64, data: Vec<u8>) -> Result<Value, String> {
    if !name.ends_with(".tar.gz") || name.contains('/') {
        return Err(format!("unexpected archive name '{name}'"));
    }
    let cfg = s3::Config::load_conn(con)
        .await
        .ok_or_else(|| "Stockage S3 non configuré (Admin → Config).".to_string())?;
    let key = cfg.key(&["backups", name]);
    let sent = data.len() as u64;
    s3::put(&cfg, &key, data, "application/octet-stream").await?;
    match s3::size(&cfg, &key).await {
        Some(size) if size > 0 => {
            if size != sent {
                return Err(format!("{key} has {size} bytes in the bucket, {sent} were sent"));
            }
        }
        _ => return Err(format!("{key} not found after upload")),
    }
    let listed = archives(&cfg).await?;
    let mut purged = Vec::new();
    for old in listed.iter().skip(keep as usize) {
        s3::delete(&cfg, old).await?;
        purged.push(old.rsplit('/').next().unwrap_or(old).to_string());
    }
    Ok(json!({
        "key": key,
        "size": sent,
        "kept": (keep as usize).min(listed.len()),
        "purged": purged,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retention_is_clamped_and_falls_back_to_the_default() {
        assert_eq!(keep_from(None), DEFAULT_KEEP);
        assert_eq!(keep_from(Some("")), DEFAULT_KEEP);
        assert_eq!(keep_from(Some("x")), DEFAULT_KEEP);
        assert_eq!(keep_from(Some(" 5 ")), 5);
        assert_eq!(keep_from(Some("0")), 1);
        assert_eq!(keep_from(Some("100000")), MAX_KEEP);
    }
}
