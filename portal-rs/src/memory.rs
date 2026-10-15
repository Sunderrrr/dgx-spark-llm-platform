//! The viewer's memory graph, parity with dgx-portal memory_routes.py: the
//! graph read, the settings, adding / correcting / forgetting a fact, the purge,
//! the JSON and Markdown export and import.
//!
//! Everything is partitioned by username, nodes AND edges: no query crosses
//! from one account to another. Node matching goes through `memnorm::mem_norm`,
//! checked against the Python function (memnorm.rs), so both backends find the
//! same node for the same name.
use std::collections::HashMap;
use std::sync::OnceLock;

use regex::Regex;
use serde_json::{json, Map, Value};
use sqlx::{Row, SqlitePool};

use crate::keys::iso;
use crate::memnorm::{mem_norm, MEM_MAX_NAME_LEN};

pub const MEMORY_SCHEMA: &str = "dgx-memory";
pub const EXPORT_SCHEMA_VERSION: i64 = 1;
pub const MEM_MAX_FACTS: i64 = 400;
pub const MEM_GENERIC_RELATION: &str = "à propos de";
pub const MEM_MAX_FACT_LEN: usize = 2000;
pub const IMPORT_MAX_BYTES: usize = 5 * 1024 * 1024;
pub const IMPORT_MAX_NODES: usize = 10_000;
const KINDS: [&str; 4] = ["subject", "person", "tool", "preference"];

fn now_iso() -> String {
    iso(&chrono::Utc::now().naive_utc())
}

/// Python's `str.isspace()`: Unicode whitespace plus the separators \x1c-\x1f.
pub fn py_is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `str.strip()`.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

/// The first `n` characters (Python slicing counts code points).
fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

// ── Settings ────────────────────────────────────────────────────────────────

/// `_mem_enabled`: on by default; only an explicit 0 turns it off.
pub async fn enabled(db: &SqlitePool, username: &str) -> bool {
    match sqlx::query("SELECT memory_enabled FROM user_prefs WHERE username = ?")
        .bind(username)
        .fetch_optional(db)
        .await
        .ok()
        .flatten()
    {
        None => true,
        Some(r) => r.try_get::<Option<i64>, _>("memory_enabled").ok().flatten().unwrap_or(0) != 0,
    }
}

/// `_mem_settings`: the two secondary switches, ON when never set.
pub async fn settings(db: &SqlitePool, username: &str) -> (bool, bool) {
    match sqlx::query("SELECT memory_search_chats, memory_sensitive FROM user_prefs WHERE username = ?")
        .bind(username)
        .fetch_optional(db)
        .await
        .ok()
        .flatten()
    {
        None => (true, true),
        Some(r) => {
            let on = |col: &str| r.try_get::<Option<i64>, _>(col).ok().flatten().is_none_or(|v| v != 0);
            (on("memory_search_chats"), on("memory_sensitive"))
        }
    }
}

/// `_mem_set_setting` / `_mem_set_enabled`: one user_prefs column, upserted.
pub async fn set_pref(db: &SqlitePool, username: &str, column: &str, on: bool) -> Result<(), sqlx::Error> {
    let column = match column {
        "memory_search_chats" | "memory_sensitive" | "memory_enabled" => column,
        _ => return Err(sqlx::Error::Protocol("unknown memory setting".into())),
    };
    sqlx::query(&format!(
        "INSERT INTO user_prefs (username, {column}) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET {column} = excluded.{column}"
    ))
    .bind(username)
    .bind(on as i64)
    .execute(db)
    .await?;
    Ok(())
}

// ── Graph ───────────────────────────────────────────────────────────────────

fn row_json(r: &sqlx::sqlite::SqliteRow, cols: &[&str]) -> Value {
    let mut m = Map::new();
    for c in cols {
        let v = if let Ok(Some(i)) = r.try_get::<Option<i64>, _>(*c) {
            json!(i)
        } else if let Ok(Some(f)) = r.try_get::<Option<f64>, _>(*c) {
            json!(f)
        } else {
            json!(r.try_get::<Option<String>, _>(*c).ok().flatten())
        };
        m.insert((*c).to_string(), v);
    }
    Value::Object(m)
}

