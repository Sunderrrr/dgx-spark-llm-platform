use axum::http::HeaderMap;
use crate::db::SessionData;

/// Extract session from cookies
pub fn get_session_token(headers: &HeaderMap) -> Option<String> {
    // Look for "session" cookie
    headers
        .get("cookie")
        .and_then(|h| h.to_str().ok())
        .and_then(|cookies| {
            cookies
                .split(';')
                .find_map(|cookie| {
                    let trimmed = cookie.trim();
                    if trimmed.starts_with("session=") {
                        trimmed.strip_prefix("session=").map(|s| s.to_string())
                    } else {
                        None
                    }
                })
        })
}

/// Require authenticated session
pub async fn require_session(
    headers: &HeaderMap,
    db: &crate::db::Database,
) -> Result<SessionData, String> {
    let token = get_session_token(headers)
        .ok_or_else(|| "No session token".to_string())?;

    db.get_session(&token)
        .await
        .map_err(|e| format!("Database error: {}", e))?
        .ok_or_else(|| "Invalid or expired session".to_string())
}
