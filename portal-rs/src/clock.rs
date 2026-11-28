//! Wall-clock seconds since the Unix epoch.
//!
//! Every stored timestamp, rate window, lock and session age in the portal is a
//! double of epoch seconds, as Flask wrote them, so all of them must read the
//! SAME clock. Fourteen modules each had their own byte-identical copy (`now`,
//! `now_s`, `now_epoch`, `epoch_now`, `now_f`) — that is one definition, here.
//!
//! A clock that predates 1970 has no meaningful value; 0.0 keeps the arithmetic
//! finite, which is what every former copy did.
use std::time::{SystemTime, UNIX_EPOCH};

/// Seconds since the Unix epoch, as a double.
pub fn now_s() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}