/// `_mem_graph`: every node and edge of the account (live facts unless `expired`).
pub async fn graph(db: &SqlitePool, username: &str, include_expired: bool) -> Result<(Vec<Value>, Vec<Value>), sqlx::Error> {
    let where_ = if include_expired { "" } else { " AND valid_until IS NULL" };
    let edges = sqlx::query(&format!(
        "SELECT e.id, e.relation, e.fact, e.source, e.created_at, e.valid_until, e.src_id, e.dst_id, \
         s.name AS subject, d.name AS object FROM memory_edges e \
         JOIN memory_nodes s ON s.id = e.src_id LEFT JOIN memory_nodes d ON d.id = e.dst_id \
         WHERE e.username = ?{where_} ORDER BY e.created_at DESC"
    ))
    .bind(username)
    .fetch_all(db)
    .await?;
    let nodes = sqlx::query("SELECT id, name, kind, created_at FROM memory_nodes WHERE username = ? ORDER BY name")
        .bind(username)
        .fetch_all(db)
        .await?;
    Ok((
        nodes.iter().map(|r| row_json(r, &["id", "name", "kind", "created_at"])).collect(),
        edges
            .iter()
            .map(|r| {
                row_json(r, &["id", "relation", "fact", "source", "created_at", "valid_until", "src_id", "dst_id", "subject", "object"])
            })
            .collect(),
    ))
}

/// `_mem_node`: the node of a name (by its normalized form, then by alias),
/// created when absent unless `create` is false. Returns its id.
pub async fn node(db: &SqlitePool, username: &str, name: &str, kind: &str, create: bool) -> Result<Option<i64>, sqlx::Error> {
    let norm = mem_norm(name);
    if norm.is_empty() {
        return Ok(None);
    }
    let found: Option<i64> = sqlx::query_scalar("SELECT id FROM memory_nodes WHERE username = ? AND name_norm = ?")
        .bind(username)
        .bind(&norm)
        .fetch_optional(db)
        .await?;
    if found.is_some() {
        return Ok(found);
    }
    let by_alias: Option<i64> = sqlx::query_scalar(
        "SELECT n.id FROM memory_nodes n JOIN memory_aliases a ON a.node_id = n.id WHERE a.username = ? AND a.alias_norm = ?",
    )
    .bind(username)
    .bind(&norm)
    .fetch_optional(db)
    .await?;
    if by_alias.is_some() || !create {
        return Ok(by_alias);
    }
    let kind = if KINDS.contains(&kind) { kind } else { "subject" };
    sqlx::query("INSERT INTO memory_nodes (username, name, name_norm, kind, created_at) VALUES (?, ?, ?, ?, ?)")
        .bind(username)
        .bind(take(py_strip(name), MEM_MAX_NAME_LEN))
        .bind(&norm)
        .bind(kind)
        .bind(now_iso())
        .execute(db)
        .await?;
    sqlx::query_scalar("SELECT id FROM memory_nodes WHERE username = ? AND name_norm = ?")
        .bind(username)
        .bind(&norm)
        .fetch_optional(db)
        .await
}

/// A JSON field as Python's `x.strip()[:n] if isinstance(x, str) else default`.
fn str_field(v: Option<&Value>, n: usize) -> Option<String> {
    v.and_then(Value::as_str).map(|s| take(py_strip(s), n))
}

