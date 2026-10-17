//! The budget-grant reaper, parity with dgx-portal admin_users.py
//! (`revert_expired_grants`, started by app.py `_start_grant_reaper`).
//!
//! A temporary grant (`budget_grants`) lives on the LiteLLM side: without this
//! sweep, a grant of 3 days would stay the limit forever. Every 60 s, the
//! accounts whose deadline has passed go back to their base cap — unless an
//! admin changed the cap in the meantime, in which case the grant is simply
//! forgotten and the admin's decision is kept. The first pass runs at startup,
//! so a deadline missed during an outage is caught up within the minute.
use std::time::Duration;

use sqlx::SqlitePool;

use crate::handlers::admin_budget::{key_duration, update_envelope};
use crate::keys;
use crate::AppState;

/// `_start_grant_reaper`: the wake-up period of the sweep.
pub const PERIOD_S: u64 = 60;

/// The sweep's decision, pure: the cap still reads as the one the grant set
/// (`abs(current - current_budget) < 1`), or LiteLLM does not say (None). An
/// unreadable envelope counts as untouched, as in the Flask copy: the update
/// that follows is then the one that decides, and it retries on failure.
pub fn cap_untouched(litellm_max_budget: Option<f64>, granted_current: f64) -> bool {
    match litellm_max_budget {
        None => true,
        Some(current) => (current - granted_current).abs() < 1.0,
    }
}

/// Spawns the reaper. Called from main.rs unless `DGX_NO_REAPER=1`, the test
/// seam shared with the Flask reapers.
pub fn spawn(state: AppState) {
    tokio::spawn(async move {
        loop {
            match revert_expired(&state).await {
                Ok(0) => {}
                Ok(n) => tracing::info!("budget grants reverted to their base cap: {n} account(s)"),
                Err(e) => tracing::error!("reaper budget_grants: {e}"),
            }
            tokio::time::sleep(Duration::from_secs(PERIOD_S)).await;
        }
    });
}

/// `revert_expired_grants()`: returns the number of accounts brought back to
/// their base cap. A row is deleted only once its outcome is settled; a failed
/// LiteLLM update keeps it for the next pass (it carries the deadline and the
/// base cap).
pub async fn revert_expired(state: &AppState) -> Result<usize, sqlx::Error> {
    let pool: &SqlitePool = state.db.pool();
    let now = keys::iso(&chrono::Utc::now().naive_utc());
    let rows: Vec<(i64, String, f64, f64)> = sqlx::query_as(
        "SELECT id, username, CAST(base_budget AS REAL), CAST(current_budget AS REAL) FROM budget_grants WHERE expires_at <= ?",
    )
    .bind(&now)
    .fetch_all(pool)
    .await?;
    if rows.is_empty() {
        return Ok(0);
    }
    let grant_duration = key_duration(state).await;
    let mut brought_back = 0;
    for (id, username, base_budget, granted_current) in rows {
        let info = keys::account_info(&username).await;
        if cap_untouched(info.max_budget, granted_current) {
            if !update_envelope(&username, base_budget, &grant_duration).await {
                crate::sessions::log_audit(
                    pool,
                    "reaper",
                    "budget.grant_revert_echec",
                    &format!("{username} : retour au plafond de base non appliqué (LiteLLM injoignable ?), nouvelle tentative au prochain passage"),
                )
                .await;
                continue;
            }
            brought_back += 1;
        }
        // Otherwise an admin intervened meanwhile: the grant is forgotten and
        // their decision is NOT overwritten.
        sqlx::query("DELETE FROM budget_grants WHERE id=?").bind(id).execute(pool).await?;
    }
    Ok(brought_back)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn untouched_cap_is_brought_back() {
        assert!(cap_untouched(Some(500.0), 500.0));
        assert!(cap_untouched(Some(500.4), 500.0));
    }

    #[test]
    fn a_cap_the_admin_moved_is_kept() {
        assert!(!cap_untouched(Some(800.0), 500.0));
        assert!(!cap_untouched(Some(499.0), 500.0));
    }

    #[test]
    fn unknown_cap_counts_as_untouched() {
        assert!(cap_untouched(None, 500.0));
    }
}
