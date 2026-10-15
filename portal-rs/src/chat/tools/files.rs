//! Editing the conversation's files (file_tools.py): the browser sends the
//! current files; view_file / str_replace / create_file work on them in
//! memory, and every touched file goes back once, in its final state.
use serde_json::{json, Value};

use crate::chat::history::py_str;

pub const MAX_FILES: usize = 40;
pub const MAX_FILE_CHARS: usize = 400_000;
pub const MAX_TOTAL_CHARS: usize = 1_200_000;
const VIEW_MAX_LINES: i64 = 400;
const SNIPPET_CONTEXT: usize = 4;
pub const FILE_TOOL_NAMES: [&str; 3] = ["view_file", "str_replace", "create_file"];
const REWRITE_KEPT_MAX: f64 = 0.5;

/// The conversation's files, in their order (a Python dict).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Files(pub Vec<(String, String)>);

impl Files {
    pub fn get(&self, name: &str) -> Option<&String> {
        self.0.iter().find(|(n, _)| n == name).map(|(_, c)| c)
    }
    fn set(&mut self, name: &str, content: String) {
        match self.0.iter_mut().find(|(n, _)| n == name) {
            Some(e) => e.1 = content,
            None => self.0.push((name.to_string(), content)),
        }
    }
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

/// {path: original} of every touched file (None = it did not exist), in order.
pub type Changes = Vec<(String, Option<String>)>;

fn record(changes: &mut Changes, path: &str, original: Option<String>) {
    if !changes.iter().any(|(p, _)| p == path) {
        changes.push((path.to_string(), original));
    }
}

/// `parse_files`: {path: content} from the request's `files`, bounded.
pub fn parse_files(raw: Option<&Value>) -> Files {
    let mut files = Files::default();
    let Some(list) = raw.and_then(Value::as_array) else { return files };
    let mut total = 0usize;
    for f in list.iter().take(MAX_FILES) {
        let Some(o) = f.as_object() else { continue };
        let name: String = crate::memory::py_strip(&o.get("name").filter(|v| crate::keys::truthy(v)).map(py_str).unwrap_or_default()).chars().take(200).collect();
        let Some(content) = o.get("content").and_then(Value::as_str) else { continue };
        let n = content.chars().count();
        if name.is_empty() || n > MAX_FILE_CHARS {
            continue;
        }
        if total + n > MAX_TOTAL_CHARS {
            break;
        }
        total += n;
        files.set(&name, content.to_string());
    }
    files
}

pub fn tools() -> Vec<Value> {
    vec![
        json!({"type": "function", "function": {
            "name": "view_file",
            "description": "Show the CURRENT content of a file of this conversation, with line numbers. Call it before editing when you are not sure of the exact text: an edit must copy the file exactly.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "File name, as listed."},
                "start_line": {"type": "integer", "description": "First line (1-based, optional)."},
                "end_line": {"type": "integer", "description": "Last line (optional)."}},
                "required": ["path"]}}}),
        json!({"type": "function", "function": {
            "name": "str_replace",
            "description": "Edit a file by replacing ONE exact passage. `old_str` must match the file character for character (indentation included) and appear exactly once — add surrounding lines to make it unique. Prefer this to rewriting the file. Several changes = several calls.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "old_str": {"type": "string", "description": "Exact text to replace."},
                "new_str": {"type": "string", "description": "Replacement text (may be empty)."}},
                "required": ["path", "old_str", "new_str"]}}}),
        json!({"type": "function", "function": {
            "name": "create_file",
            "description": "Create a new file, or replace a file entirely when most of it changes. For a localized change use str_replace.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "The COMPLETE file."}},
                "required": ["path", "content"]}}}),
    ]
}

