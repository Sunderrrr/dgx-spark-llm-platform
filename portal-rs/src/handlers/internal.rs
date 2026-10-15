//! `/internal/authcheck` (admin_routes.py): Traefik's forwardAuth on the public
//! API, asked for EVERY external request while maintenance mode is on. Outside
//! maintenance it answers 200 at once; inside, an admin's API key passes and
//! anything else gets the maintenance message. Only the proxy may ask: from any
//! other peer the answer is the same refusal, so the 200/503 difference is no
//! key-validity oracle.
use super::*;
use super::admin_groups::{local_is_admin, Local};
use sqlx::SqlitePool;

fn maintenance_refusal() -> Response {
    let body = json!({"error": {
        "message": "Mode maintenance en cours — l'API est temporairement indisponible, réessaie plus tard.",
        "type": "maintenance_mode",
    }});
    (StatusCode::SERVICE_UNAVAILABLE, Json(body)).into_response()
}

/// The Traefik bridge (172.19.0.0/16) and the loopback.
fn from_proxy(peer: Option<std::net::IpAddr>) -> bool {
    match peer {
        Some(std::net::IpAddr::V4(ip)) => {
            let o = ip.octets();
            (o[0] == 172 && o[1] == 19) || o[0] == 127
        }
        _ => false,
    }
}

/// `is_admin_username(username)`: a managed local account's admin right first,
/// then (not blocked) the owner, else the role of its last directory login.
pub(crate) async fn is_admin_username(pool: &SqlitePool, username: &str) -> bool {
    let local: Option<(Option<i64>, Option<String>, Option<i64>)> =
        sqlx::query_as("SELECT is_admin, group_name, enabled FROM local_users WHERE username=?").bind(username).fetch_optional(pool).await.ok().flatten();
    let blocked = sqlx::query_scalar::<_, i64>("SELECT 1 FROM blocked_users WHERE username=?").bind(username).fetch_optional(pool).await.ok().flatten().is_some();
    if let Some((own, group, enabled)) = local {
        if !blocked && enabled.unwrap_or(0) != 0 {
            let row = Local { username: username.to_string(), is_admin: own.unwrap_or(0) != 0, group, max_budget: None };
            if local_is_admin(pool, &row).await {
                return true;
            }
        }
    }
    if blocked {
        return false;
    }
    let owner: Option<String> = sqlx::query_scalar("SELECT value FROM settings WHERE key='owner_username'").fetch_optional(pool).await.ok().flatten();
    if owner.filter(|o| !o.is_empty()).as_deref() == Some(username) {
        return true;
    }
    let last: Option<Option<i64>> = sqlx::query_scalar("SELECT last_is_admin FROM user_sources WHERE username=?").bind(username).fetch_optional(pool).await.ok().flatten();
    last.flatten().unwrap_or(0) != 0
}

/// GET /internal/authcheck
pub async fn internal_authcheck(State(state): State<AppState>, connect: Option<axum::extract::ConnectInfo<std::net::SocketAddr>>, headers: HeaderMap) -> Response {
    let pool = state.db.pool();
    if !state.db.maintenance_active().await {
        return (StatusCode::OK, "").into_response();
    }
    if !from_proxy(connect.map(|c| c.0.ip())) {
        return maintenance_refusal();
    }
    let token = headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .filter(|a| a.len() > 7 && a[..7].eq_ignore_ascii_case("bearer "))
        .map(|a| a[7..].to_string())
        .unwrap_or_default();
    if !token.is_empty() {
        let owner: Option<String> = sqlx::query_scalar("SELECT username FROM api_keys WHERE key_value=?").bind(&token).fetch_optional(pool).await.ok().flatten();
        if let Some(user) = owner {
            if is_admin_username(pool, &user).await {
                return (StatusCode::OK, "").into_response();
            }
        }
    }
    maintenance_refusal()
}
