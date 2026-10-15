//! Skill packages, parity with dgx-portal skills_store.py and the SSRF guard
//! of mcp_client.py. This file holds the pure parts — each checked against
//! outputs of the Python functions themselves (tests/fixtures/skills_ref.json):
//! SKILL.md front matter, names, zip member paths, Git URLs, and the
//! « is this address internal? » rule of Python's `ipaddress`.
use std::net::IpAddr;

use serde_json::{json, Map, Value};

use crate::memnorm::is_word;

pub const NAME_MAX: usize = 64;
pub const DESCRIPTION_MAX: usize = 1024;
pub const INSTRUCTIONS_MAX: usize = 100_000;

/// Raised refusals, in French like every server message.
#[derive(Debug, Clone, PartialEq)]
pub struct ImportError(pub String);

/// Python's `str.strip()` / `isspace()`.
fn py_space(c: char) -> bool {
    crate::memory::py_is_space(c)
}

// ── SKILL.md front matter ───────────────────────────────────────────────────

/// `key: value` as `^([A-Za-z_][\w-]*)\s*:\s*(.*)$` matches it.
fn key_value(line: &str) -> Option<(String, String)> {
    let mut chars = line.char_indices().peekable();
    let (_, first) = chars.next()?;
    if !(first.is_ascii_alphabetic() || first == '_') {
        return None;
    }
    let mut end = first.len_utf8();
    while let Some(&(i, c)) = chars.peek() {
        if is_word(c) || c == '-' {
            end = i + c.len_utf8();
            chars.next();
        } else {
            break;
        }
    }
    let key = &line[..end];
    let rest = line[end..].trim_start_matches(py_space);
    let value = rest.strip_prefix(':')?;
    Some((key.to_string(), value.trim_start_matches(py_space).to_string()))
}

/// `parse_front_matter`: (meta, body) of a SKILL.md — the small YAML subset
/// SKILL.md files use (`key: value`, quoted values, `>` / `|` blocks).
pub fn parse_front_matter(text: &str) -> (Map<String, Value>, String) {
    let mut meta: Map<String, Value> = Map::new();
    let text = text.trim_start_matches('\u{feff}');
    if !text.starts_with("---") {
        return (meta, text.to_string());
    }
    let lines: Vec<&str> = text.split('\n').collect();
    let Some(end) = (1..lines.len()).find(|&i| lines[i].trim_matches(py_space) == "---") else {
        return (meta, text.to_string());
    };
    let mut key: Option<String> = None;
    let mut block: Vec<&str> = Vec::new();
    let flush = |meta: &mut Map<String, Value>, key: &Option<String>, block: &Vec<&str>| {
        let (Some(k), false) = (key, block.is_empty()) else { return };
        let current = meta.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
        let stripped: Vec<&str> = block.iter().map(|l| l.trim_matches(py_space)).collect();
        let joined = if matches!(current.as_str(), ">" | "|" | ">-" | "|-" | ">+" | "|+") {
            let sep = if current.starts_with('|') { "\n" } else { " " };
            stripped.join(sep)
        } else {
            std::iter::once(current.as_str()).chain(stripped.iter().copied()).collect::<Vec<_>>().join(" ")
        };
        meta.insert(k.clone(), Value::String(joined.trim_matches(py_space).to_string()));
    };
    for line in &lines[1..end] {
        if (line.starts_with(' ') || line.starts_with('\t')) && key.is_some() {
            block.push(line);
            continue;
        }
        flush(&mut meta, &key, &block);
        match key_value(line) {
            None => {
                key = None;
                block.clear();
            }
            Some((k, v)) => {
                let mut value = v.trim_matches(py_space).to_string();
                let cs: Vec<char> = value.chars().collect();
                if cs.len() >= 2 && cs[0] == cs[cs.len() - 1] && (cs[0] == '"' || cs[0] == '\'') {
                    value = cs[1..cs.len() - 1].iter().collect();
                }
                meta.insert(k.clone(), Value::String(value));
                key = Some(k);
                block.clear();
            }
        }
    }
    flush(&mut meta, &key, &block);
    let body = lines[end + 1..].join("\n").trim_start_matches('\n').to_string();
    (meta, body)
}

/// Python's `str(x or '')` for a front-matter or JSON value.
pub fn py_str_or_empty(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(false)) => String::new(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => String::new(),
        Some(other) => other.to_string(),
    }
}

/// `normalize_name`: lowercase letters, digits and hyphens, 64 at most.
pub fn normalize_name(name: Option<&Value>) -> String {
    let lowered = py_str_or_empty(name).trim_matches(py_space).to_lowercase();
    let mut out = String::new();
    let mut in_run = false;
    for c in lowered.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' {
            out.push(c);
            in_run = false;
        } else if !in_run {
            out.push('-');
            in_run = true;
        }
    }
    let trimmed = out.trim_matches('-');
    let mut collapsed = String::new();
    for c in trimmed.chars() {
        if c == '-' && collapsed.ends_with('-') {
            continue;
        }
        collapsed.push(c);
    }
    collapsed.chars().take(NAME_MAX).collect()
}

