//! Writing and removing generated files, parity with dgx-portal media_store.py:
//! Admin → Stockage picks where NEW files go (`media_storage` = local | s3);
//! an S3 write failure keeps the file locally instead of losing it, and a
//! removal clears both places. Reading is `handlers::serve_media`.
use std::time::{Duration, SystemTime};

use sqlx::SqliteConnection;

use crate::db::Database;
use crate::s3;

/// The data volume (`MEDIA_ROOT` overrides it for tests).
pub fn root() -> String {
    std::env::var("MEDIA_ROOT").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "/app/data".to_string())
}
pub const KINDS: [&str; 4] = ["image_files", "video_files", "ocr_images", "voice_audio"];

/// `_check`: a server-made bare file name, never a client path.
pub fn valid_name(kind: &str, name: &str) -> bool {
    let mut chars = name.chars();
    KINDS.contains(&kind)
        && chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && name.chars().count() <= 201
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
        && !name.contains("..")
}

pub fn local_path(kind: &str, name: &str) -> String {
    format!("{}/{kind}/{name}", root())
}

/// `location`: 's3' only when chosen AND configured, with its settings.
async fn bucket_if_chosen(db: &Database) -> Option<s3::Config> {
    if db.get_setting("media_storage").await.ok().flatten().as_deref() != Some("s3") {
        return None;
    }
    s3::Config::load(db).await
}

/// `save`: writes a new file where Admin → Stockage says; 's3' or 'local'.
pub async fn save(db: &Database, kind: &str, name: &str, data: Vec<u8>, content_type: &str) -> Result<&'static str, String> {
    if !valid_name(kind, name) {
        return Err(format!("invalid media reference {kind}/{name:?}"));
    }
    if let Some(cfg) = bucket_if_chosen(db).await {
        match s3::put(&cfg, &cfg.key(&["media", kind, name]), data.clone(), content_type).await {
            Ok(()) => return Ok("s3"),
            Err(e) => eprintln!("[media_store] S3 write failed for {kind}/{name}, kept locally: {e}"),
        }
    }
    tokio::fs::create_dir_all(format!("{}/{kind}", root())).await.map_err(|e| e.to_string())?;
    tokio::fs::write(local_path(kind, name), data).await.map_err(|e| e.to_string())?;
    Ok("local")
}

/// `exists`: on the volume, else in the bucket.
pub async fn exists(db: &Database, kind: &str, name: &str) -> bool {
    if !valid_name(kind, name) {
        return false;
    }
    if tokio::fs::metadata(local_path(kind, name)).await.is_ok_and(|m| m.is_file()) {
        return true;
    }
    match s3::Config::load(db).await {
        Some(cfg) => s3::exists(&cfg, &cfg.key(&["media", kind, name])).await,
        None => false,
    }
}

/// `delete`: removes the file wherever it is. Never fails: a leftover is swept later.
pub async fn delete(db: &Database, kind: &str, name: &str) {
    if !valid_name(kind, name) {
        return;
    }
    let _ = tokio::fs::remove_file(local_path(kind, name)).await;
    if let Some(cfg) = s3::Config::load(db).await {
        if let Err(e) = s3::delete(&cfg, &cfg.key(&["media", kind, name])).await {
            eprintln!("[media_store] S3 delete failed for {kind}/{name}: {e}");
        }
    }
}

// ── Orphan sweep (the nightly `portal media-sweep`) ─────────────────────────

/// Files older than this, with no database row left, are removed.
pub const ORPHAN_GRACE_DAYS: u64 = 7;

/// The database references each kind's files by (parts of) their name.
const REFS: [(&str, &str); 4] = [
    ("image_files", "SELECT CAST(prompt_id AS TEXT) FROM image_jobs"),
    ("video_files", "SELECT CAST(prompt_id AS TEXT) FROM video_jobs"),
    ("ocr_images", "SELECT CAST(image_path AS TEXT) FROM ocr_jobs WHERE image_path IS NOT NULL"),
    ("voice_audio", "SELECT CAST(audio_path AS TEXT) FROM voice_jobs WHERE audio_path IS NOT NULL"),
];

