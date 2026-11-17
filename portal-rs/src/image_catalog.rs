//! The image-model catalog: which diffusers folder the image sidecar serves.
//!
//! Until 2026-11-17 the launch accepted ONE identifier, written in the handler
//! and inside `image-recreate.sh`, so a second model could not exist. The row
//! now carries everything the launcher needs — the HF id, the local folder, the
//! display name and the two inference settings that differ per model (a
//! distilled few-step model needs guidance 1.0 and 4 steps; a full one wants
//! ~35 steps and guidance ~4).
use serde_json::Value;
use sqlx::SqlitePool;

#[derive(Debug, Clone, PartialEq)]
pub struct ImageModel {
    pub name: String,
    pub hf_model_id: String,
    pub slug: String,
    pub steps: i64,
    pub guidance: f64,
}

/// The table, created on first use like the other sidecar catalogs.
pub async fn ensure_table(pool: &SqlitePool) {
    let _ = sqlx::query(
        "CREATE TABLE IF NOT EXISTS image_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, hf_model_id TEXT NOT NULL, slug TEXT NOT NULL, steps INTEGER NOT NULL DEFAULT 28, guidance REAL NOT NULL DEFAULT 4.0, added_at TEXT NOT NULL DEFAULT '')",
    )
    .execute(pool)
    .await;
    // The model the platform shipped with, so the catalog is never empty.
    let _ = sqlx::query(
        "INSERT OR IGNORE INTO image_configs (name, hf_model_id, slug, steps, guidance, added_at) VALUES ('FLUX.2 Klein 4B', 'black-forest-labs/FLUX.2-klein-4B', 'flux2-klein-4b', 4, 1.0, '')",
    )
    .execute(pool)
    .await;
}

/// Every entry, oldest first.
pub async fn list(pool: &SqlitePool) -> Vec<Value> {
    ensure_table(pool).await;
    let rows: Vec<(i64, String, String, String, i64, f64, String)> = sqlx::query_as(
        "SELECT id, name, hf_model_id, slug, steps, guidance, added_at FROM image_configs ORDER BY id",
    )
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    rows.into_iter()
        .map(|(id, name, hf, slug, steps, guidance, added)| {
            serde_json::json!({"id": id, "name": name, "hf_model_id": hf, "slug": slug, "steps": steps,
                               "guidance": guidance, "added_at": added})
        })
        .collect()
}

/// One entry by its HF id (what the launch form sends).
pub async fn find(pool: &SqlitePool, hf_model_id: &str) -> Option<ImageModel> {
    ensure_table(pool).await;
    let q = hf_model_id.trim();
    let row: Option<(String, String, String, i64, f64)> = sqlx::query_as(
        "SELECT name, hf_model_id, slug, steps, guidance FROM image_configs WHERE hf_model_id=? OR name=?",
    )
    .bind(q)
    .bind(q)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten();
    row.map(|(name, hf_model_id, slug, steps, guidance)| ImageModel { name, hf_model_id, slug, steps, guidance })
}

/// A folder name derived from a repository id: `black-forest-labs/FLUX.2-klein-4B`
/// → `flux-2-klein-4b`. Bounded, lower-case, safe for a path segment.
pub fn slug_of(hf_model_id: &str) -> String {
    let last = hf_model_id.rsplit('/').next().unwrap_or(hf_model_id);
    let mut out = String::new();
    for c in last.chars() {
        let c = c.to_ascii_lowercase();
        if c.is_ascii_alphanumeric() {
            out.push(c);
        } else if !out.ends_with('-') && !out.is_empty() {
            out.push('-');
        }
    }
    out.trim_matches('-').chars().take(60).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_slug_is_a_safe_folder_name() {
        assert_eq!(slug_of("black-forest-labs/FLUX.2-klein-4B"), "flux-2-klein-4b");
        assert_eq!(slug_of("stabilityai/SDXL-Turbo"), "sdxl-turbo");
        assert_eq!(slug_of("a/.."), "");
        assert!(slug_of(&"x".repeat(200)).len() <= 60);
    }
}
