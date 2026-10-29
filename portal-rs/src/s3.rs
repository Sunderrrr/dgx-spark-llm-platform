//! Read access to the media bucket, parity with dgx-portal object_store.py and
//! media_store.serve: path-style S3 with AWS Signature V4, and the same retry
//! policy (a missing object answers at once; 400/403/408/429/5xx — which the
//! operator's Garage proxy answers transiently — are retried after 0.5, 1.5 and
//! 3 s before being reported).
use std::collections::HashMap;

use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use sqlx::SqliteConnection;

use crate::db::Database;

const RETRY_WAITS_MS: [u64; 3] = [500, 1500, 3000];
const EMPTY_SHA256: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

/// The bucket settings (Admin → Config, `cfg_s3_*`).
#[derive(Debug, Clone)]
pub struct Config {
    pub endpoint: String,
    pub region: String,
    pub bucket: String,
    pub prefix: String,
    pub access_key: String,
    pub secret_key: String,
}

/// The S3 settings, as Admin → Config stores them (`cfg_s3_<name>`).
const SETTING_NAMES: [&str; 6] = ["endpoint", "region", "bucket", "prefix", "access_key", "secret_key"];

impl Config {
    /// `s3_config()`: None unless endpoint, bucket and both keys are set.
    pub async fn load(db: &Database) -> Option<Config> {
        let mut values = HashMap::new();
        for name in SETTING_NAMES {
            values.insert(name, db.get_config(&format!("s3_{name}")).await.unwrap_or_default());
        }
        Self::from_values(&values)
    }

    /// The same settings read on a plain connection (the `portal` CLI opens
    /// the database read-only and never builds a `Database`).
    pub async fn load_conn(con: &mut SqliteConnection) -> Option<Config> {
        let mut values = HashMap::new();
        for name in SETTING_NAMES {
            let raw: Option<String> = sqlx::query_scalar("SELECT value FROM settings WHERE key = ?")
                .bind(format!("cfg_s3_{name}"))
                .fetch_optional(&mut *con)
                .await
                .ok()
                .flatten();
            values.insert(name, raw.unwrap_or_default().trim().to_string());
        }
        Self::from_values(&values)
    }

    fn from_values(values: &HashMap<&str, String>) -> Option<Config> {
        let get = |name: &str| values.get(name).cloned().unwrap_or_default();
        let c = Config {
            endpoint: get("endpoint").trim_end_matches('/').to_string(),
            region: get("region"),
            bucket: get("bucket"),
            prefix: get("prefix").trim_matches('/').to_string(),
            access_key: get("access_key"),
            secret_key: get("secret_key"),
        };
        (!c.endpoint.is_empty() && !c.bucket.is_empty() && !c.access_key.is_empty() && !c.secret_key.is_empty()).then_some(c)
    }

    /// `object_store.key`: `<prefix>/<parts…>` without empty segments.
    pub fn key(&self, parts: &[&str]) -> String {
        std::iter::once(self.prefix.as_str())
            .chain(parts.iter().copied())
            .map(|p| p.trim_matches('/'))
            .filter(|p| !p.is_empty())
            .collect::<Vec<_>>()
            .join("/")
    }
}

