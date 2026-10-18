//! The command construction of runner.py (`_build_cmd`) and its resolvers: the
//! local weights, the chat template, the projector, the exllamav3 layout. Every
//! refusal and every path matches runner.py's, down to the messages.
use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};

/// The configurable locations, from the environment as runner.py reads them.
#[derive(Clone, Debug)]
pub struct Paths {
    pub vllm_bin: String,
    pub llama_bin: String,
    pub ds4_bin: String,
    pub hf_home: String,
    pub models_dir: String,
    pub templates_dir: String,
    pub tabby_dir: String,
    pub exl3_py: String,
    /// `$HOME/exl3`: the symlink directories republished at every launch.
    pub exl3_run_dir: String,
}

impl Paths {
    pub fn from_env() -> Paths {
        let env = |k: &str, d: &str| std::env::var(k).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| d.to_string());
        // Defaults hang off the runner's own HOME. The box-specific locations are
        // set in /etc/vllm-runner/paths.env (written by install.sh), not here.
        let home = env("HOME", "/var/lib/vllm-runner");
        let tabby_dir = env("TABBY_DIR", &format!("{home}/tabbyAPI"));
        Paths {
            vllm_bin: env("VLLM_BIN", &format!("{home}/.local/bin/vllm")),
            llama_bin: env("LLAMA_BIN", &format!("{home}/llama.cpp/build/bin/llama-server")),
            ds4_bin: env("DS4_BIN", &format!("{home}/ds4-nvfp4-spark/ds4-server")),
            hf_home: env("HF_HOME", &format!("{home}/.cache/huggingface")),
            models_dir: env("MODELS_DIR", &format!("{home}/models")),
            templates_dir: env("TEMPLATES_DIR", &format!("{home}/models/templates")),
            exl3_py: env("EXL3_PY", &format!("{home}/venvs/exl3/bin/python")),
            exl3_run_dir: format!("{home}/exl3"),
            tabby_dir,
        }
    }
}

/// The two refusals of runner.py: ValueError and FileNotFoundError.
#[derive(Debug, PartialEq)]
pub enum BuildError {
    Value(String),
    NotFound(String),
}

impl BuildError {
    pub fn kind(&self) -> &'static str {
        match self {
            BuildError::Value(_) => "ValueError",
            BuildError::NotFound(_) => "FileNotFoundError",
        }
    }
    pub fn message(&self) -> &str {
        match self {
            BuildError::Value(m) | BuildError::NotFound(m) => m,
        }
    }
}

type R<T> = Result<T, BuildError>;

/// `re.sub(r'[^A-Za-z0-9._-]', '', s)`.
fn safe_name(s: &str) -> String {
    s.chars().filter(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')).collect()
}

/// `os.path.realpath(p)` for an existing path; None when it does not exist.
fn realpath(p: &str) -> Option<PathBuf> {
    fs::canonicalize(p).ok()
}

/// `_resolve_gguf(hf_id)`: the weights file of a model, under the controlled dirs.
pub fn resolve_gguf(p: &Paths, hf_id: &str) -> R<String> {
    let snaps: String;
    if let Some(rest) = hf_id.strip_prefix("local:") {
        let slug = safe_name(rest);
        snaps = format!("{}/{}", p.models_dir, slug);
        let base = realpath(&p.models_dir);
        let inside = match (realpath(&snaps), &base) {
            (Some(real), Some(base)) => real.starts_with(base.join("")) && real != *base,
            _ => false,
        };
        if slug.is_empty() || slug == "." || slug == ".." || !inside || !Path::new(&snaps).is_dir() {
            return Err(BuildError::NotFound(format!("local model \"{slug}\" not found in {}", p.models_dir)));
        }
    } else {
        snaps = format!("{}/hub/models--{}/snapshots", p.hf_home, hf_id.replace('/', "--"));
        if !Path::new(&snaps).is_dir() {
            return Err(BuildError::NotFound(format!("model {hf_id} not in HF cache — download it first")));
        }
    }
    let mut candidates: Vec<(u64, String, String)> = Vec::new();
    walk(Path::new(&snaps), &snaps, &mut candidates);
    if candidates.is_empty() {
        return Err(BuildError::NotFound(format!("no .gguf found for {hf_id}")));
    }
    let mut shards: Vec<&String> = candidates.iter().filter(|(_, f, _)| f.contains("-00001-of-")).map(|(_, _, p)| p).collect();
    shards.sort();
    if let Some(first) = shards.first() {
        return Ok((*first).clone());
    }
    Ok(candidates.iter().max().map(|c| c.2.clone()).unwrap_or_default())
}

/// `os.walk` as runner.py uses it: real directories are entered, a symlink to a
/// directory is not, a file (a symlink to one included) is a candidate when it
/// is a .gguf that is neither a projector nor an imatrix.
fn walk(dir: &Path, prefix: &str, out: &mut Vec<(u64, String, String)>) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    let mut entries: Vec<_> = rd.flatten().collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let name = e.file_name().to_string_lossy().into_owned();
        let path = format!("{prefix}/{name}");
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_dir() {
            walk(&e.path(), &path, out);
        } else if ft.is_symlink() && Path::new(&path).is_dir() {
            // os.walk does not enter a symlinked directory
        } else if name.ends_with(".gguf") && !name.contains("mmproj") && !name.contains("imatrix") {
            if let Ok(m) = fs::metadata(&path) {
                out.push((m.len(), name, path));
            }
        }
    }
}