/// `_mem_add_fact`: writes a fact; an equivalent one (same subject, explicit
/// relation, same object) is expired rather than doubled. (message, ok).
pub async fn add_fact(
    db: &SqlitePool,
    username: &str,
    subject: Option<&Value>,
    relation: Option<&Value>,
    fact: Option<&Value>,
    obj: Option<&Value>,
    source: &str,
    kind: Option<&Value>,
) -> Result<(String, bool), sqlx::Error> {
    let subject = str_field(subject, MEM_MAX_NAME_LEN).unwrap_or_default();
    let relation = str_field(relation, 80).unwrap_or_default();
    let fact = str_field(fact, MEM_MAX_FACT_LEN).unwrap_or_default();
    let obj = str_field(obj, MEM_MAX_NAME_LEN);
    let kind = kind.and_then(Value::as_str).unwrap_or("subject").to_string();
    if subject.is_empty() || fact.is_empty() {
        return Ok(("Sujet et fait sont obligatoires.".into(), false));
    }
    let n_facts: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM memory_edges WHERE username = ? AND valid_until IS NULL")
        .bind(username)
        .fetch_one(db)
        .await?;
    if n_facts >= MEM_MAX_FACTS {
        return Ok((
            format!("Mémoire pleine ({MEM_MAX_FACTS} faits). L'utilisateur doit en supprimer depuis la page Mémoire."),
            false,
        ));
    }
    let Some(src) = node(db, username, &subject, &kind, true).await? else {
        return Ok(("Sujet invalide.".into(), false));
    };
    let dst = match obj.as_deref().filter(|o| !py_strip(o).is_empty()) {
        Some(o) => node(db, username, o, "subject", true).await?,
        None => None,
    };
    if !relation.is_empty() && relation != MEM_GENERIC_RELATION {
        sqlx::query(
            "UPDATE memory_edges SET valid_until = ? WHERE username = ? AND src_id = ? AND relation = ? \
             AND valid_until IS NULL AND IFNULL(dst_id, -1) = ?",
        )
        .bind(now_iso())
        .bind(username)
        .bind(src)
        .bind(&relation)
        .bind(dst.unwrap_or(-1))
        .execute(db)
        .await?;
    }
    sqlx::query(
        "INSERT INTO memory_edges (username, src_id, relation, dst_id, fact, source, confidence, created_at) \
         VALUES (?, ?, ?, ?, ?, ?, 1.0, ?)",
    )
    .bind(username)
    .bind(src)
    .bind(&relation)
    .bind(dst)
    .bind(&fact)
    .bind(if source == "user" { "user" } else { "model" })
    .bind(now_iso())
    .execute(db)
    .await?;
    Ok((format!("Mémorisé : {fact}"), true))
}

/// Outcome of a correction: Ok(message) or Err(message) for 404.
pub enum Update {
    Done(String),
    Refused(String),
    /// A non-string field: Flask fails on `.strip()` (500).
    Malformed,
}

/// `_mem_update_fact`: corrects a live fact in place (same id, same date).
pub async fn update_fact(db: &SqlitePool, username: &str, edge_id: i64, fact: Option<&Value>, relation: Option<&Value>) -> Result<Update, sqlx::Error> {
    let row = sqlx::query("SELECT fact, relation FROM memory_edges WHERE username = ? AND id = ? AND valid_until IS NULL")
        .bind(username)
        .bind(edge_id)
        .fetch_optional(db)
        .await?;
    let Some(r) = row else { return Ok(Update::Refused("Information introuvable.".into())) };
    let old_fact: String = r.try_get::<Option<String>, _>("fact")?.unwrap_or_default();
    let old_rel: String = r.try_get::<Option<String>, _>("relation")?.unwrap_or_default();
    let pick = |v: Option<&Value>, old: &str| -> Option<String> {
        match v {
            None | Some(Value::Null) => Some(old.to_string()),
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => None,
        }
    };
    let (Some(new_fact), Some(new_rel)) = (pick(fact, &old_fact), pick(relation, &old_rel)) else {
        return Ok(Update::Malformed);
    };
    let new_fact = take(py_strip(&new_fact), MEM_MAX_FACT_LEN);
    if new_fact.is_empty() {
        return Ok(Update::Refused("Le texte ne peut pas être vide.".into()));
    }
    let new_rel = take(py_strip(&new_rel), 80);
    let new_rel = if new_rel.is_empty() { old_rel } else { new_rel };
    sqlx::query("UPDATE memory_edges SET fact = ?, relation = ? WHERE username = ? AND id = ?")
        .bind(&new_fact)
        .bind(&new_rel)
        .bind(username)
        .bind(edge_id)
        .execute(db)
        .await?;
    Ok(Update::Done(format!("Mis à jour : {new_fact}")))
}

