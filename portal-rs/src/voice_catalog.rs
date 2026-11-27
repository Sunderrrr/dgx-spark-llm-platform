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

/// The voices seeded on an empty catalog: the fine-tune (best quality) and the
/// light clone (0.6B base + a reference clip, a third of the memory). The 0.6B id
/// is in the runner's map since before this change, so it launches without any
/// runner restart; `custom-qwen-tts` needs the rebuilt runner.
const DEFAULT_VOICES: [(&str, &str); 2] = [
    ("Ma voix (0.6B, clonée)", "Qwen3-TTS-12Hz-0.6B-Base"),
    ("Ma voix (fine-tuné 1.7B)", "custom-qwen-tts"),
];

/// The table, created on first use like the other sidecar catalogs, and seeded.
pub async fn ensure_table(pool: &SqlitePool) {
    if let Err(e) = sqlx::query(
        "CREATE TABLE IF NOT EXISTS voice_configs (\
           id INTEGER PRIMARY KEY AUTOINCREMENT, \
           name TEXT NOT NULL UNIQUE, \
           repo_id TEXT NOT NULL, \
           added_at TEXT NOT NULL)",
    )
    .execute(pool)
    .await
    {
        // Loud: without the table the page shows « aucun modèle » and nothing
        // else would say why.
        eprintln!("[voice_catalog] cannot create voice_configs: {e}");
        return;
    }
    // Seed ONLY an empty catalog. `INSERT OR IGNORE` alone was not enough: a
    // deleted entry came back on the next page load, because the unique name no
    // longer existed — the operator's choice was undone every time (Kody, PR 17).
    let empty: i64 = match sqlx::query_scalar("SELECT COUNT(*) FROM voice_configs").fetch_one(pool).await {
        Ok(n) => n,
        Err(e) => {
            eprintln!("[voice_catalog] cannot count voice_configs: {e}");
            return;
        }
    };
    if empty == 0 {
        for (name, repo_id) in DEFAULT_VOICES {
            // OR IGNORE keeps it idempotent if two starts race on an empty table
            // (the count check above narrows the window, it does not close it).
            if let Err(e) = sqlx::query(
                "INSERT OR IGNORE INTO voice_configs (name, repo_id, added_at) \
                 VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            )
            .bind(name)
            .bind(repo_id)
            .execute(pool)
            .await
            {
                eprintln!("[voice_catalog] cannot seed «{name}»: {e}");
            }
        }
    }
}
