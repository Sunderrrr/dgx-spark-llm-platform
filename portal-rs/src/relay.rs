//! The admin relay: routes whose ADMIN branch could not be verified against
//! Flask (no admin session may be used for tests) are served by Rust for every
//! other account, and handed to Flask unchanged for an admin session. The
//! request goes through as it came (method, path, query, cookie, CSRF, body)
//! and Flask's answer comes back streamed, so SSE keeps flowing.
use axum::body::Body;
use axum::extract::Request;
use axum::http::{HeaderName, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};

use crate::AppState;

/// Paths (and their sub-paths) whose admin sessions stay on Flask.
pub const ADMIN_TO_FLASK: &[&str] = &["/api/home", "/system/stats", "/api/ranking", "/support/chat", "/support/confirm"];

fn covered(path: &str) -> bool {
    ADMIN_TO_FLASK.iter().any(|p| path == *p || path.starts_with(&format!("{p}/")))
}

fn flask_url() -> String {
    std::env::var("FLASK_URL").unwrap_or_else(|_| "http://dgx-portal:5000".to_string()).trim_end_matches('/').to_string()
}

const FORWARDED_REQUEST: &[&str] = &["cookie", "content-type", "x-csrftoken", "accept", "user-agent", "x-forwarded-for", "x-forwarded-proto", "x-real-ip", "cf-connecting-ip", "range"];
const FORWARDED_RESPONSE: &[&str] = &["content-type", "cache-control", "x-accel-buffering", "content-disposition", "set-cookie", "location", "content-range", "accept-ranges", "x-sample-rate"];

/// Hands the request to Flask and streams its answer back.
pub async fn to_flask(req: Request) -> Response {
    let (parts, body) = req.into_parts();
    let path_q = parts.uri.path_and_query().map_or(parts.uri.path(), |p| p.as_str()).to_string();
    let Ok(bytes) = axum::body::to_bytes(body, 64 * 1024 * 1024).await else { return StatusCode::PAYLOAD_TOO_LARGE.into_response() };
    let Ok(client) = reqwest::Client::builder().connect_timeout(std::time::Duration::from_secs(10)).redirect(reqwest::redirect::Policy::none()).build() else {
        return crate::auth::html_status(StatusCode::SERVICE_UNAVAILABLE);
    };
    let Ok(method) = reqwest::Method::from_bytes(parts.method.as_str().as_bytes()) else { return StatusCode::METHOD_NOT_ALLOWED.into_response() };
    let mut out = client.request(method, format!("{}{path_q}", flask_url())).body(bytes);
    for name in FORWARDED_REQUEST {
        for v in parts.headers.get_all(*name) {
            out = out.header(*name, v.as_bytes());
        }
    }
    let up = match out.send().await {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[relay] Flask unreachable for {path_q}: {e}");
            return crate::auth::html_status(StatusCode::SERVICE_UNAVAILABLE);
        }
    };
    let mut builder = axum::http::Response::builder().status(StatusCode::from_u16(up.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY));
    for name in FORWARDED_RESPONSE {
        for v in up.headers().get_all(*name) {
            if let Ok(n) = HeaderName::from_bytes(name.as_bytes()) {
                builder = builder.header(n, v.as_bytes());
            }
        }
    }
    builder.body(Body::from_stream(up.bytes_stream())).unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response())
}

/// Middleware: an admin session on a covered path goes to Flask.
pub async fn admin_layer(axum::extract::State(state): axum::extract::State<AppState>, req: Request, next: Next) -> Response {
    if covered(req.uri().path()) {
        if let Ok(session) = crate::auth::require_session(req.headers(), &state.db).await {
            if session.is_admin {
                return to_flask(req).await;
            }
        }
    }
    next.run(req).await
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fake Flask: echoes what it received, streams its answer in pieces.
    #[tokio::test]
    async fn the_request_goes_through_and_the_answer_streams_back() {
        use axum::routing::any;
        let app = axum::Router::new().route("/support/chat", any(|req: Request| async move {
            let (p, b) = req.into_parts();
            let body = axum::body::to_bytes(b, 1 << 20).await.unwrap();
            let echo = format!("{} {} cookie={:?} csrf={:?} body={}", p.method, p.uri, p.headers.get("cookie"), p.headers.get("x-csrftoken"), String::from_utf8_lossy(&body));
            let chunks = futures_like(vec![": open\n\n".to_string(), format!("data: {echo}\n\n")]);
            (StatusCode::CREATED, [("content-type", "text/event-stream"), ("x-accel-buffering", "no"), ("x-secret", "dropped")], Body::from_stream(chunks))
        }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        std::env::set_var("FLASK_URL", format!("http://{addr}"));
        let req = axum::http::Request::builder().method("POST").uri("/support/chat?x=1")
            .header("cookie", "session=abc").header("x-csrftoken", "tok").header("content-type", "application/json")
            .body(Body::from("{\"m\":1}")).unwrap();
        let resp = to_flask(req).await;
        assert_eq!(resp.status(), StatusCode::CREATED);
        assert_eq!(resp.headers()["content-type"], "text/event-stream");
        assert_eq!(resp.headers()["x-accel-buffering"], "no");
        assert!(resp.headers().get("x-secret").is_none());
        let body = String::from_utf8(axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap().to_vec()).unwrap();
        assert!(body.starts_with(": open\n\n"), "{body}");
        assert!(body.contains(r#"POST /support/chat?x=1 cookie=Some("session=abc") csrf=Some("tok") body={"m":1}"#), "{body}");
    }

    fn futures_like(parts: Vec<String>) -> impl tokio_stream::Stream<Item = Result<axum::body::Bytes, std::convert::Infallible>> {
        tokio_stream::iter(parts.into_iter().map(|p| Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(p))))
    }

    #[test]
    fn covered_paths() {
        assert!(covered("/api/home") && covered("/api/ranking/user/x") && covered("/support/chat"));
        assert!(!covered("/api/homes") && !covered("/api/settings") && !covered("/support/feedback"));
    }
}
