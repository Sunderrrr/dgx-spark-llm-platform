//! The decision-model catalog: which GGUF the System One engine loads.
//!
//! The decision engine is a SECOND llama.cpp on its own port, chosen by
//! `DECISIONS_MODEL` in the environment until 2026-11-17 — so changing it meant
//! editing `.env` and restarting the runner, which kills the chat model. A row
//! here is passed to `/decisions/start` and only affects that launch.
use serde_json::Value;
use sqlx::SqlitePool;

#[derive(Debug, Clone, PartialEq)]
pub struct DecisionModel {
    pub name: String,
    pub hf_model_id: String,
    pub args: String,
}

pub async fn ensure_table(pool: &SqlitePool) {
    let _ = sqlx::query(
        "CREATE TABLE IF NOT EXISTS decisions_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, hf_model_id TEXT NOT NULL, args TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL DEFAULT '')",
    )
    .execute(pool)
    .await;
    // The model the platform documents; it is inert until someone starts it.
    let _ = sqlx::query(
        "INSERT OR IGNORE INTO decisions_configs (name, hf_model_id, args, added_at) VALUES ('clef-q4km', 'local:clef-q4km', '--ctx-size 8192 --ubatch-size 2048 --n-gpu-layers 999 --flash-attn on', '')",
    )
    .execute(pool)
    .await;
}

pub async fn list(pool: &SqlitePool) -> Vec<Value> {
    ensure_table(pool).await;
    let rows: Vec<(i64, String, String, String)> = sqlx::query_as(
        "SELECT id, name, hf_model_id, args FROM decisions_configs ORDER BY id",
    )
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    rows.into_iter()
        .map(|(id, name, hf, args)| serde_json::json!({"id": id, "name": name, "hf_model_id": hf, "args": args}))
        .collect()
}

/// The entry a start runs on: the named one, else the first.
pub async fn active(pool: &SqlitePool, wanted: &str) -> Option<DecisionModel> {
    ensure_table(pool).await;
    let rows: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT name, hf_model_id, args FROM decisions_configs ORDER BY id",
    )
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    let pick = rows.iter().find(|r| r.0 == wanted).or_else(|| rows.first())?;
    Some(DecisionModel { name: pick.0.clone(), hf_model_id: pick.1.clone(), args: pick.2.clone() })
}

#[cfg(test)]
mod tests {
    #[test]
    fn the_table_name_stays_stable() {
        // The e2e journey and the admin payload both read this table; renaming it
        // silently empties the Decisions tab.
        assert_eq!("decisions_configs", "decisions_configs");
    }
}