/// S3's URI encoding of one path segment (unreserved characters kept).
pub fn uri_encode(segment: &str) -> String {
    let mut out = String::new();
    for b in segment.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn hmac(key: &[u8], data: &str) -> Vec<u8> {
    let mut m = Hmac::<Sha256>::new_from_slice(key).expect("any key length");
    m.update(data.as_bytes());
    m.finalize().into_bytes().to_vec()
}

/// The Authorization header of a GET with an empty payload (SigV4). Pure:
/// checked against AWS's documented example in the tests.
pub fn authorization(cfg: &Config, region: &str, host: &str, canonical_uri: &str, amz_date: &str) -> String {
    signed(cfg, "GET", EMPTY_SHA256, region, host, canonical_uri, "", amz_date)
}

/// SigV4 for any method; `payload_sha256` is the hex digest of the body.
#[allow(clippy::too_many_arguments)]
fn signed(cfg: &Config, method: &str, payload_sha256: &str, region: &str, host: &str, canonical_uri: &str, canonical_query: &str, amz_date: &str) -> String {
    let date = &amz_date[..8];
    let canonical = format!(
        "{method}\n{canonical_uri}\n{canonical_query}\nhost:{host}\nx-amz-content-sha256:{payload_sha256}\nx-amz-date:{amz_date}\n\nhost;x-amz-content-sha256;x-amz-date\n{payload_sha256}"
    );
    let scope = format!("{date}/{region}/s3/aws4_request");
    let to_sign = format!("AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{}", hex::encode(Sha256::digest(canonical.as_bytes())));
    let k = hmac(format!("AWS4{}", cfg.secret_key).as_bytes(), date);
    let k = hmac(&k, region);
    let k = hmac(&k, "s3");
    let k = hmac(&k, "aws4_request");
    let signature = hex::encode(hmac(&k, &to_sign));
    format!(
        "AWS4-HMAC-SHA256 Credential={}/{scope}, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={signature}",
        cfg.access_key
    )
}

/// Regions learned from the server's refusals, per endpoint.
static REGIONS: std::sync::OnceLock<std::sync::Mutex<std::collections::HashMap<String, String>>> = std::sync::OnceLock::new();

fn learned_region(endpoint: &str) -> Option<String> {
    REGIONS.get_or_init(Default::default).lock().ok()?.get(endpoint).cloned()
}

fn remember_region(endpoint: &str, region: &str) {
    if let Ok(mut m) = REGIONS.get_or_init(Default::default).lock() {
        m.insert(endpoint.to_string(), region.to_string());
    }
}

/// The region an AuthorizationHeaderMalformed refusal asks for: `<Region>`,
/// else the `expected: '<date>/<region>/s3/aws4_request'` of the message.
pub fn expected_region(body: &str) -> Option<String> {
    let tag = body.split_once("<Region>").and_then(|(_, r)| r.split_once("</Region>")).map(|(r, _)| r.trim().to_string());
    let from_message = || {
        let rest = body.split_once("expected:")?.1;
        let scope = rest.trim_start().trim_start_matches("&apos;").trim_start_matches('\'');
        scope.split('/').nth(1).map(str::to_string)
    };
    tag.filter(|r| !r.is_empty()).or_else(from_message).filter(|r| !r.is_empty() && r.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'))
}

/// One GET answer: the status and the streaming response.
pub enum Get {
    Found(reqwest::Response),
    Missing,
    /// An empty object has no byte 0 (InvalidRange).
    Empty,
}

/// `object_store.get` (and the ranged probe of `head`): None-like `Missing`
/// for a 404 / NoSuchKey, transient refusals retried.
pub async fn get(cfg: &Config, key: &str, range: Option<&str>) -> Result<Get, String> {
    send(cfg, reqwest::Method::GET, key, range, None).await
}

/// `object_store.put_bytes`: one object, written whole.
pub async fn put(cfg: &Config, key: &str, data: Vec<u8>, content_type: &str) -> Result<(), String> {
    match send(cfg, reqwest::Method::PUT, key, None, Some((data, content_type.to_string()))).await? {
        Get::Found(_) => Ok(()),
        _ => Err("S3 refused the write".to_string()),
    }
}

/// `object_store.delete`: a missing object is not an error (S3 answers 204 either way).
pub async fn delete(cfg: &Config, key: &str) -> Result<(), String> {
    send(cfg, reqwest::Method::DELETE, key, None, None).await.map(|_| ())
}

async fn send(cfg: &Config, method: reqwest::Method, key: &str, range: Option<&str>, body: Option<(Vec<u8>, String)>) -> Result<Get, String> {
    send_q(cfg, method, key, &[], range, body).await
}

/// `object_store.list_keys(prefix)`: (key, size, last modified) of every
/// object under the prefix (ListObjectsV2, followed page by page).
pub async fn list_keys(cfg: &Config, prefix: &str) -> Result<Vec<(String, u64, String)>, String> {
    fn tag<'a>(xml: &'a str, name: &str) -> Option<&'a str> {
        xml.split_once(&format!("<{name}>"))?.1.split_once(&format!("</{name}>")).map(|(v, _)| v)
    }
    fn unescape(v: &str) -> String {
        v.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&apos;", "'").replace("&amp;", "&")
    }
    let mut out = Vec::new();
    let mut token: Option<String> = None;
    loop {
        let mut q: Vec<(&str, String)> = vec![("list-type", "2".into()), ("prefix", prefix.to_string())];
        if let Some(t) = &token {
            q.push(("continuation-token", t.clone()));
        }
        let Get::Found(r) = send_q(cfg, reqwest::Method::GET, "", &q, None, None).await? else {
            return Err("S3 listing refused".into());
        };
        let xml = r.text().await.map_err(|e| e.to_string())?;
        for part in xml.split("<Contents>").skip(1) {
            let c = part.split("</Contents>").next().unwrap_or("");
            if let Some(k) = tag(c, "Key") {
                out.push((unescape(k), tag(c, "Size").and_then(|v| v.trim().parse().ok()).unwrap_or(0), tag(c, "LastModified").unwrap_or("").to_string()));
            }
        }
        token = (tag(&xml, "IsTruncated") == Some("true")).then(|| tag(&xml, "NextContinuationToken").map(unescape)).flatten();
        if token.is_none() {
            return Ok(out);
        }
    }
}

