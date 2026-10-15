//! Writing and removing generated files, parity with dgx-portal media_store.py:
//! Admin → Stockage picks where NEW files go (`media_storage` = local | s3);
//! an S3 write failure keeps the file locally instead of losing it, and a
//! removal clears both places. Reading is `handlers::serve_media`.
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