/// `files_note`: the system note listing the files, for the tool decision.
pub fn files_note(files: &Files) -> String {
    let listing = files.0.iter().map(|(n, c)| format!("{n} ({} lines)", c.matches('\n').count() + 1)).collect::<Vec<_>>().join(", ");
    format!("Files of this conversation (current versions): {listing}. The user is asking for a change: APPLY it now with the file tools — str_replace (exact, unique old_str) for each modification, view_file first if you need the current text, create_file only for a NEW file. This overrides any instruction to output a file complete: never rewrite a whole file for a localized change. When every change is applied, answer with a short summary.")
}

fn clean_path(v: Option<&Value>) -> String {
    let raw = v.filter(|v| crate::keys::truthy(v)).map(py_str).unwrap_or_default();
    crate::memory::py_strip(&raw).trim_matches('`').to_string()
}

/// `_resolve`: exact name, else a suffix match.
fn resolve(files: &Files, path: &str) -> Option<String> {
    if files.get(path).is_some() {
        return Some(path.to_string());
    }
    files.0.iter().find(|(n, _)| n.ends_with(&format!("/{path}")) || path.ends_with(&format!("/{n}"))).map(|(n, _)| n.clone())
}

fn numbered(text: &str, start: usize) -> String {
    text.split('\n').enumerate().map(|(i, l)| format!("{:>6}\t{l}", start + i)).collect::<Vec<_>>().join("\n")
}

fn line_of(content: &str, byte_index: usize) -> usize {
    content[..byte_index].matches('\n').count() + 1
}

pub fn announce(name: &str, args: &Value) -> Value {
    let path: String = args.get("path").filter(|v| crate::keys::truthy(v)).map(py_str).unwrap_or_default().chars().take(200).collect();
    json!({"step": if name != "view_file" { "file_edit" } else { "file_view" }, "tool": "files", "question": path})
}

/// Python's `repr()` of a string (`{x!r}`).
fn py_repr_str(v: Option<&Value>) -> String {
    match v {
        None => "None".into(),
        Some(v) => crate::handlers::py_repr(v),
    }
}

fn int_arg(args: &Value, k: &str, default: i64) -> i64 {
    match args.get(k) {
        Some(v) if crate::keys::truthy(v) => crate::handlers::py_int(v).unwrap_or(default),
        _ => default,
    }
}

