//! Pure figures of the leaderboard (page /ranking), ported from dgx-portal/ranking.py.
//!
//! The leaderboard reads LiteLLM's SpendLogs (Postgres, `spend.rs`) and the
//! portal's accounts (SQLite). Rules carried over, the arithmetic ones locked
//! by the tests below:
//!   - the ranked figure depends on the metric (`_metric_value`);
//!   - an average (`per_request`) has no share of the total;
//!   - a row with no previous value has no delta (never a division by zero);
//!   - the reference cost is rounded to cents, per million tokens.

use std::collections::{BTreeSet, HashMap, HashSet};

use chrono::{Datelike, NaiveDate};
use serde_json::{json, Value};
use sqlx::PgPool;

use crate::db::Database;
use crate::spend;

/// Periods and their labels (ranking.PERIOD_LABELS / PREV_LABELS).
pub const PERIODS: [&str; 5] = ["day", "week", "month", "year", "all"];
pub const NO_GROUP: &str = "Sans groupe";

pub fn period_label(period: &str) -> &'static str {
    match period {
        "week" => "7 derniers jours",
        "month" => "30 derniers jours",
        "year" => "12 derniers mois",
        "all" => "Depuis le début",
        _ => "Aujourd'hui",
    }
}

pub fn prev_label(period: &str) -> &'static str {
    match period {
        "week" => "la semaine précédente",
        "month" => "les 30 jours précédents",
        "year" => "les 12 mois précédents",
        "all" => "",
        _ => "hier",
    }
}

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
    round_to(raw, 2)
}

/// Reference prices per million tokens (ranking.prices): Admin → Config, else
/// the code defaults; a value that does not parse falls back to the default.
pub async fn prices(db: &Database) -> Value {
    async fn num(db: &Database, name: &str, default: f64, default_text: &str) -> f64 {
        let raw = db.get_config(name).await.unwrap_or_else(|| default_text.to_string());
        raw.replace(',', ".").parse::<f64>().map(|v| v.max(0.0)).unwrap_or(default)
    }
    let currency: String = db
        .get_config("ranking_price_currency")
        .await
        .unwrap_or_else(|| "USD".to_string())
        .trim()
        .chars()
        .take(8)
        .collect();
    json!({
        "input": num(db, "ranking_price_input", 2.5, "2.50").await,
        "output": num(db, "ranking_price_output", 10.0, "10.00").await,
        "currency": if currency.is_empty() { "USD".to_string() } else { currency },
    })
}

/// Python's round(x, n): the correctly rounded decimal of the exact binary
/// value, ties to even. Rust's `{:.n}` formatting does exactly that (checked
/// on 0.25, 2.675, 1.005 and the live averages), a multiply-and-round does not.
pub(crate) fn round_to(x: f64, digits: usize) -> f64 {
    format!("{x:.digits$}").parse().unwrap_or(x)
}

/// A map that keeps insertion order, like the Python dicts it replaces: ties in
/// a stable sort then come out in the same order on both backends.
#[derive(Default)]
struct Ordered<V> {
    keys: Vec<String>,
    index: HashMap<String, usize>,
    values: Vec<V>,
}

impl<V: Default> Ordered<V> {
    fn entry(&mut self, key: &str) -> &mut V {
        let i = match self.index.get(key) {
            Some(&i) => i,
            None => {
                self.keys.push(key.to_string());
                self.values.push(V::default());
                self.index.insert(key.to_string(), self.values.len() - 1);
                self.values.len() - 1
            }
        };
        &mut self.values[i]
    }
    fn get(&self, key: &str) -> Option<&V> {
        self.index.get(key).map(|&i| &self.values[i])
    }
    fn iter(&self) -> impl Iterator<Item = (&String, &V)> {
        self.keys.iter().zip(self.values.iter())
    }
}

#[derive(Default, Clone)]
struct Agg {
    prompt: i64,
    completion: i64,
    requests: i64,
    /// bucket → [prompt, completion, requests]
    spark: HashMap<String, [i64; 3]>,
    /// model → tokens, in first-seen order
    models: Vec<(String, i64)>,
    value: f64,
}

impl Agg {
    fn add_model(&mut self, model: &str, tokens: i64) {
        match self.models.iter_mut().find(|(m, _)| m == model) {
            Some((_, v)) => *v += tokens,
            None => self.models.push((model.to_string(), tokens)),
        }
    }
}

/// The period's window, computed by Postgres in local wall-clock time exactly
/// as ranking._window does with Python's aware datetimes.
struct Window {
    cur_utc: String,
    prev_utc: String,
    buckets: Vec<String>,
    bucket_sql: &'static str,
}