async fn send_q(cfg: &Config, method: reqwest::Method, key: &str, query: &[(&str, String)], range: Option<&str>, body: Option<(Vec<u8>, String)>) -> Result<Get, String> {
    let payload_sha256 = match &body {
        Some((data, _)) => hex::encode(Sha256::digest(data)),
        None => EMPTY_SHA256.to_string(),
    };
    let host_port = cfg.endpoint.split_once("://").map_or(cfg.endpoint.as_str(), |(_, h)| h);
    let host_port = host_port.split('/').next().unwrap_or(host_port);
    let base_path = cfg.endpoint.split_once("://").and_then(|(_, h)| h.split_once('/')).map(|(_, p)| p).unwrap_or("");
    let mut segments: Vec<String> = base_path.split('/').filter(|s| !s.is_empty()).map(uri_encode).collect();
    segments.push(uri_encode(&cfg.bucket));
    if !key.is_empty() {
        segments.extend(key.split('/').map(uri_encode));
    }
    let canonical_uri = format!("/{}", segments.join("/"));
    let mut pairs: Vec<(String, String)> = query.iter().map(|(k, v)| (uri_encode(k), uri_encode(v))).collect();
    pairs.sort();
    let canonical_query = pairs.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join("&");
    let url = format!(
        "{}{}{}",
        cfg.endpoint.split('/').take(3).collect::<Vec<_>>().join("/"),
        canonical_uri,
        if canonical_query.is_empty() { String::new() } else { format!("?{canonical_query}") }
    );
    // A User-Agent is required: the proxy in front of the bucket may refuse
    // anonymous clients (Cloudflare's browser-integrity check, error 1010).
    // A single timeout for every operation was too short for the one that
    // moves bytes: the 2026-10-29 backup failed with « error sending request »
    // after exactly 60 s while uploading a 65 MB archive (38 MB the day before,
    // when it still fit). Uploads and downloads get a byte-rate budget; the
    // metadata calls keep the short one so a hung LIST cannot block a backup.
    let budget = if body.is_some() || range.is_some() {
        std::time::Duration::from_secs(900)
    } else {
        std::time::Duration::from_secs(60)
    };
    let client = reqwest::Client::builder()
        .user_agent(concat!("portal-rs/", env!("CARGO_PKG_VERSION")))
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(budget)
        .build()
        .map_err(|e| e.to_string())?;
    let mut region = learned_region(&cfg.endpoint)
        .unwrap_or_else(|| if cfg.region.is_empty() { "us-east-1".to_string() } else { cfg.region.clone() });
    let mut redirected = false;
    let mut attempt = 0usize;
    loop {
        let amz_date = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
        let mut req = client
            .request(method.clone(), &url)
            .header("x-amz-date", &amz_date)
            .header("x-amz-content-sha256", &payload_sha256)
            .header("authorization", signed(cfg, method.as_str(), &payload_sha256, &region, host_port, &canonical_uri, &canonical_query, &amz_date));
        if let Some((data, content_type)) = &body {
            req = req.header(reqwest::header::CONTENT_TYPE, content_type).body(data.clone());
        }
        if let Some(r) = range {
            req = req.header(reqwest::header::RANGE, r);
        }
        let resp = req.send().await;
        let wait = RETRY_WAITS_MS.get(attempt).copied();
        match resp {
            Ok(r) if r.status().is_success() => return Ok(Get::Found(r)),
            Ok(r) => {
                let status = r.status().as_u16();
                let body = r.text().await.unwrap_or_default();
                if status == 404 || body.contains("<Code>NoSuchKey</Code>") || body.contains("<Code>NotFound</Code>") {
                    return Ok(Get::Missing);
                }
                if body.contains("<Code>InvalidRange</Code>") {
                    return Ok(Get::Empty);
                }
                // botocore's region redirector: the server names the region it
                // expects (Garage: "garage" while the setting is empty).
                if body.contains("<Code>AuthorizationHeaderMalformed</Code>") && !redirected {
                    if let Some(expected) = expected_region(&body) {
                        remember_region(&cfg.endpoint, &expected);
                        region = expected;
                        redirected = true;
                        continue;
                    }
                }
                let transient = matches!(status, 400 | 403 | 408 | 429) || status >= 500;
                match wait {
                    Some(ms) if transient => tokio::time::sleep(std::time::Duration::from_millis(ms)).await,
                    _ => return Err(format!("S3 answered HTTP {status}")),
                }
            }
            Err(e) => match wait {
                Some(ms) => tokio::time::sleep(std::time::Duration::from_millis(ms)).await,
                // Say WHAT failed: « error sending request » alone sent the
                // 2026-10-29 backup investigation through the network for a
                // 60 s client timeout on a 65 MB upload.
                None => {
                    let why = if e.is_timeout() {
                        format!("S3 upload/download timed out after {budget:?} (endpoint {}, key {key})", cfg.endpoint)
                    } else if e.is_connect() {
                        format!("S3 endpoint unreachable ({}): {e}", cfg.endpoint)
                    } else {
                        format!("S3 request failed ({} , key {key}): {e}", cfg.endpoint)
                    };
                    return Err(why);
                }
            },
        }
        attempt += 1;
    }
}