/// `manual_skill_md`: the SKILL.md of a skill written in the interface.
pub fn manual_skill_md(name: &str, description: &str, instructions: &str) -> String {
    let desc = description.replace('\n', " ").replace('"', "'");
    format!("---\nname: {name}\ndescription: \"{desc}\"\n---\n\n{}\n", instructions.trim_matches(py_space))
}

// ── Zip member paths ────────────────────────────────────────────────────────

/// `posixpath.normpath` for a relative path (absolute ones are refused before).
pub fn normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".into();
    }
    let mut parts: Vec<&str> = Vec::new();
    for comp in path.split('/') {
        match comp {
            "" | "." => {}
            ".." if parts.last().is_some_and(|p| *p != "..") => {
                parts.pop();
            }
            other => parts.push(other),
        }
    }
    if parts.is_empty() { ".".into() } else { parts.join("/") }
}

/// `_safe_path`: a zip member path, or None when it must not be extracted.
pub fn safe_path(name: &str) -> Option<String> {
    if name.is_empty() || name.ends_with('/') || name.contains('\0') || name.contains('\\') {
        return None;
    }
    let mut cs = name.chars();
    let drive = matches!((cs.next(), cs.next()), (Some(c), Some(':')) if c.is_ascii_alphabetic());
    if name.starts_with('/') || drive {
        return None;
    }
    let norm = normpath(name);
    if norm.starts_with("..") || format!("/{norm}/").contains("/../") {
        return None;
    }
    if norm.split('/').any(|p| matches!(p, ".git" | "__MACOSX" | "node_modules")) {
        return None;
    }
    Some(norm)
}

// ── Git URLs ────────────────────────────────────────────────────────────────