/// `_mem_forget`: deletes a fact for good, then the nodes left without any edge.
pub async fn forget(db: &SqlitePool, username: &str, edge_id: i64) -> Result<bool, sqlx::Error> {
    let r = sqlx::query("DELETE FROM memory_edges WHERE username = ? AND id = ?")
        .bind(username)
        .bind(edge_id)
        .execute(db)
        .await?;
    sqlx::query(
        "DELETE FROM memory_nodes WHERE username = ? AND id NOT IN (SELECT src_id FROM memory_edges WHERE username = ? \
         UNION SELECT dst_id FROM memory_edges WHERE username = ? AND dst_id IS NOT NULL)",
    )
    .bind(username)
    .bind(username)
    .bind(username)
    .execute(db)
    .await?;
    Ok(r.rows_affected() > 0)
}

/// `_mem_purge`: the whole memory of the account; returns how many facts went.
pub async fn purge(db: &SqlitePool, username: &str) -> Result<i64, sqlx::Error> {
    let n: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM memory_edges WHERE username = ?")
        .bind(username)
        .fetch_one(db)
        .await?;
    for table in ["memory_edges", "memory_aliases", "memory_nodes"] {
        sqlx::query(&format!("DELETE FROM {table} WHERE username = ?")).bind(username).execute(db).await?;
    }
    Ok(n)
}

// ── Export ──────────────────────────────────────────────────────────────────

/// `json.dumps(v, ensure_ascii=False, indent=2)`.
pub fn py_dumps_indent(v: &Value) -> String {
    fn write(v: &Value, depth: usize, out: &mut String) {
        let pad = |d: usize| "  ".repeat(d);
        match v {
            Value::Array(a) if a.is_empty() => out.push_str("[]"),
            Value::Object(o) if o.is_empty() => out.push_str("{}"),
            Value::Array(a) => {
                out.push_str("[\n");
                for (i, x) in a.iter().enumerate() {
                    if i > 0 {
                        out.push_str(",\n");
                    }
                    out.push_str(&pad(depth + 1));
                    write(x, depth + 1, out);
                }
                out.push('\n');
                out.push_str(&pad(depth));
                out.push(']');
            }
            Value::Object(o) => {
                out.push_str("{\n");
                for (i, (k, x)) in o.iter().enumerate() {
                    if i > 0 {
                        out.push_str(",\n");
                    }
                    out.push_str(&pad(depth + 1));
                    out.push_str(&crate::conversations::py_dumps(&Value::String(k.clone())));
                    out.push_str(": ");
                    write(x, depth + 1, out);
                }
                out.push('\n');
                out.push_str(&pad(depth));
                out.push('}');
            }
            scalar => out.push_str(&crate::conversations::py_dumps(scalar)),
        }
    }
    let mut out = String::new();
    write(v, 0, &mut out);
    out
}

/// `_mem_export_doc`: the account's live graph as the export schema.
pub async fn export_doc(db: &SqlitePool, username: &str) -> Result<Value, sqlx::Error> {
    let (nodes, edges) = graph(db, username, false).await?;
    let mut aliases: HashMap<i64, Vec<String>> = HashMap::new();
    for r in sqlx::query("SELECT node_id, alias_norm FROM memory_aliases WHERE username = ? ORDER BY alias_norm")
        .bind(username)
        .fetch_all(db)
        .await?
    {
        let id: i64 = r.try_get("node_id")?;
        let a: String = r.try_get::<Option<String>, _>("alias_norm")?.unwrap_or_default();
        aliases.entry(id).or_default().push(a);
    }
    let nodes: Vec<Value> = nodes
        .iter()
        .map(|n| {
            let id = n["id"].as_i64().unwrap_or(-1);
            json!({"name": n["name"], "kind": n["kind"], "aliases": aliases.get(&id).cloned().unwrap_or_default()})
        })
        .collect();
    let edges: Vec<Value> = edges
        .iter()
        .map(|e| json!({"subject": e["subject"], "relation": e["relation"], "fact": e["fact"], "object": e["object"], "source": e["source"]}))
        .collect();
    Ok(json!({
        "schema": MEMORY_SCHEMA, "version": EXPORT_SCHEMA_VERSION, "username": username,
        "exported_at": now_iso(), "nodes": nodes, "edges": edges,
    }))
}