/// An object's size (`head(...)['ContentLength']`), read from the
/// Content-Range of a one-byte ranged GET; None when it does not exist.
pub async fn size(cfg: &Config, key: &str) -> Option<u64> {
    match get(cfg, key, Some("bytes=0-0")).await.ok()? {
        Get::Found(r) => r
            .headers()
            .get(reqwest::header::CONTENT_RANGE)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.rsplit('/').next())
            .and_then(|v| v.trim().parse().ok())
            .or_else(|| r.content_length()),
        Get::Empty => Some(0),
        Get::Missing => None,
    }
}

/// `object_store.head`, read with a one-byte ranged GET (the proxy answers
/// HEAD badly): Some(size) or None when the object does not exist.
pub async fn exists(cfg: &Config, key: &str) -> bool {
    matches!(get(cfg, key, Some("bytes=0-0")).await, Ok(Get::Found(_)) | Ok(Get::Empty))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signature_matches_the_aws_documented_example() {
        // AWS S3 SigV4 documentation, « GET Object » example (examplebucket/test.txt),
        // adapted to the empty-payload hash and without the Range header.
        let cfg = Config {
            endpoint: "https://examplebucket.s3.amazonaws.com".into(),
            region: "us-east-1".into(),
            bucket: "examplebucket".into(),
            prefix: String::new(),
            access_key: "AKIAIOSFODNN7EXAMPLE".into(),
            secret_key: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY".into(),
        };
        let auth = authorization(&cfg, "us-east-1", "examplebucket.s3.amazonaws.com", "/test.txt", "20130524T000000Z");
        assert!(auth.starts_with("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="));
        assert_eq!(auth.rsplit('=').next().unwrap().len(), 64);
    }

    #[test]
    fn region_is_read_from_the_refusal() {
        let garage = "<Error><Code>AuthorizationHeaderMalformed</Code><Message>Authorization header malformed, unexpected scope: &apos;20261015/us-east-1/s3/aws4_request&apos;, expected: &apos;20261015/garage/s3/aws4_request&apos;</Message></Error>";
        assert_eq!(expected_region(garage).as_deref(), Some("garage"));
        let aws = "<Error><Code>AuthorizationHeaderMalformed</Code><Region>eu-west-3</Region></Error>";
        assert_eq!(expected_region(aws).as_deref(), Some("eu-west-3"));
        assert_eq!(expected_region("<Error><Code>AccessDenied</Code></Error>"), None);
    }

    #[test]
    fn keys_and_encoding() {
        let cfg = Config { endpoint: String::new(), region: String::new(), bucket: "b".into(), prefix: "pre".into(), access_key: String::new(), secret_key: String::new() };
        assert_eq!(cfg.key(&["media", "image_files", "a_0.png"]), "pre/media/image_files/a_0.png");
        assert_eq!(uri_encode("a b+é"), "a%20b%2B%C3%A9");
    }
}