/// `urllib.parse.quote(s)` (safe='/'): unreserved characters and '/' kept.
pub fn quote(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-' | b'~' | b'/') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// What `urlparse` gives this code: (scheme, lowercase hostname, path).
pub fn url_parts(url: &str) -> (String, String, String) {
    let Some((scheme, rest)) = url.split_once("://").filter(|(s, _)| {
        !s.is_empty() && s.chars().next().is_some_and(|c| c.is_ascii_alphabetic())
            && s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
    }) else {
        return (String::new(), String::new(), String::new());
    };
    let netloc_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (netloc, after) = rest.split_at(netloc_end);
    let path_end = after.find(['?', '#']).unwrap_or(after.len());
    let path = &after[..path_end];
    let hostport = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let host = if let Some(stripped) = hostport.strip_prefix('[') {
        stripped.split(']').next().unwrap_or_default()
    } else {
        hostport.split(':').next().unwrap_or_default()
    };
    (scheme.to_lowercase(), host.to_lowercase(), path.to_string())
}

/// `parse_git_url`: the archive URLs to try (main, then master when the ref is
/// unknown), the ref, the folder, the canonical URL and the host.
pub fn parse_git_url(url: &str, reference: &str, path: &str) -> Result<Value, ImportError> {
    let url = url.trim_matches(py_space);
    let (scheme, host, upath) = url_parts(url);
    if scheme != "https" || host.is_empty() {
        return Err(ImportError("L'adresse doit être un dépôt en https://.".into()));
    }
    let segs: Vec<&str> = upath.split('/').filter(|s| !s.is_empty()).collect();
    if url.to_lowercase().ends_with(".zip") {
        return Ok(json!({"archives": [[url, reference]], "ref": reference, "path": path, "canonical": url, "host": host}));
    }
    if segs.len() < 2 {
        return Err(ImportError("Adresse de dépôt incomplète (attendu : hôte/propriétaire/dépôt).".into()));
    }
    let refs_for = |found: &str| -> Vec<String> {
        let r = py_strip_owned(if !reference.is_empty() { reference } else { found });
        if r.is_empty() { vec!["main".into(), "master".into()] } else { vec![r] }
    };
    if host == "github.com" {
        let owner = segs[0];
        let repo = segs[1].strip_suffix(".git").unwrap_or(segs[1]);
        let (found_ref, found_path) = if segs.len() >= 4 && matches!(segs[2], "tree" | "blob") {
            (segs[3].to_string(), segs[4..].join("/"))
        } else {
            (String::new(), String::new())
        };
        let refs = refs_for(&found_ref);
        let archives: Vec<Value> = refs
            .iter()
            .map(|r| json!([format!("https://codeload.github.com/{owner}/{repo}/zip/{}", quote(r)), r]))
            .collect();
        return Ok(json!({
            "archives": archives,
            "ref": if refs.len() == 1 { refs[0].clone() } else { String::new() },
            "path": if path.is_empty() { found_path } else { path.to_string() },
            "canonical": format!("https://github.com/{owner}/{repo}"),
            "host": host,
            "github": [owner, repo],
        }));
    }
    let (project, found_ref, found_path): (Vec<&str>, String, String) = if let Some(k) = segs.iter().position(|s| *s == "-") {
        let found_ref = if segs.len() > k + 2 && matches!(segs[k + 1], "tree" | "blob") { segs[k + 2].to_string() } else { String::new() };
        let found_path = if !found_ref.is_empty() { segs.get(k + 3..).map(|s| s.join("/")).unwrap_or_default() } else { String::new() };
        (segs[..k].to_vec(), found_ref, found_path)
    } else if segs.len() >= 5 && segs[2] == "src" && matches!(segs[3], "branch" | "tag" | "commit") {
        (segs[..2].to_vec(), segs[4].to_string(), segs[5..].join("/"))
    } else {
        (segs.clone(), String::new(), String::new())
    };
    let project: Vec<String> = project.iter().map(|s| s.strip_suffix(".git").unwrap_or(s).to_string()).collect();
    let refs = refs_for(&found_ref);
    let archives: Vec<Value> = if host.contains("gitlab") {
        let name = project.last().cloned().unwrap_or_default();
        refs.iter()
            .map(|r| json!([format!("https://{host}/{}/-/archive/{}/{name}-{}.zip", project.join("/"), quote(r), quote(r)), r]))
            .collect()
    } else {
        let first_two = project.iter().take(2).cloned().collect::<Vec<_>>().join("/");
        refs.iter().map(|r| json!([format!("https://{host}/{first_two}/archive/{}.zip", quote(r)), r])).collect()
    };
    Ok(json!({
        "archives": archives,
        "ref": if refs.len() == 1 { refs[0].clone() } else { String::new() },
        "path": if path.is_empty() { found_path } else { path.to_string() },
        "canonical": format!("https://{host}/{}", project.join("/")),
        "host": host,
    }))
}

fn py_strip_owned(s: &str) -> String {
    s.trim_matches(py_space).to_string()
}

// ── Archives ────────────────────────────────────────────────────────────────

pub const MAX_ARCHIVE_BYTES: usize = 25 * 1024 * 1024;
pub const MAX_ENTRIES: usize = 3000;
pub const MAX_UNCOMPRESSED: u64 = 60 * 1024 * 1024;
pub const MAX_FILES_PER_SKILL: usize = 200;
pub const MAX_FILE_BYTES: u64 = 1024 * 1024;
pub const MAX_SKILL_BYTES: usize = 5 * 1024 * 1024;
pub const MAX_SKILLS_PER_IMPORT: usize = 50;

/// One skill read from files: name, description, instructions, version, files.
#[derive(Debug, Clone, PartialEq)]
pub struct Skill {
    pub name: String,
    pub description: String,
    pub instructions: String,
    pub version: String,
    /// path → text, first-seen order
    pub files: Vec<(String, String)>,
}

/// `skill_from_files`: ONE skill folder (SKILL.md at its root) → a skill.
pub fn skill_from_files(files: Vec<(String, String)>, folder_name: &str) -> Result<Skill, ImportError> {
    let Some((_, md)) = files.iter().find(|(p, _)| p.to_lowercase() == "skill.md") else {
        return Err(ImportError("SKILL.md manquant.".into()));
    };
    let (meta, body) = parse_front_matter(md);
    let name_src = meta.get("name").filter(|v| crate::keys::truthy(v)).cloned().unwrap_or_else(|| json!(folder_name));
    let name = normalize_name(Some(&name_src));
    let description: String = py_str_or_empty(meta.get("description")).trim_matches(py_space).chars().take(DESCRIPTION_MAX).collect();
    if name.is_empty() {
        return Err(ImportError("Nom de compétence manquant (champ « name » du SKILL.md).".into()));
    }
    if description.is_empty() {
        return Err(ImportError(format!("« {name} » : description manquante (champ « description »).")));
    }
    if body.trim_matches(py_space).is_empty() {
        return Err(ImportError(format!("« {name} » : SKILL.md sans instructions.")));
    }
    Ok(Skill {
        name,
        description,
        instructions: body.trim_matches(py_space).chars().take(INSTRUCTIONS_MAX).collect(),
        version: py_str_or_empty(meta.get("version")).trim_matches(py_space).chars().take(40).collect(),
        files,
    })
}

/// `_decode`: UTF-8 text, or None for a binary file (a NUL in its first 8 KiB).
fn decode(data: &[u8]) -> Option<String> {
    if data[..data.len().min(8192)].contains(&0) {
        return None;
    }
    String::from_utf8(data.to_vec()).ok()
}

fn dirname(p: &str) -> &str {
    p.rsplit_once('/').map_or("", |(d, _)| d)
}

fn basename(p: &str) -> &str {
    p.rsplit_once('/').map_or(p, |(_, b)| b)
}

/// `skills_from_zip`: every skill of a .zip, and the folders refused with why.
/// Guards: archive size, entry count, declared uncompressed total, per-file
/// size, per-skill size and file count; symlinks and unsafe paths skipped.
pub fn skills_from_zip(data: &[u8], subpath: &str) -> Result<(Vec<Skill>, Vec<(String, String)>), ImportError> {
    use std::io::Read;
    if data.len() > MAX_ARCHIVE_BYTES {
        return Err(ImportError("Archive trop volumineuse (25 Mo maximum).".into()));
    }
    let mut zf = zip::ZipArchive::new(std::io::Cursor::new(data))
        .map_err(|_| ImportError("Ce fichier n'est pas une archive .zip valide.".into()))?;
    if zf.len() > MAX_ENTRIES {
        return Err(ImportError("Archive trop volumineuse (trop de fichiers).".into()));
    }
    // (path, index, declared size), first-seen order; a repeated path keeps its
    // first position and its last entry, as a Python dict does.
    let mut members: Vec<(String, usize, u64)> = Vec::new();
    let mut total_declared = 0u64;
    let mut entries = Vec::new();
    for i in 0..zf.len() {
        let f = zf.by_index_raw(i).map_err(|_| ImportError("Ce fichier n'est pas une archive .zip valide.".into()))?;
        total_declared += f.size();
        entries.push((f.name().to_string(), f.size(), f.unix_mode()));
    }
    if total_declared > MAX_UNCOMPRESSED {
        return Err(ImportError("Archive trop volumineuse une fois décompressée.".into()));
    }
    for (i, (name, size, mode)) in entries.into_iter().enumerate() {
        if mode.is_some_and(|m| m & 0o170000 == 0o120000) {
            continue; // a symlink: its « content » is a target path, never followed
        }
        let Some(path) = safe_path(&name) else { continue };
        match members.iter_mut().find(|(p, _, _)| *p == path) {
            Some(slot) => *slot = (path, i, size),
            None => members.push((path, i, size)),
        }
    }
    // GitHub/GitLab archives wrap everything in « repo-ref/ »: drop it.
    let tops: std::collections::BTreeSet<&str> = members.iter().map(|(p, _, _)| p.split('/').next().unwrap_or("")).collect();
    if tops.len() == 1 && members.iter().all(|(p, _, _)| p.contains('/')) {
        let prefix = format!("{}/", tops.iter().next().unwrap());
        members = members.into_iter().map(|(p, i, s)| (p[prefix.len()..].to_string(), i, s)).collect();
    }
    let subpath = subpath.trim_matches('/');
    if !subpath.is_empty() {
        let pre = format!("{subpath}/");
        members = members.into_iter().filter(|(p, _, _)| p.starts_with(&pre)).map(|(p, i, s)| (p[pre.len()..].to_string(), i, s)).collect();
        if members.is_empty() {
            return Err(ImportError(format!("Dossier « {subpath} » introuvable dans le dépôt.")));
        }
    }
    let mut roots: Vec<String> = members
        .iter()
        .filter(|(p, _, _)| basename(p).to_lowercase() == "skill.md")
        .map(|(p, _, _)| dirname(p).to_string())
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect();
    // A skill inside another skill's folder belongs to it as a file.
    let all = roots.clone();
    roots.retain(|r| !all.iter().any(|o| !o.is_empty() && r != o && r.starts_with(&format!("{o}/"))));
    if roots.is_empty() {
        return Err(ImportError("Aucun SKILL.md trouvé dans l'archive.".into()));
    }
    let mut skills = Vec::new();
    let mut skipped = Vec::new();
    for root in roots.iter().take(MAX_SKILLS_PER_IMPORT) {
        let folder = if basename(root).is_empty() { "skill".to_string() } else { basename(root).to_string() };
        let mut files: Vec<(String, String)> = Vec::new();
        let mut total = 0usize;
        for (path, idx, size) in &members {
            if !root.is_empty() && !path.starts_with(&format!("{root}/")) {
                continue;
            }
            let rel = if root.is_empty() { path.clone() } else { path[root.len() + 1..].to_string() };
            if *size > MAX_FILE_BYTES {
                continue;
            }
            let mut buf = Vec::new();
            let read_ok = zf.by_index(*idx).ok().is_some_and(|f| f.take(MAX_FILE_BYTES + 1).read_to_end(&mut buf).is_ok());
            let Some(text) = read_ok.then(|| decode(&buf)).flatten() else { continue };
            total += text.len();
            if total > MAX_SKILL_BYTES || files.len() >= MAX_FILES_PER_SKILL {
                break;
            }
            match files.iter_mut().find(|(p, _)| *p == rel) {
                Some(slot) => slot.1 = text,
                None => files.push((rel, text)),
            }
        }
        match skill_from_files(files, &folder) {
            Ok(skill) => skills.push(skill),
            Err(ImportError(e)) => skipped.push((folder, e)),
        }
    }
    for root in roots.iter().skip(MAX_SKILLS_PER_IMPORT) {
        skipped.push((basename(root).to_string(), "Trop de compétences dans une seule archive.".into()));
    }
    Ok((skills, skipped))
}

/// `build_zip`: one skill as `<name>/<path>` entries, sorted, deflated.
pub fn build_zip(skill_name: &str, files: &[(String, String)]) -> Vec<u8> {
    use std::io::Write;
    let mut sorted: Vec<&(String, String)> = files.iter().collect();
    sorted.sort_by(|a, b| a.0.cmp(&b.0));
    let mut buf = std::io::Cursor::new(Vec::new());
    {
        let mut w = zip::ZipWriter::new(&mut buf);
        let opts: zip::write::SimpleFileOptions = zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
        for (path, content) in sorted {
            if w.start_file(format!("{skill_name}/{path}"), opts).is_ok() {
                let _ = w.write_all(content.as_bytes());
            }
        }
        let _ = w.finish();
    }
    buf.into_inner()
}

// ── « Is this address internal? » (mcp_client._is_blocked_ip) ───────────────

/// Host names never fetched, whatever they resolve to (mcp_client._BLOCKED_HOSTNAMES).
pub const BLOCKED_HOSTNAMES: [&str; 7] =
    ["dgx-portal", "dgx-portal-frontend", "host.docker.internal", "litellm", "localhost", "postgres", "vllm-runner"];

fn in_v4(ip: u32, net: &str) -> bool {
    let (addr, len) = net.split_once('/').unwrap_or((net, "32"));
    let base: u32 = addr.parse::<std::net::Ipv4Addr>().map(u32::from).unwrap_or(0);
    let len: u32 = len.parse().unwrap_or(32);
    let mask = if len == 0 { 0 } else { u32::MAX << (32 - len) };
    ip & mask == base & mask
}

fn in_v6(ip: u128, net: &str) -> bool {
    let (addr, len) = net.split_once('/').unwrap_or((net, "128"));
    let base: u128 = addr.parse::<std::net::Ipv6Addr>().map(u128::from).unwrap_or(0);
    let len: u32 = len.parse().unwrap_or(128);
    let mask = if len == 0 { 0 } else { u128::MAX << (128 - len) };
    ip & mask == base & mask
}

const V4_PRIVATE: [&str; 14] = [
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.0.170/31",
    "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4",
    "255.255.255.255/32",
];
const V4_PRIVATE_EXCEPTIONS: [&str; 2] = ["192.0.0.9/32", "192.0.0.10/32"];
const V6_PRIVATE: [&str; 11] = [
    "::1/128", "::/128", "::ffff:0.0.0.0/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "2002::/16",
    "3fff::/20", "fc00::/7", "fe80::/10",
];
const V6_PRIVATE_EXCEPTIONS: [&str; 6] =
    ["2001:1::1/128", "2001:1::2/128", "2001:3::/32", "2001:4:112::/48", "2001:20::/28", "2001:30::/28"];
const V6_RESERVED: [&str; 15] = [
    "::/8", "100::/8", "200::/7", "400::/6", "800::/5", "1000::/4", "4000::/3", "6000::/3", "8000::/3", "a000::/3",
    "c000::/3", "e000::/4", "f000::/5", "f800::/6", "fe00::/9",
];

fn v4_blocked_python(ip: u32) -> bool {
    let private = V4_PRIVATE.iter().any(|n| in_v4(ip, n)) && !V4_PRIVATE_EXCEPTIONS.iter().any(|n| in_v4(ip, n));
    let reserved = in_v4(ip, "240.0.0.0/4");
    let multicast = in_v4(ip, "224.0.0.0/4");
    let link_local = in_v4(ip, "169.254.0.0/16");
    let loopback = in_v4(ip, "127.0.0.0/8");
    let unspecified = ip == 0;
    loopback || link_local || private || reserved || multicast || unspecified
}

/// `_is_blocked_ip`: loopback, link-local, private, reserved, multicast,
/// unspecified (Python 3.12's `ipaddress` ranges) or CGNAT 100.64.0.0/10.
/// An unreadable address is refused.
pub fn is_blocked_ip(text: &str) -> bool {
    let Ok(ip) = text.parse::<IpAddr>() else { return true };
    match ip {
        IpAddr::V4(v4) => {
            let n = u32::from(v4);
            v4_blocked_python(n) || in_v4(n, "100.64.0.0/10")
        }
        IpAddr::V6(v6) => {
            let n = u128::from(v6);
            // An IPv4-mapped address follows its IPv4 rules (CGNAT excepted:
            // an IPv6 address is never « in » an IPv4 network).
            if n >> 32 == 0xFFFF {
                return v4_blocked_python((n & 0xFFFF_FFFF) as u32);
            }
            let private = V6_PRIVATE.iter().any(|x| in_v6(n, x)) && !V6_PRIVATE_EXCEPTIONS.iter().any(|x| in_v6(n, x));
            let reserved = V6_RESERVED.iter().any(|x| in_v6(n, x));
            let multicast = in_v6(n, "ff00::/8");
            let link_local = in_v6(n, "fe80::/10");
            n == 1 || link_local || private || reserved || multicast || n == 0
        }
    }
}

// ── Downloads through the SSRF guard ───────────────────────────────────────

pub const DOWNLOAD_TIMEOUT_S: u64 = 30;

/// `resolve_validated_mcp_ip`: https only, never an internal host name nor the
/// deployment's directory server, and EVERY resolved address public; the first
/// one is pinned for the connection (no second resolution: no DNS rebinding).
pub async fn validated_target(url: &str, ldap_host: &str) -> Result<(String, std::net::SocketAddr), String> {
    let (scheme, host, _) = url_parts(url);
    if scheme != "https" {
        return Err("L'URL doit être en https://.".into());
    }
    if host.is_empty() {
        return Err("URL invalide (pas d'hôte).".into());
    }
    if BLOCKED_HOSTNAMES.contains(&host.as_str()) || (!ldap_host.is_empty() && host == ldap_host) {
        return Err("Cet hôte n'est pas autorisé.".into());
    }
    let port = url_port(url).unwrap_or(443);
    let addrs: Vec<std::net::SocketAddr> = match tokio::net::lookup_host((host.as_str(), port)).await {
        Ok(a) => a.collect(),
        Err(_) => return Err("Nom d'hôte introuvable.".into()),
    };
    let mut pinned = None;
    for a in &addrs {
        if is_blocked_ip(&a.ip().to_string()) {
            return Err("Cette adresse pointe vers un réseau interne/privé, refusée.".into());
        }
        if pinned.is_none() {
            pinned = Some(*a);
        }
    }
    pinned.map(|p| (host, p)).ok_or_else(|| "Nom d'hôte introuvable.".into())
}

/// The explicit port of an https URL, if any.
fn url_port(url: &str) -> Option<u16> {
    let rest = url.split_once("://")?.1;
    let netloc = &rest[..rest.find(['/', '?', '#']).unwrap_or(rest.len())];
    let hostport = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let port = if hostport.starts_with('[') { hostport.rsplit_once("]:")?.1 } else { hostport.rsplit_once(':')?.1 };
    port.parse().ok()
}

/// `_safe_get`: GET pinned to the validated address, no redirect, body bounded
/// to 25 MB. Returns (status, body) — the body only for a 200.
pub async fn safe_get(url: &str, accept: &str, ldap_host: &str) -> Result<(u16, Vec<u8>), ImportError> {
    let (host, addr) = validated_target(url, ldap_host).await.map_err(ImportError)?;
    let client = reqwest::Client::builder()
        .resolve(&host, addr)
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(DOWNLOAD_TIMEOUT_S))
        .build()
        .map_err(|_| ImportError("Téléchargement impossible (ConnectionError).".into()))?;
    let name = |e: &reqwest::Error| if e.is_timeout() { "ReadTimeout" } else { "ConnectionError" };
    let mut resp = client
        .get(url)
        .header(reqwest::header::ACCEPT, accept)
        .header(reqwest::header::USER_AGENT, "dgx-skills-import")
        .send()
        .await
        .map_err(|e| ImportError(format!("Téléchargement impossible ({}).", name(&e))))?;
    let status = resp.status().as_u16();
    if status != 200 {
        return Ok((status, Vec::new()));
    }
    let mut body = Vec::new();
    while let Some(chunk) = resp.chunk().await.map_err(|e| ImportError(format!("Téléchargement impossible ({}).", name(&e))))? {
        body.extend_from_slice(&chunk);
        if body.len() > MAX_ARCHIVE_BYTES {
            return Err(ImportError("Archive trop volumineuse (25 Mo maximum).".into()));
        }
    }
    Ok((200, body))
}