/// `exec_file_tool`: (result for the model, error or None); `files` edited in place.
pub fn exec(name: &str, args: &Value, files: &mut Files, changes: &mut Changes) -> (String, Option<&'static str>) {
    let empty = json!({});
    let args = if args.is_object() { args } else { &empty };
    if name == "create_file" {
        let path: String = clean_path(args.get("path")).chars().take(200).collect();
        let Some(content) = args.get("content").and_then(Value::as_str) else {
            return ("Error: create_file needs `path` and `content`.".into(), Some("invalid arguments"));
        };
        if path.is_empty() {
            return ("Error: create_file needs `path` and `content`.".into(), Some("invalid arguments"));
        }
        if content.chars().count() > MAX_FILE_CHARS {
            return ("Error: file too large.".into(), Some("file too large"));
        }
        let target = resolve(files, &path).unwrap_or(path);
        if let Some(old) = files.get(&target) {
            if mostly_kept(old, content) {
                return (format!("Error: {target} already exists and this rewrite keeps most of it unchanged. Use str_replace for each change instead (several calls in one round are fine). No change made."), Some("rewrite refused"));
            }
        }
        record(changes, &target, files.get(&target).cloned());
        files.set(&target, content.to_string());
        return (format!("File {target} written ({} lines).", content.matches('\n').count() + 1), None);
    }
    let raw_path = clean_path(args.get("path"));
    let Some(path) = resolve(files, &raw_path) else {
        let known = if files.is_empty() { "none".to_string() } else { files.0.iter().map(|(n, _)| n.clone()).collect::<Vec<_>>().join(", ") };
        return (format!("Error: no file named {}. Files: {known}.", py_repr_str(args.get("path"))), Some("unknown file"));
    };
    let content = files.get(&path).cloned().unwrap_or_default();
    match name {
        "view_file" => {
            let lines: Vec<&str> = content.split('\n').collect();
            let n = lines.len() as i64;
            let start = int_arg(args, "start_line", 1).max(1);
            let end = int_arg(args, "end_line", 0);
            let end = n.min(if end >= start { end } else { start + VIEW_MAX_LINES - 1 }).min(start + VIEW_MAX_LINES - 1);
            let slice: Vec<&str> = if start <= n && end >= start { lines[(start - 1) as usize..end as usize].to_vec() } else { Vec::new() };
            let body = numbered(&slice.join("\n"), start as usize);
            let more = if end < n { format!("\n[lines {start}-{end} of {n}; call again with start_line={} for the rest]", end + 1) } else { String::new() };
            (format!("{path}:\n{body}{more}"), None)
        }
        "str_replace" => {
            let old = match args.get("old_str") {
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                _ => return ("Error: `old_str` must be a non-empty string.".into(), Some("invalid arguments")),
            };
            let new = match args.get("new_str") {
                None => String::new(),
                Some(Value::String(s)) => s.clone(),
                Some(v) => py_str(v),
            };
            let count = content.matches(old.as_str()).count();
            if count == 0 {
                let strip_lines = |t: &str| t.split('\n').map(crate::memory::py_strip).collect::<Vec<_>>().join("\n");
                let stripped = strip_lines(&old);
                let hint = if !stripped.is_empty() && strip_lines(&content).contains(&stripped) {
                    " The text exists with DIFFERENT indentation or spacing: call view_file and copy it exactly."
                } else {
                    ""
                };
                return (format!("Error: old_str not found in {path}.{hint} No change made."), Some("text not found"));
            }
            if count > 1 {
                // Python's find(old, pos + 1): overlapping positions are listed.
                let mut where_ = Vec::new();
                let mut from = 0usize;
                while let Some(p) = content[from..].find(old.as_str()) {
                    let at = from + p;
                    where_.push(line_of(&content, at).to_string());
                    if where_.len() >= 8 {
                        break;
                    }
                    from = at + content[at..].chars().next().map_or(1, char::len_utf8);
                }
                return (format!("Error: old_str appears {count} times in {path} (lines {}). Include more surrounding lines so it is unique. No change made.", where_.join(", ")), Some("ambiguous text"));
            }
            let pos = content.find(old.as_str()).unwrap_or(0);
            record(changes, &path, Some(content.clone()));
            let updated = format!("{}{new}{}", &content[..pos], &content[pos + old.len()..]);
            files.set(&path, updated.clone());
            let first = line_of(&updated, pos);
            let last = first + new.matches('\n').count();
            let lines: Vec<&str> = updated.split('\n').collect();
            let lo = first.saturating_sub(SNIPPET_CONTEXT).max(1);
            let hi = lines.len().min(last + SNIPPET_CONTEXT);
            (format!("Edited {path}. Snippet of the result:\n{}", numbered(&lines[lo - 1..hi].join("\n"), lo)), None)
        }
        _ => (format!("Error: unknown tool {name}."), Some("unknown tool")),
    }
}

/// `_mostly_kept`: a rewrite keeping half the old lines is a localized change in disguise.
pub fn mostly_kept(old: &str, new: &str) -> bool {
    let a: Vec<&str> = old.split('\n').collect();
    if a.len() < 20 {
        return false;
    }
    let b: Vec<&str> = new.split('\n').collect();
    let kept: usize = matching_blocks(&a, &b).iter().map(|m| m.2).sum();
    kept as f64 / a.len() as f64 >= REWRITE_KEPT_MAX
}

/// difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
/// (without the final sentinel): (i, j, size) triples.
pub fn matching_blocks(a: &[&str], b: &[&str]) -> Vec<(usize, usize, usize)> {
    let mut b2j: std::collections::HashMap<&str, Vec<usize>> = std::collections::HashMap::new();
    for (j, x) in b.iter().enumerate() {
        b2j.entry(*x).or_default().push(j);
    }
    // find_longest_match without junk: the longest, earliest-in-a, earliest-in-b block.
    let longest = |alo: usize, ahi: usize, blo: usize, bhi: usize| -> (usize, usize, usize) {
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
        let mut j2len: std::collections::HashMap<usize, usize> = std::collections::HashMap::new();
        for (i, x) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut newj2len = std::collections::HashMap::new();
            if let Some(js) = b2j.get(x) {
                for &j in js {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = j.checked_sub(1).and_then(|p| j2len.get(&p)).copied().unwrap_or(0) + 1;
                    newj2len.insert(j, k);
                    if k > bestsize {
                        besti = i + 1 - k;
                        bestj = j + 1 - k;
                        bestsize = k;
                    }
                }
            }
            j2len = newj2len;
        }
        (besti, bestj, bestsize)
    };
    let mut queue = vec![(0usize, a.len(), 0usize, b.len())];
    let mut blocks = Vec::new();
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = longest(alo, ahi, blo, bhi);
        if k > 0 {
            blocks.push((i, j, k));
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    blocks.sort();
    // Collapse adjacent blocks, as difflib does.
    let mut out: Vec<(usize, usize, usize)> = Vec::new();
    for (i, j, k) in blocks {
        match out.last_mut() {
            Some(l) if l.0 + l.2 == i && l.1 + l.2 == j => l.2 += k,
            _ => out.push((i, j, k)),
        }
    }
    out
}

/// `edits_note`: the final answer must not reprint the edited files.
pub fn edits_note(files: &Files, changes: &Changes) -> String {
    let touched: Vec<&str> = changes.iter().filter(|(p, orig)| files.get(p) != orig.as_ref()).map(|(p, _)| p.as_str()).collect();
    if touched.is_empty() {
        return String::new();
    }
    format!("\n\n---\n[The platform ALREADY applied your edits to: {}. The user sees the updated files and a diff. Do NOT output these files again; briefly explain what you changed, in the user's language.]", touched.join(", "))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Scenarios played by dgx-portal/file_tools.py: each call's result and the files after.
    #[test]
    fn matches_python_on_the_fixture() {
        let fx: Value = serde_json::from_str(include_str!("../../../tests/fixtures/file_tools.json")).unwrap();
        for sc in fx["scenarios"].as_array().unwrap() {
            let mut files = parse_files(Some(&sc["files"]));
            let mut changes = Changes::new();
            for step in sc["steps"].as_array().unwrap() {
                let (text, err) = exec(step["name"].as_str().unwrap(), &step["args"], &mut files, &mut changes);
                assert_eq!(text, step["text"].as_str().unwrap(), "{} {}", sc["name"], step["name"]);
                assert_eq!(err.map(str::to_string), step["error"].as_str().map(str::to_string), "{}", sc["name"]);
            }
            let got: Vec<Value> = files.0.iter().map(|(n, c)| json!([n, c])).collect();
            assert_eq!(Value::Array(got), sc["after"], "{}", sc["name"]);
            assert_eq!(edits_note(&files, &changes), sc["note"].as_str().unwrap(), "{}", sc["name"]);
        }
        for c in fx["kept"].as_array().unwrap() {
            assert_eq!(mostly_kept(c["old"].as_str().unwrap(), c["new"].as_str().unwrap()), c["out"].as_bool().unwrap(), "{}", c["name"]);
        }
        for c in fx["blocks"].as_array().unwrap() {
            let a: Vec<&str> = c["a"].as_array().unwrap().iter().map(|x| x.as_str().unwrap()).collect();
            let b: Vec<&str> = c["b"].as_array().unwrap().iter().map(|x| x.as_str().unwrap()).collect();
            let got: Vec<Value> = matching_blocks(&a, &b).iter().map(|(i, j, k)| json!([i, j, k])).collect();
            assert_eq!(Value::Array(got), c["out"], "{:?} {:?}", a, b);
        }
    }
}