/// `_resolve_template_tokens`: `--chat-template-file` names a file of TEMPLATES_DIR.
pub fn resolve_template_tokens(p: &Paths, tokens: &[String]) -> R<Vec<String>> {
    let mut out = tokens.to_vec();
    let mut i = 0;
    while i < out.len() {
        if out[i] == "--chat-template-file" && i + 1 < out.len() {
            let raw = out[i + 1].clone();
            if raw.contains('/') || raw.contains('\\') || raw.contains("..") {
                return Err(BuildError::Value("invalid template name".into()));
            }
            let path = format!("{}/{}", p.templates_dir, raw);
            if !Path::new(&path).is_file() {
                return Err(BuildError::NotFound(format!("template \"{raw}\" not found in {}", p.templates_dir)));
            }
            out[i + 1] = path;
        }
        i += 1;
    }
    Ok(out)
}

/// `_resolve_mmproj_tokens`: a projector, next to the weights of a `local:` model only.
pub fn resolve_mmproj_tokens(p: &Paths, tokens: &[String], hf_id: &str) -> R<Vec<String>> {
    let mut out = tokens.to_vec();
    let mut i = 0;
    while i < out.len() {
        if out[i] == "--mmproj" && i + 1 < out.len() {
            let raw = out[i + 1].clone();
            if raw.contains('/') || raw.contains('\\') || raw.contains("..") {
                return Err(BuildError::Value("invalid mmproj name".into()));
            }
            if !hf_id.starts_with("local:") {
                return Err(BuildError::Value("--mmproj requires a local: model".into()));
            }
            let weights = resolve_gguf(p, hf_id)?;
            let dir = weights.rsplit_once('/').map(|(d, _)| d.to_string()).unwrap_or_default();
            let path = format!("{dir}/{raw}");
            if !Path::new(&path).is_file() {
                return Err(BuildError::NotFound(format!("mmproj \"{raw}\" not found next to the weights")));
            }
            out[i + 1] = path;
        }
        i += 1;
    }
    Ok(out)
}

/// `_resolve_exl3_tokens`: the model directory and its names (no separators, existing).
pub fn resolve_exl3_tokens(p: &Paths, tokens: &[String], hf_id: &str) -> R<(String, Vec<String>)> {
    let Some(rest) = hf_id.strip_prefix("local:") else {
        return Err(BuildError::Value("exllamav3 requires a local: model".into()));
    };
    let slug = safe_name(rest);
    let base = format!("{}/{}", p.models_dir, slug);
    let inside = match (realpath(&base), realpath(&p.models_dir)) {
        (Some(real), Some(root)) => real.starts_with(root.join("")) && real != root,
        _ => false,
    };
    if slug.is_empty() || slug == "." || slug == ".." || !inside || !Path::new(&base).is_dir() {
        return Err(BuildError::NotFound(format!("local model \"{slug}\" not found in {}", p.models_dir)));
    }
    let out = tokens.to_vec();
    let mut i = 0;
    while i < out.len() {
        if (out[i] == "--model-name" || out[i] == "--draft-model-name") && i + 1 < out.len() {
            let raw = &out[i + 1];
            if raw.is_empty() || raw.contains('/') || raw.contains('\\') || raw.contains("..") {
                return Err(BuildError::Value(format!("invalid name for {}", out[i])));
            }
            if !Path::new(&format!("{base}/{raw}")).is_dir() {
                return Err(BuildError::NotFound(format!("{} \"{raw}\" not found in {base}", out[i])));
            }
        }
        i += 1;
    }
    if !out.iter().any(|t| t == "--model-name") {
        return Err(BuildError::Value("exllamav3: --model-name is required".into()));
    }
    Ok((base, out))
}

/// `_exl3_served_layout`: publish the model under its catalog name, through symlinks.
pub fn exl3_served_layout(p: &Paths, base: &str, name: &str, toks: &[String]) -> R<(String, String, Vec<String>)> {
    let alias = safe_name(name);
    if alias.is_empty() || alias == "." || alias == ".." {
        return Err(BuildError::Value("invalid served model name".into()));
    }
    let mut out = toks.to_vec();
    let mdir = format!("{}/model", p.exl3_run_dir);
    let ddir = format!("{}/draft", p.exl3_run_dir);
    for d in [&mdir, &ddir] {
        let _ = fs::remove_dir_all(d);
        fs::create_dir_all(d).map_err(|e| BuildError::Value(e.to_string()))?;
    }
    let i = out.iter().position(|t| t == "--model-name").ok_or_else(|| BuildError::Value("list.index(x): x not in list".into()))?;
    let target = format!("{base}/{}", out.get(i + 1).ok_or_else(|| BuildError::Value("list index out of range".into()))?);
    symlink(&target, format!("{mdir}/{alias}")).map_err(|e| BuildError::Value(e.to_string()))?;
    out[i + 1] = alias;
    if let Some(j) = out.iter().position(|t| t == "--draft-model-name") {
        let draft = out.get(j + 1).ok_or_else(|| BuildError::Value("list index out of range".into()))?.clone();
        symlink(format!("{base}/{draft}"), format!("{ddir}/{draft}")).map_err(|e| BuildError::Value(e.to_string()))?;
    }
    Ok((mdir, ddir, out))
}