/// `api_memory_export_md`'s text, from the export document. Pure.
pub fn export_markdown(doc: &Value, platform: &str) -> String {
    let s = |v: &Value| v.as_str().unwrap_or_default().to_string();
    let edges = doc["edges"].as_array().cloned().unwrap_or_default();
    let nodes = doc["nodes"].as_array().cloned().unwrap_or_default();
    let exported: String = take(&s(&doc["exported_at"]), 19).replace('T', " ");
    let mut lines = vec![
        format!("# Ce que {platform} sait de moi"),
        String::new(),
        format!("> Exporté le {exported} — {} fait(s), {} sujet(s).", edges.len(), nodes.len()),
        String::new(),
    ];
    let mut order: Vec<String> = Vec::new();
    let mut by_subject: HashMap<String, Vec<&Value>> = HashMap::new();
    for e in &edges {
        let subj = s(&e["subject"]);
        if !by_subject.contains_key(&subj) {
            order.push(subj.clone());
        }
        by_subject.entry(subj).or_default().push(e);
    }
    let node_by_name: HashMap<String, &Value> = nodes.iter().map(|n| (s(&n["name"]), n)).collect();
    let mut subjects = order.clone();
    subjects.sort_by_key(|x| x.to_lowercase());
    for subject in &subjects {
        lines.push(format!("## {subject}"));
        if let Some(n) = node_by_name.get(subject) {
            let al: Vec<String> = n["aliases"].as_array().into_iter().flatten().map(|a| s(a)).collect();
            if !al.is_empty() {
                lines.push(format!("_alias : {}_", al.join(", ")));
            }
        }
        for e in &by_subject[subject] {
            let rel = Some(s(&e["relation"])).filter(|r| !r.is_empty()).unwrap_or_else(|| MEM_GENERIC_RELATION.to_string());
            let obj = s(&e["object"]);
            if !obj.is_empty() {
                lines.push(format!("- **{rel}** {obj} : {}", s(&e["fact"])));
            } else {
                lines.push(format!("- **{rel}** : {}", s(&e["fact"])));
            }
        }
        lines.push(String::new());
    }
    let mut orphans: Vec<&Value> = nodes.iter().filter(|n| !by_subject.contains_key(&s(&n["name"]))).collect();
    if !orphans.is_empty() {
        lines.push("## Sujets sans fait".into());
        orphans.sort_by_key(|n| s(&n["name"]).to_lowercase());
        for n in orphans {
            lines.push(format!("- {} ({})", s(&n["name"]), s(&n["kind"])));
        }
        lines.push(String::new());
    }
    lines.join("\n")
}

// ── Import ──────────────────────────────────────────────────────────────────

/// `_validate_import_doc`: None when valid (the node kinds are fixed in place).
pub fn validate_import(doc: &mut Value) -> Option<String> {
    let Some(obj) = doc.as_object_mut() else { return Some("Document JSON invalide (objet attendu).".into()) };
    if obj.get("schema").and_then(Value::as_str) != Some(MEMORY_SCHEMA) {
        return Some("Format non reconnu (clé 'schema' manquante ou incorrecte).".into());
    }
    let (Some(Value::Array(_)), Some(Value::Array(_))) = (obj.get("nodes"), obj.get("edges")) else {
        return Some("'nodes' et 'edges' doivent être des listes.".into());
    };
    let n_nodes = obj["nodes"].as_array().map_or(0, Vec::len);
    let n_edges = obj["edges"].as_array().map_or(0, Vec::len);
    if n_nodes > IMPORT_MAX_NODES {
        return Some(format!("Trop de sujets ({n_nodes})."));
    }
    if n_edges > MEM_MAX_FACTS as usize {
        return Some(format!("Trop de faits ({n_edges} > {MEM_MAX_FACTS})."));
    }
    for n in obj.get_mut("nodes").and_then(Value::as_array_mut).into_iter().flatten() {
        let Some(nm) = n.as_object_mut() else { return Some("Un sujet est invalide (champ 'name' manquant).".into()) };
        let Some(name) = nm.get("name").and_then(Value::as_str) else {
            return Some("Un sujet est invalide (champ 'name' manquant).".into());
        };
        if name.chars().count() > MEM_MAX_NAME_LEN {
            return Some("Nom de sujet trop long.".into());
        }
        if !nm.get("kind").and_then(Value::as_str).is_some_and(|k| KINDS.contains(&k)) {
            nm.insert("kind".into(), json!("subject"));
        }
        if nm.get("aliases").is_some_and(|a| !a.is_null() && !a.is_array()) {
            return Some("Les alias d'un sujet doivent être une liste.".into());
        }
    }
    for e in obj.get("edges").and_then(Value::as_array).into_iter().flatten() {
        let Some(em) = e.as_object() else { return Some("Un fait est invalide.".into()) };
        let (Some(_), Some(fact)) = (em.get("subject").and_then(Value::as_str), em.get("fact").and_then(Value::as_str)) else {
            return Some("Un fait est invalide (subject/fact manquants).".into());
        };
        if fact.chars().count() > MEM_MAX_FACT_LEN {
            return Some("Fait trop long.".into());
        }
    }
    None
}

