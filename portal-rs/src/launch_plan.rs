//! What a model will cost, before launching it.
//!
//! The operator's question is concrete: « will this fit, and how much will it
//! add? » On the GB10 the GPU allocation IS system RAM, so one budget decides
//! everything — the numbers here are read from the same `/proc/meminfo` and the
//! same weight folders the runner will use, never invented.
use serde_json::{json, Value};
use std::path::Path;

/// `MemTotal` and `MemAvailable` in GiB, as the kernel reports them.
pub fn ram_gib() -> (f64, f64) {
    let text = std::fs::read_to_string("/proc/meminfo").unwrap_or_default();
    let mut total = 0.0;
    let mut avail = 0.0;
    for line in text.lines() {
        let Some((k, rest)) = line.split_once(':') else { continue };
        let Some(kb) = rest.split_whitespace().next().and_then(|v| v.parse::<f64>().ok()) else { continue };
        match k {
            "MemTotal" => total = kb / 1048576.0,
            "MemAvailable" => avail = kb / 1048576.0,
            _ => {}
        }
    }
    (round1(total), round1(avail))
}

fn round1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}

/// Bytes under `path`, recursively (a model folder holds shards and a projector).
pub fn dir_bytes(path: &Path) -> u64 {
    let Ok(md) = std::fs::metadata(path) else { return 0 };
    if md.is_file() {
        return md.len();
    }
    let mut total = 0;
    if let Ok(entries) = std::fs::read_dir(path) {
        for e in entries.flatten() {
            total += dir_bytes(&e.path());
        }
    }
    total
}

/// The weights of a catalog entry, in GiB.
///
/// `local:<slug>` resolves under the mounted `/models`; anything else is an HF
/// cache repository, whose snapshots live under `HF_HOME` when it is mounted.
pub fn weights_gib(models_dir: &Path, hf_id: &str) -> Option<f64> {
    let slug = hf_id.strip_prefix("local:");
    if let Some(slug) = slug {
        let dir = models_dir.join(slug);
        if !dir.is_dir() {
            return None;
        }
        let b = dir_bytes(&dir);
        return (b > 0).then(|| round1(b as f64 / 1073741824.0));
    }
    let cache = std::env::var("HF_HOME").unwrap_or_else(|_| "/root/.cache/huggingface".into());
    let repo = format!("{}/hub/models--{}", cache, hf_id.replace('/', "--"));
    if !Path::new(&repo).is_dir() {
        return None;
    }
    let b = dir_bytes(Path::new(&repo));
    (b > 0).then(|| round1(b as f64 / 1073741824.0))
}

/// Per-token KV bytes of a GGUF, read from its header.
pub fn kv_bytes_per_token(model_dir: &Path) -> Option<(u64, crate::hf::GgufMeta)> {
    let mut candidates: Vec<(u64, std::path::PathBuf)> = Vec::new();
    for e in std::fs::read_dir(model_dir).ok()?.flatten() {
        let p = e.path();
        let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("").to_ascii_lowercase();
        if !name.ends_with(".gguf") || name.contains("mmproj") {
            continue;
        }
        let size = std::fs::metadata(&p).map(|m| m.len()).unwrap_or(0);
        candidates.push((size, p));
    }
    // NAME order, not size: a split GGUF carries its real metadata in the FIRST
    // shard (`…-00001-of-00002.gguf`); the others only hold `split.no`/
    // `split.count`, so a size-based pick reads an empty header (measured
    // 2026-11-17 on the served model).
    candidates.sort();
    use std::io::Read;
    for (_, path) in candidates.iter().take(4) {
        let Ok(f) = std::fs::File::open(path) else { continue };
        let mut buf = vec![0u8; 4 * 1024 * 1024];
        let mut r = std::io::BufReader::new(f);
        let Ok(n) = r.read(&mut buf) else { continue };
        buf.truncate(n);
        let meta = crate::hf::gguf_meta(&buf);
        if meta.context.is_some() && meta.layers.is_some() {
            if let Some(per_token) = meta.kv_bytes(1) {
                return Some((per_token, meta));
            }
        }
    }
    None
}

/// GiB from bytes.
fn gib(b: u64) -> f64 {
    round1(b as f64 / 1073741824.0)
}

