//! The playground chat, ported from chat_routes.py and the modules it drives.
//! Built piece by piece; the route moves to Rust once every piece is there.
pub mod gauges;
pub mod history;
pub mod memory_turn;
pub mod reasoning;
pub mod tools;
pub mod triggers;

use std::convert::Infallible;

/// The SSE stream being written: a sink of text frames. `send` is false once
/// the client is gone.
#[derive(Clone)]
pub struct Out(pub tokio::sync::mpsc::Sender<Result<axum::body::Bytes, Infallible>>);

impl Out {
    pub async fn send(&self, frame: impl Into<String>) -> bool {
        self.0.send(Ok(axum::body::Bytes::from(frame.into()))).await.is_ok()
    }
}

/// `data: {json}\n\n` with Python's json.dumps defaults (ASCII-escaped).
pub fn data_frame(v: &serde_json::Value) -> String {
    format!("data: {}\n\n", crate::handlers::py_dumps_ascii(v))
}
pub mod phase;
pub mod playground;