/// `_build_cmd(hf_id, name, extra_tokens, engine, bin_override)`: the engine's argv.
pub fn build_cmd(p: &Paths, hf_id: &str, name: &str, extra: &[String], engine: &str, bin_override: Option<&str>) -> R<Vec<String>> {
    let has = |f: &str| extra.iter().any(|t| t == f);
    let strs = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<String>>();
    match engine {
        "exllamav3" => {
            let (base, toks) = resolve_exl3_tokens(p, extra, hf_id)?;
            let (mdir, ddir, toks) = exl3_served_layout(p, &base, name, &toks)?;
            let mut cmd = vec![p.exl3_py.clone(), format!("{}/main.py", p.tabby_dir)];
            cmd.extend(strs(&["--host", "0.0.0.0", "--port", "8000", "--disable-auth", "true", "--disable-fetch-requests", "true", "--allowed-origins", "https://no-browser.invalid", "--model-dir"]));
            cmd.push(mdir);
            if toks.iter().any(|t| t == "--draft-model-name") {
                cmd.push("--draft-model-dir".into());
                cmd.push(ddir);
            }
            cmd.extend(toks);
            Ok(cmd)
        }
        "ds4" => {
            let mut cmd = vec![p.ds4_bin.clone(), "-m".into(), resolve_gguf(p, hf_id)?];
            cmd.extend(strs(&["--host", "0.0.0.0", "--port", "8000"]));
            cmd.extend(extra.iter().cloned());
            if !has("--cpu") && !has("--cuda") {
                cmd.insert(1, "--cuda".into());
            }
            Ok(cmd)
        }
        "vllm-docker" => {
            // vLLM as a container (NVIDIA ships it that way for some models). Same
            // OpenAI API on :8000; the weights come from the host HF cache. Mirrors
            // runner.py's vllm-docker branch.
            let img = std::env::var("VLLM_DOCKER_IMAGE").ok().filter(|v| !v.is_empty())
                .unwrap_or_else(|| "vllm/vllm-openai:nightly-3f1bcfd82bb2e93fcd364fc7a5491718453a77e2".to_string());
            let cache = format!("{}/.cache/huggingface", std::env::var("HOME").unwrap_or_else(|_| "/var/lib/vllm-runner".into()));
            // Published where the portal can reach it: VLLM_DOCKER_PUBLISH is the host
            // address (the portal's bridge gateway on this machine), loopback by default.
            let bind = std::env::var("VLLM_DOCKER_PUBLISH").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "127.0.0.1".to_string());
            let publish = format!("{bind}:8000:8000");
            let mut cmd = strs(&["docker", "run", "--rm", "--gpus", "all", "--ipc=host", "-p", publish.as_str(),
                "-e", "HF_TOKEN", "-e", "FLASHINFER_DISABLE_VERSION_CHECK=1", "-v"]);
            cmd.push(format!("{cache}:/root/.cache/huggingface"));
            cmd.extend(strs(&["--entrypoint", "vllm"]));
            cmd.push(img);
            cmd.extend(strs(&["serve"]));
            cmd.push(hf_id.to_string());
            cmd.extend(strs(&["--host", "0.0.0.0", "--port", "8000", "--served-model-name"]));
            cmd.push(name.to_string());
            cmd.extend(extra.iter().cloned());
            Ok(cmd)
        }
        "llamacpp" => {
            let src = if hf_id.starts_with("local:") { vec!["-m".into(), resolve_gguf(p, hf_id)?] } else { vec!["-hf".into(), hf_id.to_string()] };
            let toks = resolve_template_tokens(p, extra)?;
            let toks = resolve_mmproj_tokens(p, &toks, hf_id)?;
            let mut cmd = vec![bin_override.map(str::to_string).unwrap_or_else(|| p.llama_bin.clone())];
            cmd.extend(src);
            cmd.extend(strs(&["--host", "0.0.0.0", "--port", "8000", "--alias"]));
            cmd.push(name.to_string());
            cmd.push("--metrics".into());
            cmd.extend(toks);
            Ok(cmd)
        }
        _ => {
            let mut cmd = vec![bin_override.map(str::to_string).unwrap_or_else(|| p.vllm_bin.clone()), "serve".into(), hf_id.to_string()];
            cmd.extend(strs(&["--port", "8000", "--host", "0.0.0.0", "--served-model-name"]));
            cmd.push(name.to_string());
            cmd.extend(extra.iter().cloned());
            Ok(cmd)
        }
    }
}