/// The whole projection for one catalog entry.
pub fn plan(name: &str, engine: &str, hf_id: &str, args: &str, models_dir: &Path, freed: f64) -> Value {
    let (total, avail) = ram_gib();
    let used = round1(total - avail);
    let weights = weights_gib(models_dir, hf_id);
    // The context the launch will reserve: `--kv-unified-per-slot` wins (that is
    // the per-session window), then `--ctx-size` (the total when partitioned).
    let per_slot = crate::health::arg_int(args, "kv-unified-per-slot").filter(|n| *n > 0);
    let ctx_total = crate::health::arg_int(args, "ctx-size")
        .or_else(|| crate::health::arg_int(args, "context"))
        .or_else(|| crate::health::arg_int(args, "max-model-len"));
    let parallel = crate::health::arg_int(args, "parallel").filter(|n| *n > 0);
    let unified = args.split_whitespace().any(|t| t == "--kv-unified");
    let slots = per_slot
        .map(|p| ctx_total.map(|c| (c / p) as u64).unwrap_or(1))
        .or_else(|| parallel.map(|p| p as u64))
        .unwrap_or(1);

    let mut kv = None;
    let mut meta = None;
    if engine == "llamacpp" || engine == "edge0" {
        let slug = hf_id.strip_prefix("local:").unwrap_or("");
        if let Some((per_token, m)) = kv_bytes_per_token(&models_dir.join(slug)) {
            // Partitioned: `--ctx-size` is the TOTAL. Unified: the pool is
            // `per-slot x slots`, which is what `--ctx-size` already means.
            let cells = ctx_total.map(|c| c as u64).unwrap_or(4096);
            let cells = if unified { per_slot.map(|p| p as u64).unwrap_or(cells) } else { cells };
            kv = Some(gib(per_token * cells));
            meta = Some(m);
        }
    }
    // llama.cpp moves its weights into the GPU allocation plus a small host
    // overhead; the +2 GiB covers the process, the tokeniser and the graphs.
    let weights_total = weights.unwrap_or(0.0);
    let kv_gib = kv.unwrap_or(0.0);
    // The engine decides what is RESIDENT. Edge0 streams its expert pool from
    // disk and fills whatever the reservation leaves; counting its 106 GB of
    // files as RAM announced a launch that cannot fit while the same model was
    // measured running with 24 GiB free (2026-11-16).
    let added = if engine == "edge0" {
        let reserve = crate::health::arg_int(args, "vram-reserve-mib").filter(|m| *m > 0).unwrap_or(20480) as f64 / 1024.0;
        round1((total - reserve).max(0.0))
    } else {
        round1(weights_total + kv_gib + 2.0)
    };
    // Launching STOPS what is served now, so its own footprint is freed first —
    // otherwise the bar showed a launch that could never fit (measured
    // 2026-11-17: -64 GiB free for the model that was already running).
    let projected_used = round1((used - freed).max(0.0) + added);
    let free_after = round1(total - projected_used);
    json!({
        "ok": true,
        "model": name,
        "engine": engine,
        "weights_gb": weights_total,
        "kv_gb": kv_gib,
        "overhead_gb": 2.0,
        "added_gb": added,
        "freed_gb": round1(freed),
        "ram_used_gb": used,
        "ram_total_gb": total,
        "ram_free_gb": round1(avail),
        "projected_used_gb": projected_used,
        "projected_free_gb": free_after,
        "fits": free_after >= 0.0,
        "tight": free_after >= 0.0 && free_after < 10.0,
        "context": ctx_total,
        "slots": slots,
        "layers": meta.as_ref().and_then(|m| m.layers),
        "kv_heads": meta.as_ref().and_then(|m| m.kv_heads),
        "heuristic": weights.is_none() || kv.is_none(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

#[test]
fn probe_real_gguf() {
    let dir = std::path::Path::new("/root/models/qwen38-flash-next-abliterated-uncensored");
    if !dir.is_dir() { eprintln!("absent"); return; }
    match super::kv_bytes_per_token(dir) {
        Some((b, m)) => eprintln!("per_token={b} meta={m:?}"),
        None => {
            // what does the raw header say?
            let f = std::fs::read_dir(dir).unwrap().flatten().map(|e| e.path())
                .find(|p| p.to_string_lossy().ends_with(".gguf") && !p.to_string_lossy().contains("mmproj")).unwrap();
            let mut buf = vec![0u8; 524_288];
            use std::io::Read;
            let n = std::fs::File::open(&f).unwrap().read(&mut buf).unwrap();
            buf.truncate(n);
            eprintln!("parse → {:?}", crate::hf::gguf_meta(&buf));
            eprintln!("magic={:?} n_kv={:?}", &buf[0..4], crate::hf::rd_u64(&buf, 16));
            let mut pos = 24usize;
            for i in 0..4 {
                let k = crate::hf::rd_str(&buf, &mut pos);
                let t = crate::hf::rd_u32(&buf, pos);
                eprintln!("  key{i}={:?} type={:?} pos={pos}", k, t);
                pos += 4;
                match t {
                    Some(8) => { let v = crate::hf::rd_str(&buf, &mut pos); eprintln!("     str={:?}", v); }
                    Some(4) => { eprintln!("     u32={:?}", crate::hf::rd_u32(&buf, pos)); pos += 4; }
                    other => { eprintln!("     autre={:?}", other); break; }
                }
            }
        }
    }
}


    #[test]
    fn ram_is_read_from_the_kernel() {
        let (total, avail) = ram_gib();
        assert!(total > 8.0, "MemTotal looks wrong: {total}");
        assert!(avail <= total, "MemAvailable above MemTotal: {avail} > {total}");
    }

    /// Real weights, when the checkout has them (host only): the shapes must be
    /// read from the FIRST shard of a split GGUF.
    #[test]
    fn a_split_gguf_gives_its_shapes() {
        let dir = std::path::Path::new("/root/models/qwen38-flash-next-abliterated-uncensored");
        if !dir.is_dir() {
            return; // not on the host that has the weights
        }
        let (per_token, meta) = kv_bytes_per_token(dir).expect("shapes");
        assert!(meta.layers.is_some() && meta.kv_heads.is_some());
        assert!(per_token > 1024, "implausible per-token KV: {per_token}");
    }

    #[test]
    fn the_plan_projects_the_addition() {
        let p = plan("m", "llamacpp", "local:nowhere", "--ctx-size 8192", Path::new("/models"), 0.0);
        // No weights, no header: only the overhead is certain, and the flag says
        // the figures are a heuristic rather than a measurement.
        assert_eq!(p["heuristic"], true);
        assert!(p["added_gb"].as_f64().unwrap() >= 2.0);
        let (total, _) = ram_gib();
        assert_eq!(p["ram_total_gb"].as_f64().unwrap(), total);
    }
}
