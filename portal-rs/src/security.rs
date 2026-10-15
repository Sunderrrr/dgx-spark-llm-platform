//! Security headers on every answer, parity with dgx-portal/app.py
//! `_security_headers`: each one is set only if the route did not set it
//! (Python's `setdefault`), and an answer carrying its own sandbox policy
//! (the HTML preview) is left untouched — the portal's policy would make its
//! scripts inert again.
use axum::extract::Request;
use axum::http::{HeaderMap, HeaderName, HeaderValue};
use axum::middleware::Next;
use axum::response::Response;

pub const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; \
font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; \
form-action 'self'";

const HEADERS: [(&str, &str); 6] = [
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "same-origin"),
    ("content-security-policy", CSP),
    // `microphone` (dictation) and `camera` (photo capture) stay for 'self'.
    ("permissions-policy", "geolocation=(), payment=(), usb=(), camera=(self), microphone=(self)"),
    // Ignored over HTTP, applied behind Traefik's TLS.
    ("strict-transport-security", "max-age=63072000; includeSubDomains"),
];

/// Adds the missing security headers. Pure on the header map.
pub fn apply(headers: &mut HeaderMap) {
    let sandboxed = headers
        .get("content-security-policy")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("sandbox"));
    if sandboxed {
        return;
    }
    for (name, value) in HEADERS {
        let name = HeaderName::from_static(name);
        if !headers.contains_key(&name) {
            headers.insert(name, HeaderValue::from_static(value));
        }
    }
}

/// The middleware: run the route, then add what is missing.
pub async fn layer(req: Request, next: Next) -> Response {
    let mut resp = next.run(req).await;
    // The router's own 405 (a known path, another method) is empty; werkzeug's
    // carries its error page.
    if resp.status() == axum::http::StatusCode::METHOD_NOT_ALLOWED && !resp.headers().contains_key(axum::http::header::CONTENT_TYPE) {
        let allow = resp.headers().get(axum::http::header::ALLOW).cloned();
        resp = crate::auth::html_status(axum::http::StatusCode::METHOD_NOT_ALLOWED);
        if let Some(a) = allow {
            resp.headers_mut().insert(axum::http::header::ALLOW, a);
        }
    }
    apply(resp.headers_mut());
    resp
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_headers_are_added() {
        let mut h = HeaderMap::new();
        apply(&mut h);
        assert_eq!(h["x-content-type-options"], "nosniff");
        assert_eq!(h["content-security-policy"], CSP);
        assert_eq!(h.len(), 6);
    }

    #[test]
    fn a_route_s_own_value_wins() {
        let mut h = HeaderMap::new();
        h.insert("x-frame-options", HeaderValue::from_static("SAMEORIGIN"));
        apply(&mut h);
        assert_eq!(h["x-frame-options"], "SAMEORIGIN");
    }

    #[test]
    fn a_sandboxed_answer_is_left_alone() {
        let mut h = HeaderMap::new();
        h.insert("content-security-policy", HeaderValue::from_static("sandbox allow-scripts"));
        apply(&mut h);
        assert_eq!(h.len(), 1);
    }
}