async fn window(pool: &PgPool, period: &str, tz: &str) -> Result<Window, sqlx::Error> {
    let (cur, prev) = match period {
        "week" => ("l - interval '7 days'", "l - interval '14 days'"),
        "month" => ("l - interval '30 days'", "l - interval '60 days'"),
        "year" => (
            "date_trunc('month', l) - interval '11 months'",
            "date_trunc('month', l) - interval '23 months'",
        ),
        "all" => (
            "date_trunc('month', COALESCE((SELECT MIN(\"startTime\") FROM \"LiteLLM_SpendLogs\")::date, l::date)::timestamp)",
            "date_trunc('month', COALESCE((SELECT MIN(\"startTime\") FROM \"LiteLLM_SpendLogs\")::date, l::date)::timestamp)",
        ),
        _ => ("date_trunc('day', l)", "date_trunc('day', l) - interval '1 day'"),
    };
    let sql = format!(
        "WITH n AS (SELECT (now() AT TIME ZONE $1) AS l), w AS (SELECT l, {cur} AS cur, {prev} AS prev FROM n) \
         SELECT ((cur AT TIME ZONE $1) AT TIME ZONE 'UTC')::text, ((prev AT TIME ZONE $1) AT TIME ZONE 'UTC')::text, \
         l::date::text, cur::date::text FROM w"
    );
    let (cur_utc, prev_utc, today, cur_date): (String, String, String, String) =
        sqlx::query_as(&sql).bind(tz).fetch_one(pool).await?;
    let parse = |d: &str| NaiveDate::parse_from_str(d, "%Y-%m-%d").unwrap_or_default();
    let today = parse(&today);
    let days = |n: i64| -> Vec<String> {
        (0..n).rev().map(|i| (today - chrono::Duration::days(i)).format("%Y-%m-%d").to_string()).collect()
    };
    let (buckets, bucket_sql) = match period {
        "week" => (days(7), "day"),
        "month" => (days(30), "day"),
        "year" | "all" => {
            let start = parse(&cur_date);
            let mut out = Vec::new();
            let (mut y, mut m) = (start.year(), start.month());
            while (y, m) <= (today.year(), today.month()) {
                out.push(format!("{y:04}-{m:02}-01"));
                m += 1;
                if m > 12 {
                    m = 1;
                    y += 1;
                }
            }
            (out, "month")
        }
        _ => ((0..24).map(|h| h.to_string()).collect(), "hour"),
    };
    let bucket_sql = match bucket_sql {
        "hour" => "EXTRACT(HOUR FROM ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE $1))::int",
        "month" => "date_trunc('month', ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE $1))::date",
        _ => "((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE $1)::date",
    };
    Ok(Window { cur_utc, prev_utc, buckets, bucket_sql })
}

/// Who a key belongs to, or None for the proxy's own keys (skipped).
fn owner_of(api_key: &Option<String>, umap: &HashMap<String, String>) -> Option<String> {
    match api_key {
        Some(k) if spend::NON_USER_KEYS.contains(&k.as_str()) => None,
        Some(k) => Some(umap.get(k).cloned().unwrap_or_else(|| "unknown".to_string())),
        // Python: None is not in _NON_USER_KEYS, umap.get(None) → 'unknown'.
        None => Some("unknown".to_string()),
    }
}

/// ranking.ranking_full: the ranking of a period, its total, average, models.
pub async fn ranking_full(
    db: &Database,
    pool: Option<&PgPool>,
    period: &str,
    me: Option<&str>,
    metric: &str,
    model: Option<&str>,
    by: &str,
) -> Value {
    let metric = if RANKING_METRICS.contains(&metric) { metric } else { "total" };
    let by = if by == "group" { "group" } else { "account" };
    let price = prices(db).await;
    let empty = json!({
        "period": period, "metric": metric, "rows": [], "active_count": 0, "total": 0, "avg": 0,
        "has_prev": period != "all", "models": [], "model": model, "by": by, "cost_total": 0,
        "prices": price,
    });
    let Some(pool) = pool else { return empty };
    match ranking_inner(db, pool, period, me, metric, model, by, &price).await {
        Ok(v) => v,
        // Any failure answers the empty ranking, as the Flask version does.
        Err(_) => empty,
    }
}

