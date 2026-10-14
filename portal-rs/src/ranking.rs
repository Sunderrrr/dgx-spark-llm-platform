//! Pure figures of the leaderboard (page /ranking), ported from dgx-portal/ranking.py.
//!
//! Only the arithmetic lives here: the SpendLogs reads (LiteLLM Postgres) and the
//! route are not ported yet. Rules carried over, each one locked by a test:
//!   - the ranked figure depends on the metric (`_metric_value`);
//!   - an average (`per_request`) has no share of the total;
//!   - a row with no previous value has no delta (never a division by zero);
//!   - the reference cost is rounded to cents, per million tokens.

/// What a ranking can be based on (the order matches ranking.py).
pub const RANKING_METRICS: [&str; 5] = ["total", "completion", "prompt", "requests", "per_request"];

/// The figure we RANK on. `per_request` is the average size of a request.
pub fn metric_value(prompt: i64, completion: i64, metric: &str, requests: i64) -> f64 {
    let (p, c, n) = (prompt.max(0) as f64, completion.max(0) as f64, requests.max(0) as f64);
    match metric {
        "completion" => c,
        "prompt" => p,
        "requests" => n,
        "per_request" => {
            if n > 0.0 {
                (p + c) / n
            } else {
                0.0
            }
        }
        _ => p + c,
    }
}

/// Share of the period total, in percent. None for an average or an empty total.
pub fn share_pct(value: f64, total: f64, metric: &str) -> Option<f64> {
    if total == 0.0 || metric == "per_request" {
        None
    } else {
        Some(value / total * 100.0)
    }
}

/// Change against the previous period, in percent. None when there is nothing to
/// compare with (no previous value): a leftover or a new account has no delta.
pub fn delta_pct(value: f64, previous: f64) -> Option<f64> {
    if previous == 0.0 {
        None
    } else {
        Some((value - previous) / previous * 100.0)
    }
}

/// Reference price of the consumption, rounded to cents (never a bill).
/// Prices are per million tokens.
pub fn cost(prompt: i64, completion: i64, price_input: f64, price_output: f64) -> f64 {
    let raw = prompt.max(0) as f64 / 1e6 * price_input + completion.max(0) as f64 / 1e6 * price_output;
    (raw * 100.0).round() / 100.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn total_is_prompt_plus_completion() {
        assert_eq!(metric_value(300, 200, "total", 9), 500.0);
    }

    #[test]
    fn per_request_is_an_average_and_zero_without_requests() {
        assert_eq!(metric_value(300, 200, "per_request", 5), 100.0);
        assert_eq!(metric_value(300, 200, "per_request", 0), 0.0);
    }

    #[test]
    fn negative_inputs_never_count_negative() {
        assert_eq!(metric_value(-5, 10, "total", 0), 10.0);
    }

    #[test]
    fn average_has_no_share() {
        assert_eq!(share_pct(100.0, 400.0, "per_request"), None);
        assert_eq!(share_pct(100.0, 400.0, "total"), Some(25.0));
    }

    #[test]
    fn empty_total_has_no_share() {
        assert_eq!(share_pct(0.0, 0.0, "total"), None);
    }

    #[test]
    fn no_previous_value_means_no_delta() {
        assert_eq!(delta_pct(50.0, 0.0), None);
        assert_eq!(delta_pct(150.0, 100.0), Some(50.0));
        assert_eq!(delta_pct(50.0, 100.0), Some(-50.0));
    }

    #[test]
    fn cost_is_per_million_and_rounded_to_cents() {
        // 1M prompt at 2.5 + 1M completion at 10 = 12.5
        assert_eq!(cost(1_000_000, 1_000_000, 2.5, 10.0), 12.5);
        assert_eq!(cost(1, 1, 2.5, 10.0), 0.0);
    }
}