/// `_github_default_branch`: the repository's default branch, or '' when unknown.
async fn github_default_branch(owner: &str, repo: &str, ldap_host: &str) -> String {
    match safe_get(&format!("https://api.github.com/repos/{owner}/{repo}"), "application/vnd.github+json", ldap_host).await {
        Ok((200, body)) => serde_json::from_slice::<Value>(&body)
            .ok()
            .and_then(|v| v.get("default_branch").map(|b| py_str_or_empty(Some(b))))
            .unwrap_or_default(),
        _ => String::new(),
    }
}

/// `fetch_git_skills`: downloads a repository archive → (skills, skipped, source).
pub async fn fetch_git_skills(url: &str, reference: &str, path: &str, ldap_host: &str) -> Result<(Vec<Skill>, Vec<(String, String)>, Value), ImportError> {
    let mut src = parse_git_url(url, reference, path)?;
    let mut archives: Vec<(String, String)> = src["archives"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|a| (a[0].as_str().unwrap_or_default().to_string(), a[1].as_str().unwrap_or_default().to_string()))
        .collect();
    if let (Some(gh), true) = (src.get("github").cloned(), src["ref"].as_str().unwrap_or_default().is_empty()) {
        let (owner, repo) = (gh[0].as_str().unwrap_or_default().to_string(), gh[1].as_str().unwrap_or_default().to_string());
        let branch = github_default_branch(&owner, &repo, ldap_host).await;
        if !branch.is_empty() {
            archives = vec![(format!("https://codeload.github.com/{owner}/{repo}/zip/{}", quote(&branch)), branch)];
        }
    }
    let mut last = 0u16;
    for (archive, archive_ref) in archives {
        let (status, data) = safe_get(&archive, "application/zip", ldap_host).await?;
        if status == 200 {
            src["ref"] = json!(archive_ref);
            let (skills, skipped) = skills_from_zip(&data, src["path"].as_str().unwrap_or_default())?;
            return Ok((skills, skipped, json!({"url": src["canonical"], "ref": src["ref"], "path": src["path"]})));
        }
        last = status;
    }
    if last == 404 {
        return Err(ImportError("Dépôt ou branche introuvable (dépôt privé ?).".into()));
    }
    Err(ImportError(format!("Le dépôt a répondu HTTP {last}.")))
}