/// `_mem_fact_exists`: a live fact with the same normalized subject and text.
pub async fn fact_exists(db: &SqlitePool, username: &str, subject: &str, fact: &str) -> Result<bool, sqlx::Error> {
    Ok(sqlx::query_scalar::<_, i64>(
        "SELECT 1 FROM memory_edges e JOIN memory_nodes s ON s.id = e.src_id \
         WHERE e.username = ? AND s.name_norm = ? AND e.fact = ? AND e.valid_until IS NULL",
    )
    .bind(username)
    .bind(mem_norm(subject))
    .bind(fact)
    .fetch_optional(db)
    .await?
    .is_some())
}

/// Attaches an alias to a node; returns how many rows were inserted (0 or 1).
pub async fn add_alias(db: &SqlitePool, username: &str, node_id: i64, alias: &str) -> Result<u64, sqlx::Error> {
    let norm = mem_norm(alias);
    if norm.is_empty() {
        return Ok(0);
    }
    Ok(sqlx::query("INSERT OR IGNORE INTO memory_aliases (node_id, username, alias_norm) VALUES (?, ?, ?)")
        .bind(node_id)
        .bind(username)
        .bind(norm)
        .execute(db)
        .await?
        .rows_affected())
}

// ── Markdown parsing (import.md) ────────────────────────────────────────────

fn re(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("static pattern"))
}

static BULLET: OnceLock<Regex> = OnceLock::new();
static HEADING: OnceLock<Regex> = OnceLock::new();
static ALIAS: OnceLock<Regex> = OnceLock::new();
static BOLD_LINE: OnceLock<Regex> = OnceLock::new();
static ITALIC_LINE: OnceLock<Regex> = OnceLock::new();
static STRUCT: OnceLock<Regex> = OnceLock::new();
static BOLD_INLINE: OnceLock<Regex> = OnceLock::new();

/// Python's `str.splitlines()`: every line boundary it knows, no trailing empty line.
pub fn py_splitlines(text: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut cur = String::new();
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\r' => {
                if chars.peek() == Some(&'\n') {
                    chars.next();
                }
                lines.push(std::mem::take(&mut cur));
            }
            '\n' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}' => {
                lines.push(std::mem::take(&mut cur));
            }
            c => cur.push(c),
        }
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    lines
}

/// `_strip_markdown_inline`: `**x**` → `x`.
pub fn strip_inline(text: &str) -> String {
    re(&BOLD_INLINE, r"\*\*(.+?)\*\*").replace_all(text, "$1").into_owned()
}