/// `_id_of`: the database reference a file belongs to.
pub fn id_of(kind: &str, name: &str) -> String {
    let stem = name.rsplit_once('.').map_or(name, |(s, _)| s);
    match kind {
        "ocr_images" | "voice_audio" => name.to_string(),
        // image_files: <prompt_id>_<idx>.<ext>
        "image_files" => {
            let trimmed = stem.trim_end_matches(|c: char| c.is_ascii_digit());
            match trimmed.strip_suffix('_') {
                Some(id) if trimmed.len() < stem.len() => id.to_string(),
                _ => stem.to_string(),
            }
        }
        // video_files: <prompt_id>.mp4
        _ => stem.to_string(),
    }
}

/// `sweep(dry_run=…)`: removes the files no row references any more, older than
/// the grace period (a job writes its file before its row in some paths).
/// Returns `{"local": n, "s3": n, "dry_run": bool}`.
pub async fn sweep(con: &mut SqliteConnection, dry_run: bool) -> Result<serde_json::Value, String> {
    let mut refs: Vec<(&str, std::collections::HashSet<String>)> = Vec::new();
    for (kind, query) in REFS {
        let rows: Vec<Option<String>> = sqlx::query_scalar(query).fetch_all(&mut *con).await.map_err(|e| e.to_string())?;
        refs.push((kind, rows.into_iter().flatten().collect()));
    }
    let referenced = |kind: &str, name: &str| refs.iter().any(|(k, set)| *k == kind && set.contains(&id_of(kind, name)));
    let limit = SystemTime::now() - Duration::from_secs(ORPHAN_GRACE_DAYS * 86400);
    let (mut local, mut s3_count) = (0u64, 0u64);
    for kind in KINDS {
        let dir = format!("{}/{kind}", root());
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            let path = entry.path();
            let Ok(meta) = std::fs::metadata(&path) else { continue };
            if !meta.is_file() || name.starts_with('.') || referenced(kind, &name) {
                continue;
            }
            if meta.modified().is_ok_and(|m| m < limit) {
                local += 1;
                if !dry_run {
                    std::fs::remove_file(&path).map_err(|e| e.to_string())?;
                }
            }
        }
    }
    if let Some(cfg) = s3::Config::load_conn(con).await {
        let prefix = format!("{}/", cfg.key(&["media"]));
        for (key, _size, modified) in s3::list_keys(&cfg, &prefix).await? {
            // `kind, name = k.rsplit('/', 2)[-2:]`
            let mut parts = key.rsplit('/');
            let (Some(name), Some(kind)) = (parts.next(), parts.next()) else { continue };
            let old = chrono::DateTime::parse_from_rfc3339(&modified)
                .is_ok_and(|m| SystemTime::from(m.with_timezone(&chrono::Utc)) < limit);
            if !KINDS.contains(&kind) || !old || referenced(kind, name) {
                continue;
            }
            s3_count += 1;
            if !dry_run {
                s3::delete(&cfg, &key).await?;
            }
        }
    }
    Ok(serde_json::json!({"local": local, "s3": s3_count, "dry_run": dry_run}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_are_bare_server_names() {
        assert!(valid_name("image_files", "abc_0.png"));
        assert!(!valid_name("image_files", "../x.png"));
        assert!(!valid_name("image_files", "a/b.png"));
        assert!(!valid_name("other", "a.png"));
        assert!(!valid_name("image_files", "_a.png"));
    }
}

#[cfg(test)]
mod sweep_tests {
    use super::*;

    #[test]
    fn a_file_maps_to_the_row_it_belongs_to() {
        assert_eq!(id_of("image_files", "abc123_0.png"), "abc123");
        assert_eq!(id_of("image_files", "abc_1_12.webp"), "abc_1");
        assert_eq!(id_of("image_files", "_3.png"), "");
        assert_eq!(id_of("video_files", "vid1.mp4"), "vid1");
        assert_eq!(id_of("ocr_images", "scan.png"), "scan.png");
    }
}