#[allow(clippy::too_many_arguments)]
async fn ranking_inner(
    db: &Database,
    pool: &PgPool,
    period: &str,
    me: Option<&str>,
    metric: &str,
    model: Option<&str>,
    by: &str,
    price: &Value,
) -> Result<Value, sqlx::Error> {
    let tz = spend::local_tz();
    let w = window(pool, period, &tz).await?;
    let umap = spend::key_user_map(pool).await;
    let known = db.known_accounts().await;
    let model_sql = if model.is_some() { " AND model_group = $3" } else { "" };

    // Current period, per bucket, key AND model.
    // The inner query is ranking.py's, verbatim: Postgres then aggregates in
    // the same order, and ties in the ranking come out as they do in Flask.
    // Only the outer select converts the types.
    let sql = format!(
        "SELECT b::text, api_key, model_group, p::bigint, c::bigint, n FROM (\
         SELECT {b} AS b, api_key, model_group, SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c, \
         COUNT(*) FILTER (WHERE status = 'success') AS n FROM \"LiteLLM_SpendLogs\" \
         WHERE \"startTime\" >= $2::timestamp{model_sql} GROUP BY b, api_key, model_group) s",
        b = w.bucket_sql
    );
    let mut q = sqlx::query_as::<_, (Option<String>, Option<String>, Option<String>, Option<i64>, Option<i64>, i64)>(&sql)
        .bind(&tz)
        .bind(&w.cur_utc);
    if let Some(m) = model {
        q = q.bind(m);
    }
    let mut agg: Ordered<Agg> = Ordered::default();
    for (b, api_key, mdl, p, c, n) in q.fetch_all(pool).await? {
        let Some(u) = owner_of(&api_key, &umap) else { continue };
        let (p, c) = (p.unwrap_or(0), c.unwrap_or(0));
        let a = agg.entry(&u);
        a.prompt += p;
        a.completion += c;
        a.requests += n;
        let s = a.spark.entry(b.unwrap_or_default()).or_insert([0, 0, 0]);
        s[0] += p;
        s[1] += c;
        s[2] += n;
        if let Some(m) = mdl.filter(|m| !m.is_empty()) {
            if p != 0 || c != 0 {
                a.add_model(&m, p + c);
            }
        }
    }

    // Previous period, same metric and model; « depuis le début » has none.
    let mut prev: Ordered<[i64; 3]> = Ordered::default();
    if period != "all" {
        let sql = format!(
            "SELECT api_key, SUM(COALESCE(prompt_tokens,0))::bigint, SUM(COALESCE(completion_tokens,0))::bigint, \
             COUNT(*) FILTER (WHERE status = 'success') FROM \"LiteLLM_SpendLogs\" \
             WHERE \"startTime\" >= $1::timestamp AND \"startTime\" < $2::timestamp{} GROUP BY api_key",
            if model.is_some() { " AND model_group = $3" } else { "" }
        );
        let mut q = sqlx::query_as::<_, (Option<String>, Option<i64>, Option<i64>, i64)>(&sql)
            .bind(&w.prev_utc)
            .bind(&w.cur_utc);
        if let Some(m) = model {
            q = q.bind(m);
        }
        for (api_key, p, c, n) in q.fetch_all(pool).await? {
            let Some(u) = owner_of(&api_key, &umap) else { continue };
            let e = prev.entry(&u);
            e[0] += p.unwrap_or(0);
            e[1] += c.unwrap_or(0);
            e[2] += n;
        }
    }

    // Models seen over the period, for the filter (never filtered itself).
    let seen: Vec<(Option<String>, i64, Option<i64>)> = sqlx::query_as(
        "SELECT model_group, COUNT(*) FILTER (WHERE status = 'success'), \
         SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0))::bigint FROM \"LiteLLM_SpendLogs\" \
         WHERE \"startTime\" >= $1::timestamp AND model_group IS NOT NULL AND model_group != '' GROUP BY model_group",
    )
    .bind(&w.cur_utc)
    .fetch_all(pool)
    .await?;
    let mut models: Vec<(String, i64, i64)> = seen
        .into_iter()
        .filter(|(_, n, _)| *n > 0)
        .map(|(m, n, t)| (m.unwrap_or_default(), n, t.unwrap_or(0)))
        .collect();
    models.sort_by(|a, b| b.2.cmp(&a.2));
    let models: Vec<Value> = models
        .into_iter()
        .map(|(m, n, t)| json!({"model": m, "requests": n, "tokens": t}))
        .collect();

    let is_residue = |u: &str| u == "unknown" || known.as_ref().is_some_and(|k| !k.contains(u));

    let mut members: HashMap<String, BTreeSet<String>> = HashMap::new();
    if by == "group" {
        let groups: HashMap<String, BTreeSet<String>> = db.user_groups().await.into_iter().collect();
        let no_group: BTreeSet<String> = std::iter::once(NO_GROUP.to_string()).collect();
        let mut out: Ordered<Agg> = Ordered::default();
        for (u, a) in agg.iter() {
            if is_residue(u) {
                continue;
            }
            for g in groups.get(u).filter(|g| !g.is_empty()).unwrap_or(&no_group) {
                members.entry(g.clone()).or_default().insert(u.clone());
                let t = out.entry(g);
                t.prompt += a.prompt;
                t.completion += a.completion;
                t.requests += a.requests;
                for (b, v) in &a.spark {
                    let s = t.spark.entry(b.clone()).or_insert([0, 0, 0]);
                    for i in 0..3 {
                        s[i] += v[i];
                    }
                }
                for (m, v) in &a.models {
                    t.add_model(m, *v);
                }
            }
        }
        let mut out_prev: Ordered<[i64; 3]> = Ordered::default();
        for (u, q) in prev.iter() {
            if is_residue(u) {
                continue;
            }
            for g in groups.get(u).filter(|g| !g.is_empty()).unwrap_or(&no_group) {
                let t = out_prev.entry(g);
                for i in 0..3 {
                    t[i] += q[i];
                }
            }
        }
        agg = out;
        prev = out_prev;
    }

    for v in agg.values.iter_mut() {
        v.value = metric_value(v.prompt, v.completion, metric, v.requests);
    }
    let tot_prompt: i64 = agg.values.iter().map(|a| a.prompt).sum();
    let tot_completion: i64 = agg.values.iter().map(|a| a.completion).sum();
    let tot_requests: i64 = agg.values.iter().map(|a| a.requests).sum();

    let residue_row = |u: &str| by == "account" && is_residue(u);
    let mut actives: Vec<(&String, &Agg)> =
        agg.iter().filter(|(u, a)| a.value > 0.0 && !residue_row(u)).collect();
    let mut others: Vec<(&String, &Agg)> =
        agg.iter().filter(|(u, a)| a.value > 0.0 && residue_row(u)).collect();
    // Stable sorts, descending: ties keep their first-seen order, as in Python.
    actives.sort_by(|a, b| b.1.value.partial_cmp(&a.1.value).unwrap_or(std::cmp::Ordering::Equal));
    others.sort_by(|a, b| b.1.value.partial_cmp(&a.1.value).unwrap_or(std::cmp::Ordering::Equal));

    let (total, avg) = if metric == "per_request" {
        // An average does not add up: the platform's figure is the average
        // over ALL its requests.
        let t = metric_value(tot_prompt, tot_completion, metric, tot_requests);
        (t, t)
    } else {
        let total: f64 = actives.iter().chain(others.iter()).map(|(_, a)| a.value).sum();
        let total_active: f64 = actives.iter().map(|(_, a)| a.value).sum();
        let avg = if actives.is_empty() { 0.0 } else { total_active / actives.len() as f64 };
        (total, avg)
    };

    let (p_in, p_out) = (
        price["input"].as_f64().unwrap_or(2.5),
        price["output"].as_f64().unwrap_or(10.0),
    );
    let line = |u: &String, a: &Agg, rank: Option<usize>| -> Value {
        let pv_value = prev.get(u).map(|q| metric_value(q[0], q[1], metric, q[2])).unwrap_or(0.0);
        let residue = residue_row(u);
        let tokens = a.prompt + a.completion;
        let mut top = a.models.clone();
        top.sort_by(|x, y| y.1.cmp(&x.1));
        top.truncate(3);
        let value = if metric == "per_request" { json!(round_to(a.value, 1)) } else { json!(a.value as i64) };
        json!({
            "rank": rank,
            "username": u,
            "is_me": if by == "group" { me.is_some_and(|m| members.get(u).is_some_and(|s| s.contains(m))) } else { Some(u.as_str()) == me },
            "is_group": by == "group",
            "members": if by == "group" { json!(members.get(u).map_or(0, |s| s.len())) } else { Value::Null },
            "is_unattributed": residue,
            "value": value,
            "tokens": tokens,
            "prompt": a.prompt,
            "completion": a.completion,
            "requests": a.requests,
            "share_pct": share_pct(a.value, total, metric),
            // No delta on an unattributed row: a leftover has no history.
            "delta": if residue { None } else { delta_pct(a.value, pv_value) },
            "trend": w.buckets.iter().map(|b| {
                let s = a.spark.get(b).copied().unwrap_or([0, 0, 0]);
                let v = metric_value(s[0], s[1], metric, s[2]);
                if metric == "per_request" { json!(v) } else { json!(v as i64) }
            }).collect::<Vec<_>>(),
            "models": top.iter().map(|(m, v)| json!({
                "model": m,
                "share_pct": if tokens != 0 { *v as f64 / tokens as f64 * 100.0 } else { 0.0 },
            })).collect::<Vec<_>>(),
            "cost": cost(a.prompt, a.completion, p_in, p_out),
        })
    };
    let mut rows: Vec<Value> = actives.iter().enumerate().map(|(i, (u, a))| line(u, a, Some(i + 1))).collect();
    rows.extend(others.iter().map(|(u, a)| line(u, a, None)));

    Ok(json!({
        "period": period, "metric": metric, "rows": rows, "by": by, "model": model,
        "active_count": actives.len(), "total": round_to(total, 1), "avg": round_to(avg, 1),
        "has_prev": period != "all", "models": models, "prices": price,
        "cost_total": cost(tot_prompt, tot_completion, p_in, p_out),
    }))
}