/// `_split_sentences`: cut after `.!?` + whitespace + an uppercase letter
/// (A-Z, À-Ö, Ø-Þ); pieces under 40 characters join the previous one.
/// Written by hand: the Python pattern uses look-around, which Rust's regex
/// does not have.
pub fn split_sentences(text: &str) -> Vec<String> {
    let chars: Vec<char> = py_strip(text).chars().collect();
    let upper = |c: char| c.is_ascii_uppercase() || ('\u{c0}'..='\u{d6}').contains(&c) || ('\u{d8}'..='\u{de}').contains(&c);
    let mut parts: Vec<String> = Vec::new();
    let mut start = 0usize;
    let mut i = 0usize;
    while i < chars.len() {
        if py_is_space(chars[i]) && i > 0 && matches!(chars[i - 1], '.' | '!' | '?') {
            let mut j = i;
            while j < chars.len() && py_is_space(chars[j]) {
                j += 1;
            }
            if j < chars.len() && upper(chars[j]) {
                parts.push(chars[start..i].iter().collect());
                start = j;
                i = j;
                continue;
            }
            i = j;
            continue;
        }
        i += 1;
    }
    parts.push(chars[start..].iter().collect());
    let mut sentences = Vec::new();
    let mut buf = String::new();
    for p in parts {
        let p = py_strip(&p).to_string();
        if p.is_empty() {
            continue;
        }
        if !buf.is_empty() && buf.chars().count() < 40 {
            buf = py_strip(&format!("{buf} {p}")).to_string();
        } else {
            if !buf.is_empty() {
                sentences.push(buf);
            }
            buf = p;
        }
    }
    if !buf.is_empty() {
        sentences.push(buf);
    }
    sentences
}

/// One fact read from Markdown.
#[derive(Debug, Clone, PartialEq)]
pub struct MdFact {
    pub subject: String,
    pub relation: String,
    pub object: Option<String>,
    pub fact: String,
}

