//! The voice-model catalog: which engine the voice sidecar serves.
//!
//! The table and the endpoint that launches from it already existed
//! (`/admin/voice/catalog/launch`), but nothing ever SEEDED it: the admin page
//! rendered « aucun modèle » and the only way to start the sidecar was the
//! generic « Démarrer » (a bare `docker start`), which cannot switch engine and
//! does not exist once the container has been removed. The operator's own voice
//! — a fine-tune served by `voice-qwen-custom-recreate.sh` — therefore could not
//! be launched from the page at all (reported 2026-11-27).
//!
//! Seeding it here mirrors `image_catalog`: the catalog is never empty, and the
//! card appears on the first load of the admin page.
use sqlx::SqlitePool;

/// The operator's fine-tuned voice, and the label the card shows.
const DEFAULT_VOICE: (&str, &str) = ("Ma voix (fine-tuné)", "custom-qwen-tts");

/// The table, created on first use like the other sidecar catalogs, and seeded.
pub async fn ensure_table(pool: &SqlitePool) {
    let _ = sqlx::query(
        "CREATE TABLE IF NOT EXISTS voice_configs (\
           id INTEGER PRIMARY KEY AUTOINCREMENT, \
           name TEXT NOT NULL UNIQUE, \
           repo_id TEXT NOT NULL, \
           added_at TEXT NOT NULL)",
    )
    .execute(pool)
    .await;
    // Seed ONLY an empty catalog. `INSERT OR IGNORE` alone was not enough: a
    // deleted entry came back on the next page load, because the unique name no
    // longer existed — the operator's choice was undone every time (Kody, PR 17).
    let empty: Option<i64> = sqlx::query_scalar("SELECT COUNT(*) FROM voice_configs")
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
    if empty.unwrap_or(1) == 0 {
        let _ = sqlx::query(
            "INSERT INTO voice_configs (name, repo_id, added_at) \
             VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        )
        .bind(DEFAULT_VOICE.0)
        .bind(DEFAULT_VOICE.1)
        .execute(pool)
        .await;
    }
}