// ── Storage ─────────────────────────────────────────────────────────────────

pub const MAX_SKILLS: i64 = 50;

fn now_seconds() -> String {
    chrono::Utc::now().naive_utc().format("%Y-%m-%dT%H:%M:%S").to_string()
}

/// `save_skill`: inserts, or replaces when `replace`, a skill and its files.
/// Ok(id) or Err(reason) when it exists and is not replaced.
#[allow(clippy::too_many_arguments)]
pub async fn save_skill(
    db: &mut sqlx::SqliteConnection,
    username: &str,
    skill: &Skill,
    source: &str,
    source_url: Option<&str>,
    source_ref: Option<&str>,
    source_path: Option<&str>,
    replace: bool,
) -> Result<Result<i64, String>, sqlx::Error> {
    let now = now_seconds();
    let existing: Option<i64> = sqlx::query_scalar("SELECT id FROM skills WHERE username = ? AND name = ?")
        .bind(username)
        .bind(&skill.name)
        .fetch_optional(&mut *db)
        .await?;
    if existing.is_some() && !replace {
        return Ok(Err("existe déjà".into()));
    }
    let skill_id = match existing {
        Some(id) => {
            sqlx::query("UPDATE skills SET description = ?, instructions = ?, source = ?, source_url = ?, source_ref = ?, \
                         source_path = ?, version = ?, synced_at = ? WHERE id = ?")
                .bind(&skill.description).bind(&skill.instructions).bind(source).bind(source_url).bind(source_ref)
                .bind(source_path).bind(&skill.version).bind(&now).bind(id)
                .execute(&mut *db).await?;
            sqlx::query("DELETE FROM skill_files WHERE skill_id = ?").bind(id).execute(&mut *db).await?;
            id
        }
        None => sqlx::query(
            "INSERT INTO skills (username, name, description, instructions, created_at, source, source_url, source_ref, \
             source_path, version, synced_at, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        )
        .bind(username).bind(&skill.name).bind(&skill.description).bind(&skill.instructions).bind(&now).bind(source)
        .bind(source_url).bind(source_ref).bind(source_path).bind(&skill.version).bind(&now)
        .execute(&mut *db)
        .await?
        .last_insert_rowid(),
    };
    for (path, content) in &skill.files {
        sqlx::query("INSERT INTO skill_files (skill_id, username, path, content) VALUES (?, ?, ?, ?)")
            .bind(skill_id).bind(username).bind(path).bind(content)
            .execute(&mut *db).await?;
    }
    Ok(Ok(skill_id))
}

/// skills_routes._import: stores the imported skills; (HTTP status, answer).
#[allow(clippy::too_many_arguments)]
pub async fn import_skills(
    pool: &sqlx::SqlitePool,
    username: &str,
    skills: &[Skill],
    skipped: &[(String, String)],
    replace: bool,
    source: &str,
    url: Option<&str>,
    reference: Option<&str>,
    path: Option<&str>,
) -> Result<(u16, Value), sqlx::Error> {
    let mut tx = pool.begin().await?;
    let mut count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM skills WHERE username = ?").bind(username).fetch_one(&mut *tx).await?;
    let mut imported = Vec::new();
    let mut skipped: Vec<Value> = skipped.iter().map(|(n, why)| json!({"name": n, "reason": why})).collect();
    for skill in skills {
        let exists = sqlx::query_scalar::<_, i64>("SELECT 1 FROM skills WHERE username = ? AND name = ?")
            .bind(username).bind(&skill.name).fetch_optional(&mut *tx).await?.is_some();
        if !exists && count >= MAX_SKILLS {
            skipped.push(json!({"name": skill.name, "reason": format!("maximum {MAX_SKILLS} compétences atteint")}));
            continue;
        }
        match save_skill(&mut tx, username, skill, source, url, reference, path, replace).await? {
            Err(why) => skipped.push(json!({"name": skill.name, "reason": why})),
            Ok(id) => {
                if !exists {
                    count += 1;
                }
                imported.push(json!({"id": id, "name": skill.name}));
            }
        }
    }
    tx.commit().await?;
    if imported.is_empty() {
        return Ok((400, json!({"ok": false, "error": "Aucune compétence importée.", "skipped": skipped})));
    }
    Ok((200, json!({"ok": true, "imported": imported, "skipped": skipped})))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Value {
        serde_json::from_str(include_str!("../tests/fixtures/skills_ref.json")).unwrap()
    }

    #[test]
    fn front_matter_matches_python() {
        let mut bad = Vec::new();
        for c in fixture()["front_matter"].as_array().unwrap() {
            let (meta, body) = parse_front_matter(c["in"].as_str().unwrap());
            if Value::Object(meta.clone()) != c["meta"] || body != c["body"].as_str().unwrap() {
                bad.push(format!("{:?}: python {} {:?} rust {:?} {:?}", c["in"], c["meta"], c["body"], meta, body));
            }
        }
        assert!(bad.is_empty(), "{} mismatches: {:?}", bad.len(), bad.iter().take(3).collect::<Vec<_>>());
    }

    #[test]
    fn names_paths_and_manual_md_match_python() {
        let f = fixture();
        for c in f["names"].as_array().unwrap() {
            assert_eq!(normalize_name(Some(&c["in"])), c["out"].as_str().unwrap(), "{:?}", c["in"]);
        }
        for c in f["paths"].as_array().unwrap() {
            assert_eq!(json!(safe_path(c["in"].as_str().unwrap())), c["out"], "{:?}", c["in"]);
        }
        for c in f["manual_md"].as_array().unwrap() {
            let a = &c["in"];
            assert_eq!(manual_skill_md(a[0].as_str().unwrap(), a[1].as_str().unwrap(), a[2].as_str().unwrap()), c["out"].as_str().unwrap());
        }
        let hosts: Vec<&str> = f["blocked_hosts"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
        assert_eq!(hosts, BLOCKED_HOSTNAMES.to_vec());
    }

    #[test]
    fn git_urls_match_python() {
        for c in fixture()["git"].as_array().unwrap() {
            let a = &c["in"];
            let got = parse_git_url(a[0].as_str().unwrap(), a[1].as_str().unwrap(), a[2].as_str().unwrap());
            match (&got, c.get("ok"), c.get("err")) {
                (Ok(v), Some(ok), _) => assert_eq!(v, ok, "{:?}", a),
                (Err(ImportError(e)), _, Some(err)) => assert_eq!(e, err.as_str().unwrap(), "{:?}", a),
                _ => panic!("{a:?}: python {c} rust {got:?}"),
            }
        }
    }

    #[test]
    fn address_filter_matches_python_on_6947_addresses() {
        let mut bad = Vec::new();
        for c in fixture()["ips"].as_array().unwrap() {
            let ip = c["in"].as_str().unwrap();
            if is_blocked_ip(ip) != c["blocked"].as_bool().unwrap() {
                bad.push(format!("{ip}: python {} rust {}", c["blocked"], is_blocked_ip(ip)));
            }
        }
        assert!(bad.is_empty(), "{} mismatches: {:?}", bad.len(), bad.iter().take(5).collect::<Vec<_>>());
    }

    /// Archives built with Python's zipfile, traps included, and what
    /// skills_from_zip made of them (tests/fixtures/skills_zip.json).
    #[test]
    fn zip_reading_matches_python() {
        use base64::Engine;
        let cases: Vec<Value> = serde_json::from_str(include_str!("../tests/fixtures/skills_zip.json")).unwrap();
        for c in &cases {
            if c["label"] == "build_zip" {
                let files = vec![("b.txt".to_string(), "B".to_string()),
                                 ("SKILL.md".to_string(), "---\nname: demo-skill\ndescription: Une compétence\n---\n\nFais ceci.\n".to_string()),
                                 ("a/c.md".to_string(), "C".to_string())];
                let built = build_zip("demo-skill", &files);
                let mut z = zip::ZipArchive::new(std::io::Cursor::new(built)).unwrap();
                let mut entries = Vec::new();
                for i in 0..z.len() {
                    use std::io::Read;
                    let mut f = z.by_index(i).unwrap();
                    let mut s = String::new();
                    f.read_to_string(&mut s).unwrap();
                    entries.push(json!([f.name(), s]));
                }
                assert_eq!(json!(entries), c["entries"], "build_zip");
                continue;
            }
            let data = base64::engine::general_purpose::STANDARD.decode(c["zip"].as_str().unwrap()).unwrap();
            let got = match skills_from_zip(&data, c["sub"].as_str().unwrap()) {
                Ok((skills, skipped)) => json!({
                    "skills": skills.iter().map(|s| json!({"name": s.name, "description": s.description, "instructions": s.instructions,
                        "version": s.version, "files": s.files.iter().map(|(p, t)| json!([p, t])).collect::<Vec<_>>()})).collect::<Vec<_>>(),
                    "skipped": skipped.iter().map(|(a, b)| json!([a, b])).collect::<Vec<_>>(),
                }),
                Err(ImportError(e)) => json!({"err": e}),
            };
            assert_eq!(got, c["res"], "case {}", c["label"]);
        }
    }
}