/// `_md_parse`: (facts, (subject, alias) pairs).
pub fn md_parse(text: &str) -> (Vec<MdFact>, Vec<(String, String)>) {
    let bullet = re(&BULLET, r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$");
    let heading = re(&HEADING, r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$");
    let alias = re(&ALIAS, r"(?i)^\s*_?alias\s*:\s*(.+?)\s*_?\s*$");
    let bold_line = re(&BOLD_LINE, r"^\s*\*\*(?P<t>.+?)\*\*\s*$");
    let italic_line = re(&ITALIC_LINE, r"^\s*\*(?P<t>[^*].*?)\*\s*$");
    let structured = re(&STRUCT, r"^\*\*(?P<rel>[^*]+)\*\*(?:\s+(?P<obj>[^:]+?))?\s*:\s*(?P<fact>.+)$");
    let mut edges = Vec::new();
    let mut aliases = Vec::new();
    let mut cur: Option<String> = None;
    for raw in py_splitlines(text) {
        let line = raw.trim_end_matches(py_is_space).to_string();
        if py_strip(&line).is_empty() {
            continue;
        }
        if let Some(m) = heading.captures(&line) {
            let level = m[1].chars().count();
            let title = py_strip(&m[2]).to_string();
            if title.is_empty() || level == 1 {
                continue;
            }
            cur = if title.to_lowercase() == "sujets sans fait" { None } else { Some(title) };
            continue;
        }
        if let Some(m) = bold_line.captures(&line) {
            let t = py_strip(&strip_inline(&m["t"])).to_string();
            cur = Some(t.clone());
            if !t.is_empty() {
                continue;
            }
        }
        if let Some(m) = italic_line.captures(&line) {
            let t = py_strip(&strip_inline(&m["t"])).to_string();
            cur = Some(t.clone());
            if !t.is_empty() {
                continue;
            }
        }
        // Python: `if m and cur_subject` — an empty current subject takes no alias.
        if let (Some(m), Some(subject)) = (alias.captures(&line), cur.as_ref().filter(|s| !s.is_empty())) {
            aliases.push((subject.clone(), py_strip(&m[1]).to_string()));
            continue;
        }
        if let Some(m) = bullet.captures(&line) {
            let content = py_strip(&m[1]).to_string();
            if content.is_empty() {
                continue;
            }
            let Some(subject) = cur.clone() else { continue };
            let (relation, object, fact) = match structured.captures(&content) {
                Some(sm) => {
                    let rel = py_strip(&sm["rel"]).to_string();
                    let obj = sm.name("obj").map(|o| py_strip(o.as_str()).to_string()).filter(|o| !o.is_empty());
                    (
                        if rel.is_empty() { MEM_GENERIC_RELATION.to_string() } else { rel },
                        obj,
                        strip_inline(py_strip(&sm["fact"])),
                    )
                }
                None => (MEM_GENERIC_RELATION.to_string(), None, strip_inline(&content)),
            };
            if !fact.is_empty() {
                edges.push(MdFact { subject, relation, object, fact });
            }
            continue;
        }
        if let Some(subject) = cur.clone() {
            for sent in split_sentences(&line) {
                edges.push(MdFact {
                    subject: subject.clone(),
                    relation: MEM_GENERIC_RELATION.to_string(),
                    object: None,
                    fact: strip_inline(&sent),
                });
            }
        }
    }
    (edges, aliases)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitlines_like_python() {
        assert_eq!(py_splitlines("a\r\nb\rc\nd\u{2028}e\n"), vec!["a", "b", "c", "d", "e"]);
        assert_eq!(py_splitlines("a\n\nb"), vec!["a", "", "b"]);
    }

    #[test]
    fn sentences_split_on_end_mark_then_uppercase_and_short_ones_join() {
        let s = split_sentences("Le DGX Spark tourne bien. Il sert Qwen avec quatre sessions en parallèle. ok. Fin de la phrase finale ici.");
        assert_eq!(s[0], "Le DGX Spark tourne bien. Il sert Qwen avec quatre sessions en parallèle. ok.");
        assert_eq!(s.len(), 2);
        assert_eq!(split_sentences("e.g. une abréviation reste"), vec!["e.g. une abréviation reste"]);
    }

    #[test]
    fn markdown_parse_reads_our_export_and_prose() {
        let md = "# Ce que DGX sait de moi\n\n## Projets\n_alias : proj_\n- **utilise** vLLM : pour servir\n- **à propos de** : aime Rust\n\n**Work context**\nIl travaille sur une plateforme IA. Elle tourne sur un DGX.\n## Sujets sans fait\n- orphelin (subject)\n";
        let (edges, aliases) = md_parse(md);
        assert_eq!(aliases, vec![("Projets".to_string(), "proj".to_string())]);
        assert_eq!(edges[0], MdFact { subject: "Projets".into(), relation: "utilise".into(), object: Some("vLLM".into()), fact: "pour servir".into() });
        assert_eq!(edges[1].relation, "à propos de");
        assert_eq!(edges[1].fact, "aime Rust");
        assert_eq!(edges[2].subject, "Work context");
        assert_eq!(edges.len(), 3, "{edges:?}");
    }

    #[test]
    fn indented_dump_like_python() {
        let v = json!({"a": [1, {"b": "é"}], "c": [], "d": {}});
        assert_eq!(py_dumps_indent(&v), "{\n  \"a\": [\n    1,\n    {\n      \"b\": \"é\"\n    }\n  ],\n  \"c\": [],\n  \"d\": {}\n}");
    }

    /// Reference outputs of the Python `_md_parse` and `_split_sentences`
    /// (tests/fixtures/memory_md.json): 1 203 generated documents, 600 sentences.
    #[test]
    fn markdown_and_sentences_match_the_python_reference() {
        let doc: Value = serde_json::from_str(include_str!("../tests/fixtures/memory_md.json")).unwrap();
        let mut bad = Vec::new();
        for c in doc["md"].as_array().unwrap() {
            let (edges, aliases) = md_parse(c["in"].as_str().unwrap());
            let got_e: Vec<Value> = edges.iter().map(|e| json!([e.subject, e.relation, e.object, e.fact])).collect();
            let got_a: Vec<Value> = aliases.iter().map(|(s, a)| json!([s, a])).collect();
            if Value::Array(got_e.clone()) != c["edges"] || Value::Array(got_a.clone()) != c["aliases"] {
                bad.push(format!("{:?}\n  python {} {}\n  rust   {:?} {:?}", c["in"], c["edges"], c["aliases"], got_e, got_a));
            }
        }
        for c in doc["sentences"].as_array().unwrap() {
            let got = split_sentences(c["in"].as_str().unwrap());
            if json!(got) != c["out"] {
                bad.push(format!("sentences {:?}: python {} rust {:?}", c["in"], c["out"], got));
            }
        }
        assert!(bad.is_empty(), "{} mismatches:\n{}", bad.len(), bad.iter().take(4).cloned().collect::<Vec<_>>().join("\n"));
    }
}