/// The leaderboard as one viewer sees it (ranking_routes.api_ranking):
/// accounts that chose anonymity are hidden from the OTHER users.
pub async fn ranking_for_viewer(
    db: &Database,
    pool: Option<&PgPool>,
    period: &str,
    metric: &str,
    by: &str,
    model: Option<&str>,
    me: &str,
    admin: bool,
) -> Value {
    let mut data = ranking_full(db, pool, period, Some(me), metric, model, by).await;
    let anonymous: HashSet<String> = db.anonymous_accounts().await;
    let avatars = db.avatars().await;
    let by_group = data["by"] == "group";
    let groups = if by_group { db.user_groups().await } else { Vec::new() };
    if let Some(rows) = data["rows"].as_array_mut() {
        for (n, row) in rows.iter_mut().enumerate() {
            let name = row["username"].as_str().unwrap_or_default().to_string();
            if by_group {
                row["avatar_id"] = Value::Null;
                // A group of one IS its member.
                if row["members"] == 1 && !admin && row["is_me"] != true {
                    let first = groups.iter().find(|(_, gs)| gs.contains(&name)).map(|(u, _)| u);
                    if first.is_some_and(|u| anonymous.contains(u)) {
                        row["username"] = Value::Null;
                        row["is_anonymous"] = json!(true);
                    }
                }
                continue;
            }
            let hidden = anonymous.contains(&name) && name != me && !admin;
            row["is_anonymous"] = json!(anonymous.contains(&name));
            row["avatar_id"] = if hidden { Value::Null } else { json!(avatars.get(&name).cloned().flatten()) };
            if hidden {
                row["username"] = Value::Null;
                row["anon_id"] = json!(format!("anon-{n}"));
            }
            if admin {
                // Not ported: the LiteLLM budget share (/user/list, master key).
                row["quota_pct"] = Value::Null;
            }
        }
    }
    data["is_admin"] = json!(admin);
    data["me_anonymous"] = json!(anonymous.contains(me));
    data["period_label"] = json!(period_label(period));
    data["prev_label"] = json!(prev_label(period));
    data
}

