//! Minimal MCP client (dgx-portal mcp_client.py): JSON-RPC 2.0 over HTTP POST
//! (« Streamable HTTP »), only `initialize` and `tools/list` — what registering
//! a server needs. Every request goes through the SSRF guard: https only, every
//! resolved address public, the first one PINNED for the connection, and no
//! redirect followed.
use serde_json::{json, Value};

use crate::skillstore;

/// Why a call failed: a message the server (or the guard) gave, or a transport failure.
#[derive(Debug)]
pub enum McpError {
    /// `MCPError`: shown to the user after « Connexion au serveur MCP impossible : ».
    Said(String),
    /// Any other failure (connection, timeout): shown without detail.
    Transport,
}

pub struct Client {
    url: String,
    auth: Option<String>,
    ldap_host: String,
    session: Option<String>,
}

impl Client {
    pub fn new(url: &str, auth: Option<&str>, ldap_host: &str) -> Client {
        Client { url: url.to_string(), auth: auth.map(str::to_string), ldap_host: ldap_host.to_string(), session: None }
    }

    /// `_post`: resolve and validate once, then connect to that very address.
    async fn post(&self, payload: &Value) -> Result<reqwest::Response, McpError> {
        let (host, addr) = skillstore::validated_target(&self.url, &self.ldap_host).await.map_err(McpError::Said)?;
        let client = reqwest::Client::builder()
            .resolve(&host, addr)
            .redirect(reqwest::redirect::Policy::none())
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .map_err(|_| McpError::Transport)?;
        let mut req = client
            .post(&self.url)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .header(reqwest::header::ACCEPT, "application/json, text/event-stream")
            .json(payload);
        if let Some(a) = &self.auth {
            req = req.header(reqwest::header::AUTHORIZATION, a);
        }
        if let Some(s) = &self.session {
            req = req.header("Mcp-Session-Id", s);
        }
        req.send().await.map_err(|_| McpError::Transport)
    }

    /// `_rpc`: one call; the `result`, or why not.
    async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, McpError> {
        let id = uuid::Uuid::new_v4().to_string();
        let r = self.post(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})).await?;
        let status = r.status().as_u16();
        if matches!(status, 301 | 302 | 303 | 307 | 308) && r.headers().contains_key(reqwest::header::LOCATION) {
            return Err(McpError::Said("Le serveur MCP a répondu par une redirection, refusée.".into()));
        }
        if status >= 400 {
            return Err(McpError::Said(format!("Le serveur MCP a renvoyé une erreur ({status}).")));
        }
        if let Some(sid) = r.headers().get("Mcp-Session-Id").and_then(|v| v.to_str().ok()).filter(|s| !s.is_empty()) {
            self.session = Some(sid.to_string());
        }
        let sse = r.headers().get(reqwest::header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).is_some_and(|c| c.contains("text/event-stream"));
        let text = r.text().await.map_err(|_| McpError::Transport)?;
        let data: Value = if sse {
            let first = text.lines().filter_map(|l| l.strip_prefix("data:")).find_map(|d| serde_json::from_str::<Value>(d.trim()).ok());
            first.ok_or_else(|| McpError::Said("Réponse SSE du serveur MCP illisible.".into()))?
        } else {
            serde_json::from_str(&text).map_err(|_| McpError::Said("Réponse du serveur MCP illisible.".into()))?
        };
        if let Some(e) = data.get("error") {
            let msg = e.get("message").map(|m| m.as_str().map(str::to_string).unwrap_or_else(|| m.to_string())).unwrap_or_else(|| "Erreur MCP.".into());
            return Err(McpError::Said(msg));
        }
        Ok(data.get("result").cloned().unwrap_or_else(|| json!({})))
    }

    /// `initialize`, then the `initialized` notification (best-effort).
    pub async fn initialize(&mut self) -> Result<Value, McpError> {
        let result = self.rpc("initialize", json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "dgx-support", "version": "1.0"}})).await?;
        let _ = self.post(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"})).await;
        Ok(result)
    }

    pub async fn list_tools(&mut self) -> Result<Vec<Value>, McpError> {
        Ok(self.rpc("tools/list", json!({})).await?.get("tools").and_then(Value::as_array).cloned().unwrap_or_default())
    }
}

impl Client {
    /// `call_tool`: the text parts joined, and whether the server reported success.
    pub async fn call_tool(&mut self, name: &str, arguments: &Value) -> Result<(String, bool), McpError> {
        let result = self.rpc("tools/call", json!({"name": name, "arguments": arguments})).await?;
        let parts: Vec<String> = result
            .get("content")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter(|c| c.get("type").and_then(Value::as_str) == Some("text")).map(|c| c.get("text").and_then(Value::as_str).unwrap_or("").to_string()).filter(|t| !t.is_empty()).collect())
            .unwrap_or_default();
        let text = if parts.is_empty() { "(réponse vide du serveur MCP)".to_string() } else { parts.join("\n") };
        Ok((text, !result.get("isError").is_some_and(crate::keys::truthy)))
    }
}

/// Per-server tools, cached 120 s — failures too (a dead server must not cost
/// its timeouts on every message).
static TOOLS_CACHE: std::sync::Mutex<Option<std::collections::HashMap<i64, (std::time::Instant, Vec<Value>)>>> = std::sync::Mutex::new(None);

/// `list_tools_cached`.
pub async fn list_tools_cached(server_id: i64, url: &str, auth: Option<&str>, ldap_host: &str) -> Vec<Value> {
    if let Some((at, tools)) = TOOLS_CACHE.lock().unwrap_or_else(|e| e.into_inner()).as_ref().and_then(|m| m.get(&server_id)).cloned() {
        if at.elapsed() < std::time::Duration::from_secs(120) {
            return tools;
        }
    }
    let mut c = Client::new(url, auth, ldap_host);
    let tools = match c.initialize().await {
        Ok(_) => c.list_tools().await.unwrap_or_default(),
        Err(_) => Vec::new(),
    };
    TOOLS_CACHE.lock().unwrap_or_else(|e| e.into_inner()).get_or_insert_with(Default::default).insert(server_id, (std::time::Instant::now(), tools.clone()));
    tools
}
