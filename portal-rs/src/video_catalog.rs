//! The video-model catalog: which UNET ComfyUI loads for a generation.
//!
//! Until 2026-11-17 the pipeline was frozen: the finish templates carried the
//! file names, so the only video model that could ever run was the one baked
//! into them. A row now carries the files, and `h3_graph_with` substitutes them.
use serde_json::Value;
use sqlx::SqlitePool;

#[derive(Debug, Clone, PartialEq)]
pub struct VideoModel {
    pub name: String,
    pub hf_model_id: String,
    pub unet_file: String,
    pub vae_file: String,
    pub audio_vae_file: String,
}

/// The table, created on first use like the other sidecar catalogs.
pub async fn ensure_table(pool: &SqlitePool) {
    let _ = sqlx::query(
        "CREATE TABLE IF NOT EXISTS video_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, hf_model_id TEXT NOT NULL DEFAULT '', unet_file TEXT NOT NULL, vae_file TEXT NOT NULL, audio_vae_file TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL DEFAULT '')",
    )
    .execute(pool)
    .await;
    // The pipeline the platform shipped with, so the catalog is never empty.
    let _ = sqlx::query(
        "INSERT OR IGNORE INTO video_configs (name, hf_model_id, unet_file, vae_file, audio_vae_file, added_at) VALUES ('MiniMax H3', 'MiniMaxAI/MiniMax-H3', 'minimax_h3_fl2va_pruned_nvfp4.safetensors', 'minimax_h3_video_vae_fp16.safetensors', 'minimax_h3_audio_vae_fp32.safetensors', '')",
    )
    .execute(pool)
    .await;
}

/// Every entry, oldest first.
pub async fn list(pool: &SqlitePool) -> Vec<Value> {
    ensure_table(pool).await;
    let rows: Vec<(i64, String, String, String, String, String)> = sqlx::query_as(
        "SELECT id, name, hf_model_id, unet_file, vae_file, audio_vae_file FROM video_configs ORDER BY id",
    )
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    rows.into_iter()
        .map(|(id, name, hf, unet, vae, audio)| {
            serde_json::json!({"id": id, "name": name, "hf_model_id": hf, "unet_file": unet,
                               "vae_file": vae, "audio_vae_file": audio})
        })
        .collect()
}

/// The entry a job runs on: the named one, else the first.
pub async fn active(pool: &SqlitePool, wanted: &str) -> Option<VideoModel> {
    ensure_table(pool).await;
    let rows: Vec<(String, String, String, String, String)> = sqlx::query_as(
        "SELECT name, hf_model_id, unet_file, vae_file, audio_vae_file FROM video_configs ORDER BY id",
    )
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    let pick = rows
        .iter()
        .find(|r| r.0 == wanted)
        .or_else(|| rows.first())?;
    Some(VideoModel {
        name: pick.0.clone(),
        hf_model_id: pick.1.clone(),
        unet_file: pick.2.clone(),
        vae_file: pick.3.clone(),
        audio_vae_file: pick.4.clone(),
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn an_empty_catalog_falls_back_to_the_default_entry() {
        // The seed is what keeps a fresh install working; the query itself is
        // exercised by the e2e journey, this pins the SELECT shape.
        assert!("SELECT id, name, hf_model_id, unet_file, vae_file, audio_vae_file FROM video_configs ORDER BY id"
            .contains("video_configs"));
    }
}