/// ranking.ACTIVITY_DAYS: days per period for the heatmap and the detail.
fn activity_days(period: &str) -> Option<i64> {
    match period {
        "week" => Some(7),
        "month" => Some(30),
        "year" => Some(365),
        _ => None,
    }
}

/// Today's local date and the UTC instant of local midnight `back` days ago,
/// from Postgres (the portal image carries no time zone database). Without
/// the database, UTC's today — only the empty grid's dates depend on it.
async fn local_today(pool: Option<&PgPool>, tz: &str, back: i64) -> (NaiveDate, Option<String>) {
    if let Some(pool) = pool {
        let row: Result<(String, String), _> = sqlx::query_as(
            "SELECT (now() AT TIME ZONE $1)::date::text, \
             ((((now() AT TIME ZONE $1)::date - $2::int)::timestamp AT TIME ZONE $1) AT TIME ZONE 'UTC')::text",
        )
        .bind(tz)
        .bind(back as i32)
        .fetch_one(pool)
        .await;
        if let Ok((today, since)) = row {
            if let Ok(d) = NaiveDate::parse_from_str(&today, "%Y-%m-%d") {
                return (d, Some(since));
            }
        }
    }
    (chrono::Utc::now().date_naive(), None)
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// ranking.activity: successful requests and tokens per real hour (week) or
/// per real day (month, year); `username` restricts it to one account.
pub async fn activity(pool: Option<&PgPool>, period: &str, username: Option<&str>) -> Value {
    let period = if activity_days(period).is_some() { period } else { "month" };
    let days = activity_days(period).unwrap_or(30);
    let tz = spend::local_tz();
    let (today, since) = local_today(pool, &tz, days - 1).await;
    let dates: Vec<NaiveDate> = (0..days).rev().map(|i| today - chrono::Duration::days(i)).collect();
    let hourly = period == "week";
    // date → 24 cells (hourly) or 1 cell, each [requests, tokens]
    let mut grid: HashMap<String, Vec<[i64; 2]>> =
        dates.iter().map(|d| (iso(*d), vec![[0, 0]; if hourly { 24 } else { 1 }])).collect();

    if let (Some(pool), Some(since)) = (pool, since) {
        let umap = spend::key_user_map(pool).await;
        let local = "((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE $1)";
        let sql = format!(
            "SELECT d::text, h, api_key, n, t::bigint FROM (\
             SELECT ({local})::date AS d, EXTRACT(HOUR FROM {local})::int AS h, api_key, COUNT(*) AS n, \
             SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) AS t \
             FROM \"LiteLLM_SpendLogs\" WHERE \"startTime\" >= $2::timestamp AND status = 'success' \
             GROUP BY 1, 2, api_key) s"
        );
        let rows: Vec<(String, i32, Option<String>, i64, Option<i64>)> =
            sqlx::query_as(&sql).bind(&tz).bind(&since).fetch_all(pool).await.unwrap_or_default();
        for (d, hour, api_key, n, tokens) in rows {
            if api_key.as_deref().is_some_and(|k| spend::NON_USER_KEYS.contains(&k)) {
                continue;
            }
            let Some(cells) = grid.get_mut(&d) else { continue };
            if let Some(u) = username {
                if api_key.as_ref().and_then(|k| umap.get(k)).map(String::as_str) != Some(u) {
                    continue;
                }
            }
            let c = &mut cells[if hourly { hour.clamp(0, 23) as usize } else { 0 }];
            c[0] += n;
            c[1] += tokens.unwrap_or(0);
        }
    }

    let cell = |c: &[i64; 2]| json!({"requests": c[0], "tokens": c[1]});
    let mut out = json!({"period": period, "mode": if hourly { "hours" } else { "days" }});
    let flat: Vec<i64> = if hourly {
        out["rows"] = dates
            .iter()
            .map(|d| json!({"day": iso(*d), "cells": grid[&iso(*d)].iter().map(cell).collect::<Vec<_>>()}))
            .collect();
        dates.iter().flat_map(|d| grid[&iso(*d)].iter().map(|c| c[0])).collect()
    } else {
        out["days"] = dates
            .iter()
            .map(|d| {
                let c = grid[&iso(*d)][0];
                json!({"day": iso(*d), "requests": c[0], "tokens": c[1]})
            })
            .collect();
        dates.iter().map(|d| grid[&iso(*d)][0][0]).collect()
    };
    out["max_requests"] = json!(flat.iter().copied().max().unwrap_or(0));
    out["total_requests"] = json!(flat.iter().sum::<i64>());
    out
}

/// ranking._streaks: (longest run of consecutive active days, current run —
/// alive if it reaches today or yesterday). Pure.
pub fn streaks(days: &[NaiveDate], today: NaiveDate) -> (i64, i64) {
    let mut sorted = days.to_vec();
    sorted.sort();
    let (mut longest, mut run) = (0i64, 0i64);
    let mut prev: Option<NaiveDate> = None;
    for d in &sorted {
        run = if prev.is_some_and(|p| *d - p == chrono::Duration::days(1)) { run + 1 } else { 1 };
        longest = longest.max(run);
        prev = Some(*d);
    }
    let current = match prev {
        Some(p) if (today - p).num_days() <= 1 => run,
        _ => 0,
    };
    (longest, current)
}

struct History {
    /// account → (local day → [tokens, requests]), both in first-seen order
    days: Vec<(String, Vec<(NaiveDate, [i64; 2])>)>,
    /// model → (first use epoch, first use UTC date, account), first-seen order
    firsts: Vec<(String, f64, String, String)>,
}

static HISTORY: std::sync::Mutex<Option<(std::time::Instant, std::sync::Arc<History>)>> =
    std::sync::Mutex::new(None);

/// ranking._daily_history over all time (successes only), cached 120 s.
async fn daily_history(db: &Database, pool: Option<&PgPool>) -> std::sync::Arc<History> {
    if let Some((at, h)) = HISTORY.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
        if at.elapsed() < std::time::Duration::from_secs(120) {
            return h.clone();
        }
    }
    let empty = std::sync::Arc::new(History { days: Vec::new(), firsts: Vec::new() });
    let Some(pool) = pool else { return empty };
    let umap = spend::key_user_map(pool).await;
    let known = db.known_accounts().await;
    let sql = "SELECT d::text, api_key, model_group, t::bigint, n, EXTRACT(EPOCH FROM f)::float8, f::date::text FROM (\
               SELECT ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE $1)::date AS d, api_key, \
               model_group, SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)) AS t, COUNT(*) AS n, \
               MIN(\"startTime\") AS f FROM \"LiteLLM_SpendLogs\" WHERE status = 'success' \
               GROUP BY d, api_key, model_group) s";
    let rows: Result<Vec<(String, Option<String>, Option<String>, Option<i64>, i64, f64, String)>, _> =
        sqlx::query_as(sql).bind(spend::local_tz()).fetch_all(pool).await;
    let Ok(rows) = rows else { return empty };
    let mut h = History { days: Vec::new(), firsts: Vec::new() };
    for (d, api_key, mdl, tokens, n, first, first_day) in rows {
        let Some(u) = owner_of(&api_key, &umap) else { continue };
        if u == "unknown" || known.as_ref().is_some_and(|k| !k.contains(&u)) {
            continue;
        }
        let Ok(day) = NaiveDate::parse_from_str(&d, "%Y-%m-%d") else { continue };
        let i = match h.days.iter().position(|(name, _)| *name == u) {
            Some(i) => i,
            None => {
                h.days.push((u.clone(), Vec::new()));
                h.days.len() - 1
            }
        };
        let per_day = &mut h.days[i].1;
        match per_day.iter_mut().find(|(x, _)| *x == day) {
            Some((_, v)) => {
                v[0] += tokens.unwrap_or(0);
                v[1] += n;
            }
            None => per_day.push((day, [tokens.unwrap_or(0), n])),
        }
        if let Some(m) = mdl.filter(|m| !m.is_empty()) {
            match h.firsts.iter_mut().find(|(name, ..)| *name == m) {
                Some(entry) if first < entry.1 => *entry = (m, first, first_day, u.clone()),
                Some(_) => {}
                None => h.firsts.push((m, first, first_day, u.clone())),
            }
        }
    }
    let h = std::sync::Arc::new(h);
    *HISTORY.lock().unwrap_or_else(|e| e.into_inner()) = Some((std::time::Instant::now(), h.clone()));
    h
}

/// The first n rows by `key`, descending, ties in their original order.
fn top(mut rows: Vec<Value>, key: &str, n: usize) -> Vec<Value> {
    rows.sort_by(|a, b| b[key].as_i64().unwrap_or(0).cmp(&a[key].as_i64().unwrap_or(0)));
    rows.truncate(n);
    rows
}

/// ranking.records: biggest days, longest and current streaks, first to use
/// each model, and the viewer's own.
pub async fn records(db: &Database, pool: Option<&PgPool>, me: Option<&str>) -> Value {
    let h = daily_history(db, pool).await;
    let (today, _) = local_today(pool, &spend::local_tz(), 0).await;
    let (mut best_days, mut longest, mut current) = (Vec::new(), Vec::new(), Vec::new());
    let mut mine = Value::Null;
    for (u, per_day) in &h.days {
        // max() keeps the FIRST maximum met, as Python's does.
        let Some((d, [tokens, n])) =
            per_day.iter().fold(None::<&(NaiveDate, [i64; 2])>, |best, x| match best {
                Some(b) if b.1[0] >= x.1[0] => Some(b),
                _ => Some(x),
            })
        else {
            continue;
        };
        best_days.push(json!({"username": u, "day": iso(*d), "tokens": tokens, "requests": n}));
        let day_list: Vec<NaiveDate> = per_day.iter().map(|(x, _)| *x).collect();
        let (lg, cr) = streaks(&day_list, today);
        longest.push(json!({"username": u, "days": lg}));
        if cr != 0 {
            current.push(json!({"username": u, "days": cr}));
        }
        if Some(u.as_str()) == me {
            let mut first_on: Vec<&String> =
                h.firsts.iter().filter(|(_, _, _, who)| Some(who.as_str()) == me).map(|(m, ..)| m).collect();
            first_on.sort();
            mine = json!({
                "best_day": {"day": iso(*d), "tokens": tokens},
                "longest_streak": lg, "current_streak": cr, "active_days": per_day.len(),
                "first_on": first_on,
            });
        }
    }
    let mut firsts = h.firsts.clone();
    firsts.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    firsts.truncate(8);
    json!({
        "best_days": top(best_days, "tokens", 3),
        "longest_streaks": top(longest, "days", 3),
        "current_streaks": top(current, "days", 3),
        "firsts": firsts.iter().map(|(m, _, day, who)| json!({"model": m, "username": who, "day": day})).collect::<Vec<_>>(),
        "me": mine,
    })
}

/// ranking.user_detail: per model and per day, over the period, for ONE account.
pub async fn user_detail(db: &Database, pool: Option<&PgPool>, username: &str, period: &str) -> Value {
    let days = activity_days(period).unwrap_or(30);
    let price = prices(db).await;
    let mut out = json!({
        "username": username, "period": if activity_days(period).is_some() { period } else { "month" },
        "models": [], "daily": [], "prompt": 0, "completion": 0, "requests": 0, "cost": 0,
    });
    let Some(pool) = pool else { return out };
    let umap = spend::key_user_map(pool).await;
    let keys: Vec<String> = umap.iter().filter(|(_, u)| u.as_str() == username).map(|(k, _)| k.clone()).collect();
    if keys.is_empty() {
        return out;
    }
    let tz = spend::local_tz();
    let sql = "SELECT d::text, model_group, p::bigint, c::bigint, n FROM (\
               SELECT ((\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE $1)::date AS d, model_group, \
               SUM(COALESCE(prompt_tokens,0)) AS p, SUM(COALESCE(completion_tokens,0)) AS c, COUNT(*) AS n \
               FROM \"LiteLLM_SpendLogs\" WHERE api_key = ANY($2) \
               AND \"startTime\" >= (((now() AT TIME ZONE $1) - make_interval(days => $3)) AT TIME ZONE $1) AT TIME ZONE 'UTC' \
               AND status = 'success' GROUP BY d, model_group) s";
    let rows: Result<Vec<(String, Option<String>, Option<i64>, Option<i64>, i64)>, _> =
        sqlx::query_as(sql).bind(&tz).bind(&keys).bind(days as i32).fetch_all(pool).await;
    let Ok(rows) = rows else { return out };
    let mut per_model: Vec<(String, [i64; 3])> = Vec::new();
    let mut per_day: HashMap<String, [i64; 3]> = HashMap::new();
    for (d, mdl, p, c, n) in rows {
        let m = mdl.unwrap_or_else(|| "?".to_string());
        let m = if m.is_empty() { "?".to_string() } else { m };
        let add = |acc: &mut [i64; 3]| {
            acc[0] += p.unwrap_or(0);
            acc[1] += c.unwrap_or(0);
            acc[2] += n;
        };
        match per_model.iter_mut().find(|(x, _)| *x == m) {
            Some((_, v)) => add(v),
            None => {
                let mut v = [0; 3];
                add(&mut v);
                per_model.push((m, v));
            }
        }
        add(per_day.entry(d).or_insert([0; 3]));
    }
    let mut models = per_model.clone();
    models.sort_by(|a, b| (b.1[0] + b.1[1]).cmp(&(a.1[0] + a.1[1])));
    out["models"] = models
        .iter()
        .map(|(m, v)| json!({"model": m, "prompt": v[0], "completion": v[1], "requests": v[2]}))
        .collect();
    let (today, _) = local_today(Some(pool), &tz, 0).await;
    let start = today - chrono::Duration::days(days - 1);
    out["daily"] = if days <= 31 {
        (0..days)
            .map(|i| {
                let d = iso(start + chrono::Duration::days(i));
                let v = per_day.get(&d).copied().unwrap_or([0; 3]);
                json!({"day": d, "tokens": v[0] + v[1], "requests": v[2]})
            })
            .collect()
    } else {
        json!([])
    };
    let (p, c, n) = per_model.iter().fold((0, 0, 0), |a, (_, v)| (a.0 + v[0], a.1 + v[1], a.2 + v[2]));
    out["prompt"] = json!(p);
    out["completion"] = json!(c);
    out["requests"] = json!(n);
    out["cost"] = json!(cost(p, c, price["input"].as_f64().unwrap_or(2.5), price["output"].as_f64().unwrap_or(10.0)));
    out["currency"] = price["currency"].clone();
    out
}

/// Python's str() of a number in a CSV cell: an integral float keeps its
/// `.0` (« 12.0 »), an int has none. Rust's shortest round-trip display is
/// the same algorithm as Python's repr otherwise.
fn py_num(v: &Value) -> String {
    match v {
        Value::Number(n) if n.is_f64() => {
            let f = n.as_f64().unwrap_or(0.0);
            if f.fract() == 0.0 && f.is_finite() { format!("{f:.1}") } else { format!("{f}") }
        }
        Value::Number(n) => n.to_string(),
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// csv.writer's QUOTE_MINIMAL: quote a field holding the delimiter, a quote
/// or a line break, doubling inner quotes.
fn csv_field(s: &str) -> String {
    if s.contains([',', '"', '\r', '\n']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}

/// ranking_routes.api_ranking_export's body and file name.
pub fn export_csv(data: &Value, period: &str) -> (String, String) {
    let currency = data["prices"]["currency"].as_str().unwrap_or("USD");
    let mut out = String::new();
    let mut line = |cells: Vec<String>| {
        out.push_str(&cells.iter().map(|c| csv_field(c)).collect::<Vec<_>>().join(","));
        out.push_str("\r\n");
    };
    line(
        ["rank", "name", "value", "tokens", "prompt", "completion", "requests", "share_pct", "delta_pct"]
            .iter()
            .map(|s| s.to_string())
            .chain(std::iter::once(format!("cost_{currency}")))
            .collect(),
    );
    for r in data["rows"].as_array().into_iter().flatten() {
        let opt_round = |v: &Value, digits: usize| match v.as_f64() {
            Some(f) => py_num(&json!(round_to(f, digits))),
            None => String::new(),
        };
        line(vec![
            match &r["rank"] {
                Value::Null => String::new(),
                v => py_num(v),
            },
            py_num(&r["username"]),
            py_num(&r["value"]),
            py_num(&r["tokens"]),
            py_num(&r["prompt"]),
            py_num(&r["completion"]),
            py_num(&r["requests"]),
            opt_round(&r["share_pct"], 2),
            opt_round(&r["delta"], 1),
            py_num(&r["cost"]),
        ]);
    }
    let name = format!(
        "ranking-{}-{}-{}.csv",
        data["by"].as_str().unwrap_or("account"),
        period,
        data["metric"].as_str().unwrap_or("total")
    );
    (out, name)
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

    #[test]
    fn streaks_count_consecutive_days_and_a_run_lives_until_yesterday() {
        let d = |s: &str| NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap();
        let days = [d("2026-10-01"), d("2026-10-02"), d("2026-10-03"), d("2026-10-05"), d("2026-10-06")];
        assert_eq!(streaks(&days, d("2026-10-07")), (3, 2));
        assert_eq!(streaks(&days, d("2026-10-06")), (3, 2));
        assert_eq!(streaks(&days, d("2026-10-08")), (3, 0));
        assert_eq!(streaks(&[], d("2026-10-08")), (0, 0));
    }
}
