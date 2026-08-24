"""
Model Runner — local HTTP daemon on port 8001.
Manages a single inference process at a time (with its children), one of:
  - vLLM      : safetensors weights (NVFP4 / FP8 / BF16)
  - llama.cpp : GGUF weights (llama-server, OpenAI-compatible API on the same port)
In both cases the model is served on :8000 → LiteLLM routing is identical.
"""
import hmac, json, os, re, shutil, signal, subprocess, threading, time, urllib.request
from flask import Flask, jsonify, request, Response

VLLM_BIN     = os.environ.get("VLLM_BIN", "/root/.local/bin/vllm")
# Separate venv (vLLM 0.25.1 + FlashInfer nightly) for models that require a
# newer vLLM than the globally installed one — avoids a major version bump that
# would break existing models (nemotron/minimax/ornith run on VLLM_BIN, tested
# and stable there). Enabled by the pseudo-flag --vllm-025 in vllm_args (see _BIN_FLAGS).
VLLM_BIN_025 = os.environ.get("VLLM_BIN_025", "/root/venvs/vllm025/bin/vllm")
# vLLM 0.27.1 venv (torch 2.13 cu130, aarch64/sm_121). Benchmarked equal to
# 0.25.1 on Qwen3.8-27B-FP8+MTP (~12 vs ~11.8 tok/s — memory-bandwidth bound),
# kept as an opt-in --vllm-027 flag for models we choose to run on the latest.
VLLM_BIN_027 = os.environ.get("VLLM_BIN_027", "/root/venvs/vllm-next/bin/vllm")
LLAMA_BIN    = os.environ.get("LLAMA_BIN", "/root/llama.cpp/build/bin/llama-server")
# ds4 engine: DGX Spark-specific "multi-tensor" NVFP4 GGUF (DeepSeek-V4-Flash).
# Neither vLLM nor stock llama.cpp can load this format.
DS4_BIN      = os.environ.get("DS4_BIN", "/root/ds4-nvfp4-spark/ds4-server")
HF_HOME      = os.environ.get("HF_HOME", "/root/.cache/huggingface")
# Directory of weights downloaded outside the Hub (e.g. HF throttles large GGUFs
# when unauthenticated). A model is referenced there by "local:<name>" — the name
# is sanitized, so no arbitrary path or directory traversal.
MODELS_DIR   = os.environ.get("MODELS_DIR", "/root/models")
# Fixed-up chat templates (e.g. neutralize the strict alternation of Mistral
# models that breaks in agentic use). Referenced by name only → no arbitrary path.
TEMPLATES_DIR = os.environ.get("TEMPLATES_DIR", "/root/models/templates")
RUNNER_TOKEN = os.environ["RUNNER_TOKEN"]  # required — no default, the service must fail at startup if absent

ENGINES = ("vllm", "llamacpp", "ds4")
_ENGINE_BIN = {"vllm": VLLM_BIN, "llamacpp": LLAMA_BIN, "ds4": DS4_BIN}

# Persist the last successful launch so it can be resumed automatically after a
# service restart (system update, reboot, crash) — except on a deliberate /stop,
# which clears this file.
STATE_FILE = os.path.join(os.environ.get("HOME", "/var/lib/vllm-runner"), "last_model.json")
MAX_AUTO_RETRIES = 3

app = Flask(__name__)

_lock   = threading.Lock()
_proc   = None
_model  = None
_engine = None        # engine of the current model: 'vllm' | 'llamacpp'
_logs   = []
_status = "stopped"   # stopped | starting | running | error
_auto_retries = 0     # consecutive failed automatic relaunch attempts

# ── Auth ─────────────────────────────────────────────────────────────────
# Every route requires "Authorization: Bearer <RUNNER_TOKEN>".
# This API drives a root process and launches arbitrary models: it must never
# be callable without proof that the caller really is dgx-portal.
@app.before_request
def _check_auth():
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    token  = header[len(prefix):] if header.startswith(prefix) else ""
    if not hmac.compare_digest(token, RUNNER_TOKEN):
        return jsonify({"error": "unauthorized"}), 401


# ── Whitelist of vLLM flags allowed in vllm_args ───────────────────────────
# Strict allowlist (not a denylist): any unlisted flag is refused.
# Deliberately absent: --trust-remote-code (RCE via HF repo code),
# --download-dir / --chat-template / --tokenizer (arbitrary file read /
# Jinja2 SSTI), --model / --host / --port / --served-model-name / --api-key
# (already set by the runner, must not be overridable).
_BOOL_FLAGS = {
    "--enable-auto-tool-choice", "--enforce-eager",
    "--disable-log-requests", "--disable-log-stats",
    "--skip-mm-profiling",
    # KAT-Coder-V2.5 (and other text-only Qwen3.5-MoE releases): without this
    # flag vLLM resolves a multimodal config (Qwen3_5MoeConfig) instead of the
    # text config (Qwen3_5MoeTextConfig) that this weight actually expects →
    # TypeError at load. Required per the model card.
    "--language-model-only",
}

# Pseudo-flags: NOT passed to the engine, they set an environment variable for
# THIS model only. Closed allowlist → no arbitrary env injection. Useful when a
# model requires a particular kernel path that we specifically don't want to
# impose globally on the other models.
_ENV_FLAGS = {
    # MiniMax-M2 (mixed NVFP4+FP8 quant): vLLM finds no FP8 ScaledMM kernel on
    # GB10 and explicitly requests this Marlin fallback.
    "--force-fp8-marlin": ("VLLM_TEST_FORCE_FP8_MARLIN", "1"),
    # Laguna S 2.1 (native NVFP4 via FlashInfer): architecture string required
    # by the FP4 kernels' JIT on GB10 (official poolside recipe).
    "--cute-dsl-arch-sm121a": ("CUTE_DSL_ARCH", "sm_121a"),
}
_BOOL_FLAGS |= set(_ENV_FLAGS)

# Separate pseudo-flag (not just an env var): switches the vLLM binary used for
# THIS launch only, without touching VLLM_BIN (so no risk for the other vllm
# models). Removed from extra_tokens in _start_process, like _ENV_FLAGS entries.
_BIN_FLAGS = {
    "--vllm-025": VLLM_BIN_025,
    "--vllm-027": VLLM_BIN_027,
}
_BOOL_FLAGS |= set(_BIN_FLAGS)
_VALUE_FLAGS = {
    "--tool-call-parser", "--dtype", "--max-model-len",
    "--gpu-memory-utilization", "--max-num-seqs", "--kv-cache-dtype",
    "--max-num-batched-tokens", "--block-size", "--swap-space",
    "--quantization", "--tensor-parallel-size", "--pipeline-parallel-size",
    "--reasoning-parser", "--limit-mm-per-prompt",
    "--uvicorn-log-level",
    # Speculative decoding (MTP / draft model): a compact JSON value, e.g.
    # {"method":"mtp","num_speculative_tokens":1}. Passed as argv to vLLM (never
    # shell-interpreted), so the JSON is inert. Lets bundled-MTP models (Qwen3.5)
    # predict several tokens per weight-read → ~1.5-2x with no quality loss.
    "--speculative-config",
    # Enumerated value (auto|slow|mistral|custom), never a path → safe.
    # Needed for Mistral models (tekken): vLLM 0.24 auto-detection falls onto a
    # broken backend ("CachedMistralCommonBackend has no attribute is_fast"),
    # whereas --tokenizer-mode mistral works.
    "--tokenizer-mode",
}

# ── OCR whitelist (dedicated docker container, NOT the main host process) ──
# More permissive than _BOOL_FLAGS/_VALUE_FLAGS: --trust-remote-code and
# --logits_processors are needed by Unlimited-OCR (custom logits processor from
# the repo) and probably by other OCR VLMs. Real RCE risk if the admin points
# at a malicious HF repo — accepted here: (1) admin-only, same trust level as
# the main chat catalog, which already fully controls what runs on the host;
# (2) this container is isolated (dedicated docker network, no docker.sock, no
# access to the other services).
_OCR_BOOL_FLAGS = _BOOL_FLAGS | {"--trust-remote-code", "--no-enable-prefix-caching"}
_OCR_VALUE_FLAGS = _VALUE_FLAGS | {"--logits_processors", "--mm-processor-cache-gb"}

# ── Whitelist of llama.cpp flags (llama-server) ────────────────────────────
# Same principle: strict allowlist. Deliberately absent:
# --model / --hf-repo / --host / --port / --alias (set by the runner),
# --chat-template-file & --grammar-file & --lora (arbitrary file read),
# --chat-template (accepts a full Jinja template → injection surface).
_LLAMA_BOOL_FLAGS = {
    "--no-mmap", "--mlock", "--jinja", "--cont-batching",
    "--no-kv-offload", "--metrics", "--no-warmup",
    # Truncates old tokens when a slot is full instead of ERRORing (otherwise a
    # client like OpenCode retries the same over-long request → crash).
    "--context-shift", "--no-context-shift",
}
_LLAMA_VALUE_FLAGS = {
    "--ctx-size", "--n-gpu-layers", "--parallel", "--threads", "--threads-batch",
    "--batch-size", "--ubatch-size", "--cache-type-k", "--cache-type-v",
    "--n-predict", "--rope-scaling", "--rope-freq-base", "--rope-freq-scale",
    "--split-mode", "--main-gpu", "--seed", "--defrag-thold", "--log-verbosity",
    "--reasoning-format", "--chat-template-kwargs",
    # Careful: --flash-attn takes a VALUE (on|off|auto) in recent llama.cpp —
    # treating it as a boolean makes it swallow the next flag.
    "--flash-attn",
    # Value = file name only, resolved under TEMPLATES_DIR (no arbitrary path,
    # see _resolve_template) → used to fix up an embedded template.
    "--chat-template-file",
}


# ── Whitelist of ds4 flags (ds4-server) ────────────────────────────────────
# -m / --host / --port are set by the runner. No flag taking a path
# (--kv-disk-dir, --dir-steering-file) → no arbitrary read/write.
_DS4_BOOL_FLAGS = {
    "--cuda", "--cpu", "--kv-cache-reject-different-quant",
    "--disable-exact-dsml-tool-replay",
}
_DS4_VALUE_FLAGS = {
    "--ctx", "--backend", "--kv-cache-min-tokens", "--kv-cache-cold-max-tokens",
    "--kv-cache-boundary-align-tokens", "--kv-cache-boundary-trim-tokens",
    "--kv-cache-continued-interval-tokens", "--kv-disk-space-mb",
}


def _flags_for(engine):
    if engine == "llamacpp":
        return _LLAMA_BOOL_FLAGS, _LLAMA_VALUE_FLAGS
    if engine == "ds4":
        return _DS4_BOOL_FLAGS, _DS4_VALUE_FLAGS
    if engine == "ocr":
        return _OCR_BOOL_FLAGS, _OCR_VALUE_FLAGS
    return _BOOL_FLAGS, _VALUE_FLAGS


def _resolve_gguf(hf_id):
    """Resolve a local GGUF (the ds4/llama.cpp engines want a file via -m).

    Two sources, both UNDER A CONTROLLED DIRECTORY — never an arbitrary path
    coming from the API:
      - "local:<name>"  → MODELS_DIR/<name>/  (sanitized name)
      - "user/repo"     → HF cache of the repo
    Returns the 1st shard if it is split, otherwise the largest .gguf.
    """
    if hf_id.startswith("local:"):
        slug = re.sub(r'[^A-Za-z0-9._-]', '', hf_id[len("local:"):])
        snaps = os.path.join(MODELS_DIR, slug)
        # The char filter strips slashes but keeps dots, so guard against '.'/'..'
        # and confirm the resolved path stays strictly under MODELS_DIR — no
        # one-level escape (e.g. local:.. → /root).
        _base = os.path.realpath(MODELS_DIR)
        if slug in ("", ".", "..") or not os.path.realpath(snaps).startswith(_base + os.sep) or not os.path.isdir(snaps):
            raise FileNotFoundError(f"local model \"{slug}\" not found in {MODELS_DIR}")
    else:
        snaps = os.path.join(HF_HOME, "hub",
                             "models--" + hf_id.replace("/", "--"), "snapshots")
        if not os.path.isdir(snaps):
            raise FileNotFoundError(f"model {hf_id} not in HF cache — download it first")
    candidates = []
    for root, _dirs, files in os.walk(snaps):
        for f in files:
            if f.endswith(".gguf") and "mmproj" not in f and "imatrix" not in f:
                p = os.path.join(root, f)
                candidates.append((os.path.getsize(os.path.realpath(p)), f, p))
    if not candidates:
        raise FileNotFoundError(f"no .gguf found for {hf_id}")
    # Model split into shards → always point at the first (00001-of-000NN),
    # the engine loads the rest on its own.
    shards = sorted(p for _s, f, p in candidates if "-00001-of-" in f)
    if shards:
        return shards[0]
    return max(candidates)[2]


def _validate_vllm_args(extra, engine="vllm"):
    """Return (ok, tokens_or_error_message). The allowlist depends on the engine."""
    bool_flags, value_flags = _flags_for(engine)
    tokens = extra.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in bool_flags:
            i += 1
        elif tok in value_flags:
            if i + 1 >= len(tokens) or tokens[i + 1].startswith("--"):
                return False, f"flag {tok} requires a value"
            i += 2
        else:
            return False, f"flag not allowed: {tok}"
    return True, tokens


def _append(line):
    _logs.append(line)
    if len(_logs) > 2000:
        del _logs[:500]


def _save_last_launch(hf_id, name, extra_tokens, engine="vllm"):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"hf_model_id": hf_id, "model_name": name,
                       "vllm_args": " ".join(extra_tokens), "engine": engine}, f)
    except OSError as e:
        _append(f"[runner] could not save state for auto-resume: {e}")


def _load_last_launch():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _clear_last_launch():
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass
    except OSError as e:
        _append(f"[runner] could not clear auto-resume state: {e}")


def _kill(proc):
    """Kill the process AND all its children (process group)."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            proc.kill()
        proc.wait(timeout=5)


def _mem_available_gib():
    """Available memory (GiB). On GB10 memory is UNIFIED (GPU + CPU):
    /proc/meminfo is therefore the right indicator — nvidia-smi does not report
    memory on this integrated card."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return None


def _wait_mem_release(timeout=60, settle=4.0):
    """Wait until the previous model's memory is actually released.

    The driver does not reclaim unified memory instantly when the process dies:
    spawning the new vLLM too early makes it fail on a GPU OOM
    (NVRM: NV_ERR_NO_MEMORY) — the model crashes then goes into auto-retry. So we
    wait for MemAvailable to stop rising (plateau) before relaunching.
    """
    start = time.time()
    prev = _mem_available_gib()
    if prev is None:
        time.sleep(settle)
        return
    stable = 0
    while time.time() - start < timeout:
        time.sleep(1.5)
        cur = _mem_available_gib()
        if cur is None:
            break
        if cur - prev < 0.5:      # no more notable release
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0            # still releasing, keep waiting
        prev = cur
    free = _mem_available_gib()
    _append(f"[runner] Memory released: {free:.1f} GiB free "
            f"(waited {time.time() - start:.0f}s) — relaunching the model")
    time.sleep(settle)            # small margin for the driver


def _reader(proc):
    global _status, _proc, _model, _auto_retries
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            _append(line)
            # vLLM is ready when it prints "Application startup complete"
            # (only touch the global status if this process is still the active one —
            # otherwise an old reader thread, still draining a process killed by
            # /launch, could overwrite the status of the NEW process that is starting)
            if "Application startup complete" in line and proc is _proc:
                _status = "running"
                _auto_retries = 0  # this launch worked, restart with a fresh retry budget
    except Exception as e:
        _append(f"[runner] read interrupted: {e}")
    proc.wait()
    with _lock:
        if proc is _proc and _status != "stopped":
            _status = "error" if proc.returncode not in (0, -15, -9) else "stopped"
        _append(f"[runner] Process exited (code {proc.returncode})")


def _health_watch(proc):
    """Flip the status to 'running' as soon as vLLM actually responds, without
    relying on logs: --uvicorn-log-level warning hides "Application startup
    complete", which left the status stuck on 'starting' while the model served."""
    global _status, _auto_retries
    url = "http://127.0.0.1:8000/v1/models"
    while proc is _proc and proc.poll() is None:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200 and proc is _proc:
                    _status = "running"
                    _auto_retries = 0
                    return
        except Exception:
            pass
        time.sleep(3)


@app.route("/status")
def status():
    return jsonify({"status": _status, "model": _model, "engine": _engine,
                    "pid": _proc.pid if _proc else None,
                    "engines_available": {e: (e == "vllm" or os.path.exists(b))
                                          for e, b in _ENGINE_BIN.items()}})


@app.route("/logs")
def logs():
    n = min(int(request.args.get("n", 200)), 2000)
    return jsonify({"logs": _logs[-n:]})


@app.route("/stream")
def stream():
    """SSE — push new log lines in real time."""
    def generate():
        # Send all existing logs at once
        with _lock:
            snapshot = list(_logs)
        last = len(snapshot)
        for line in snapshot:
            yield f"data: {line}\n\n"

        while True:
            time.sleep(0.05)   # 50 ms → near real time
            with _lock:
                current_len = len(_logs)
                if current_len < last:
                    # _logs.clear() called by /launch → new startup
                    yield "event: clear\ndata: \n\n"
                    new_lines = list(_logs)
                    last = current_len
                    for line in new_lines:
                        yield f"data: {line}\n\n"
                elif current_len > last:
                    new_lines = _logs[last:]
                    last = current_len
                    for line in new_lines:
                        yield f"data: {line}\n\n"
                else:
                    yield ": ping\n\n"   # keep-alive (every 50 ms)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return Response(generate(), mimetype="text/event-stream", headers=headers)


def _resolve_template_tokens(tokens):
    """Replace the value of --chat-template-file (file name only) with the
    absolute path under TEMPLATES_DIR. Reject anything containing a separator or
    ".." → impossible to read a file outside the controlled directory."""
    out = list(tokens)
    for i, t in enumerate(out):
        if t == "--chat-template-file" and i + 1 < len(out):
            raw = out[i + 1]
            if "/" in raw or "\\" in raw or ".." in raw:
                raise ValueError("invalid template name")
            path = os.path.join(TEMPLATES_DIR, raw)
            if not os.path.isfile(path):
                raise FileNotFoundError(f"template \"{raw}\" not found in {TEMPLATES_DIR}")
            out[i + 1] = path
    return out


def _build_cmd(hf_id, name, extra_tokens, engine, vllm_bin=None):
    """Engine command line. They all serve an OpenAI API on :8000, so nothing
    downstream changes (LiteLLM, portal, playground)."""
    if engine == "ds4":
        # ds4-server takes a local GGUF; --cuda is required for the GPU.
        cmd = [DS4_BIN, "-m", _resolve_gguf(hf_id),
               "--host", "0.0.0.0", "--port", "8000"] + extra_tokens
        if "--cpu" not in extra_tokens and "--cuda" not in extra_tokens:
            cmd.insert(1, "--cuda")
        return cmd
    if engine == "llamacpp":
        # "local:<name>" → weights already on disk, point at the file (-m).
        # Otherwise -hf accepts "user/repo[:QUANT]" and llama.cpp downloads it itself.
        # --metrics exposes /metrics (Prometheus) like vLLM, for the health panel.
        src = ["-m", _resolve_gguf(hf_id)] if hf_id.startswith("local:") else ["-hf", hf_id]
        extra_tokens = _resolve_template_tokens(extra_tokens)
        return [LLAMA_BIN] + src + [
                "--host", "0.0.0.0", "--port", "8000",
                "--alias", name,
                "--metrics"] + extra_tokens
    return [vllm_bin or VLLM_BIN, "serve", hf_id,
            "--port", "8000", "--host", "0.0.0.0",
            "--served-model-name", name] + extra_tokens


def _start_process(hf_id, name, extra_tokens, engine="vllm"):
    """Launch the inference engine. Must be called with _lock already held."""
    global _proc, _model, _status, _engine

    killed = bool(_proc and _proc.poll() is None)
    if killed:
        _append("[runner] Stopping previous model…")
        _kill(_proc)

    _logs.clear()
    _model  = name
    _engine = engine
    _status = "starting"

    # The previous model was just killed: wait for the driver to release the
    # unified memory, otherwise the new process OOMs at startup.
    if killed:
        _wait_mem_release()

    # Kept for persistence (auto-resume): the pseudo-flags (e.g. --vllm-025) are
    # stripped from extra_tokens just below for execution, but an auto-resume
    # that lost them would relaunch on the wrong binary / without the necessary
    # workaround — so we save the full list.
    original_tokens = list(extra_tokens)

    # Pseudo-flags become env vars specific to this model and are removed from
    # argv (the engine doesn't know them).
    model_env = {}
    for flag, (var, val) in _ENV_FLAGS.items():
        if flag in extra_tokens:
            extra_tokens = [t for t in extra_tokens if t != flag]
            model_env[var] = val

    # --vllm-025 (see _BIN_FLAGS): switch to the separate vLLM 0.25.1 venv for
    # this launch, without touching the default binary of the other models.
    vllm_bin = None
    for flag, bin_path in _BIN_FLAGS.items():
        if flag in extra_tokens:
            extra_tokens = [t for t in extra_tokens if t != flag]
            vllm_bin = bin_path

    cmd = _build_cmd(hf_id, name, extra_tokens, engine, vllm_bin=vllm_bin)
    _append(f"[runner] ({engine}) $ {' '.join(cmd)}")
    if model_env:
        _append(f"[runner] model-specific env: {model_env}")

    # Explicit minimal env rather than **os.environ — avoids leaking the full
    # root environment (assorted secrets) into /logs and /stream.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "HF_HOME": HF_HOME,
        "PYTHONUNBUFFERED": "1",
        # DeepGEMM E8M0 breaks FP8 MoE on Blackwell/GB10 ("Unknown SF
        # transformation") and degrades accuracy (vLLM partially auto-disables
        # it) → we turn it off entirely, CUTLASS fallback.
        "VLLM_USE_DEEP_GEMM": "0",
        # FlashInfer JIT-compiles its NVFP4 kernels at startup. Without MAX_JOBS
        # it passes no -j to ninja, which spawns ~nproc+2 `cicc` compilers of
        # ~3 GB each — on top of the already-loaded weights, that triggers the
        # OOM killer and the model dies at init (seen on Leanstral and Nemotron).
        # 4 jobs ≈ 12 GB peak: compilation a bit slower, but only once (kernels
        # are then cached).
        "MAX_JOBS": os.environ.get("MAX_JOBS", "4"),
    }
    env.update(model_env)
    if os.environ.get("HF_TOKEN"):
        env["HF_TOKEN"] = os.environ["HF_TOKEN"]
    if engine == "ds4":
        # KV cache packed in FP8 → ~7 GiB saved at 1M context (see model card).
        env["DS4_KV_TURBO"] = "1"

    _proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,   # new process group → killpg works
    )
    threading.Thread(target=_reader, args=(_proc,), daemon=True).start()
    threading.Thread(target=_health_watch, args=(_proc,), daemon=True).start()
    # Always persist the state (manual, boot resume, watchdog) so last_model.json
    # stays present as long as the model is meant to run.
    _save_last_launch(hf_id, name, original_tokens, engine)
    return _proc


@app.route("/launch", methods=["POST"])
def launch():
    global _auto_retries
    data     = request.get_json(silent=True) or {}
    hf_id    = data.get("hf_model_id", "").strip()
    name     = data.get("model_name", hf_id).strip()
    extra    = data.get("vllm_args", "").strip()
    engine   = (data.get("engine") or "vllm").strip().lower()

    if not hf_id:
        return jsonify({"error": "hf_model_id required"}), 400
    if engine not in ENGINES:
        return jsonify({"error": f"unknown engine: {engine}"}), 400
    if engine != "vllm" and not os.path.exists(_ENGINE_BIN[engine]):
        return jsonify({"error": f"engine {engine} not installed on this machine"}), 400

    ok, result = _validate_vllm_args(extra, engine)
    if not ok:
        return jsonify({"error": result}), 400
    extra_tokens = result

    with _lock:
        proc = _start_process(hf_id, name, extra_tokens, engine)
        _auto_retries = 0

    return jsonify({"status": "starting", "model": name, "engine": engine, "pid": proc.pid})


@app.route("/stop", methods=["POST"])
def stop():
    global _proc, _model, _status
    _clear_last_launch()  # deliberate stop: do not resume on its own
    with _lock:
        if _proc and _proc.poll() is None:
            _append("[runner] Stop requested.")
            _kill(_proc)
            _status = "stopped"
            _model  = None
            return jsonify({"status": "stopped"})
    return jsonify({"status": "already_stopped"})


# ── OCR (docker container) / Video (ComfyUI systemd service) ────────────────
# Two side services, always active alongside the main chat model (no start/stop
# of the shared RAM/VRAM at play here, just start/stop of the service itself).
# Fixed commands, without any caller-driven argument → allowed via scoped
# NOPASSWD sudoers (see /etc/sudoers.d/vllmrunner-services), no docker.sock and
# no general systemd access.
def _sudo(*cmd, timeout=20):
    try:
        r = subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except Exception as e:
        return False, str(e)


@app.route("/ocr/status")
def ocr_status():
    ok, out = _sudo("/usr/bin/docker", "inspect", "ocr")
    if not ok:
        return jsonify({"status": "unknown", "detail": out})
    try:
        state = json.loads(out)[0]["State"]
        running = bool(state.get("Running"))
        return jsonify({"status": "running" if running else "stopped"})
    except Exception as e:
        return jsonify({"status": "unknown", "detail": str(e)})


@app.route("/ocr/start", methods=["POST"])
def ocr_start():
    ok, out = _sudo("/usr/bin/docker", "start", "ocr", timeout=60)
    return jsonify({"ok": ok, "detail": out})


@app.route("/ocr/stop", methods=["POST"])
def ocr_stop():
    ok, out = _sudo("/usr/bin/docker", "stop", "ocr", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@app.route("/ocr/launch", methods=["POST"])
def ocr_launch():
    """Recreate the OCR container with a different HF model. hf_model_id is used
    as-is (like _build_cmd for the main model — list argv, never interpreted by a
    shell, so no injection possible even if the value is malformed); vllm_args
    goes through the same allowlist as the other engines (see
    _OCR_BOOL_FLAGS/_OCR_VALUE_FLAGS)."""
    data = request.get_json(silent=True) or {}
    hf_id = (data.get("hf_model_id") or "").strip()
    if not hf_id:
        return jsonify({"ok": False, "detail": "hf_model_id missing"}), 400
    ok, tokens_or_err = _validate_vllm_args(data.get("vllm_args", "") or "", engine="ocr")
    if not ok:
        return jsonify({"ok": False, "detail": tokens_or_err}), 400
    ok, out = _sudo("/usr/local/sbin/ocr-recreate.sh", hf_id, *tokens_or_err, timeout=120)
    return jsonify({"ok": ok, "detail": out})


# Chatterbox only has these three possible variants (cf. model.repo_id in their
# config.yaml) — closed allowlist, not a flag pattern like _validate_vllm_args:
# voice-recreate.sh trusts this upstream validation again but also re-validates
# itself (defense in depth).
_VOICE_REPO_IDS = {"chatterbox", "chatterbox-turbo", "chatterbox-multilingual"}
# Second voice engine (Qwen3-TTS, Apache 2.0). Same "voice" container and same
# port: a single voice backend at a time, the GB10's unified memory already being
# shared with chat, OCR and video. Closed allowlist here too.
_VOICE_QWEN_IDS = {"Qwen3-TTS-12Hz-1.7B-Base", "Qwen3-TTS-12Hz-0.6B-Base"}


@app.route("/voice/status")
def voice_status():
    ok, out = _sudo("/usr/bin/docker", "inspect", "voice")
    if not ok:
        return jsonify({"status": "unknown", "detail": out})
    try:
        state = json.loads(out)[0]["State"]
        running = bool(state.get("Running"))
        return jsonify({"status": "running" if running else "stopped"})
    except Exception as e:
        return jsonify({"status": "unknown", "detail": str(e)})


@app.route("/voice/start", methods=["POST"])
def voice_start():
    ok, out = _sudo("/usr/bin/docker", "start", "voice", timeout=60)
    return jsonify({"ok": ok, "detail": out})


@app.route("/voice/stop", methods=["POST"])
def voice_stop():
    ok, out = _sudo("/usr/bin/docker", "stop", "voice", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@app.route("/voice/launch", methods=["POST"])
def voice_launch():
    """Recreate the voice container with one of the three Chatterbox variants.
    repo_id comes from a closed allowlist (no free argv like for OCR/vLLM): no
    command construction to validate here, just membership in _VOICE_REPO_IDS."""
    data = request.get_json(silent=True) or {}
    repo_id = (data.get("repo_id") or "").strip()
    if repo_id in _VOICE_REPO_IDS:
        script = "/usr/local/sbin/voice-recreate.sh"
    elif repo_id in _VOICE_QWEN_IDS:
        script = "/usr/local/sbin/voice-qwen-recreate.sh"
    else:
        return jsonify({"ok": False, "detail": "invalid repo_id"}), 400
    ok, out = _sudo(script, repo_id, timeout=120)
    return jsonify({"ok": ok, "detail": out})


# Transcription (dictation). Same closed allowlist as voice.
_ASR_MODEL_IDS = {
    "openai/whisper-large-v3-turbo", "openai/whisper-large-v3",
    "openai/whisper-medium", "openai/whisper-small",
}


@app.route("/asr/status")
def asr_status():
    ok, out = _sudo("/usr/bin/docker", "inspect", "asr")
    if not ok:
        return jsonify({"status": "unknown", "detail": out})
    try:
        state = json.loads(out)[0]["State"]
        return jsonify({"status": "running" if bool(state.get("Running")) else "stopped"})
    except Exception as e:
        return jsonify({"status": "unknown", "detail": str(e)})


@app.route("/asr/start", methods=["POST"])
def asr_start():
    ok, out = _sudo("/usr/bin/docker", "start", "asr", timeout=60)
    return jsonify({"ok": ok, "detail": out})


@app.route("/asr/stop", methods=["POST"])
def asr_stop():
    ok, out = _sudo("/usr/bin/docker", "stop", "asr", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@app.route("/asr/launch", methods=["POST"])
def asr_launch():
    data = request.get_json(silent=True) or {}
    model = (data.get("model_id") or "").strip()
    if model not in _ASR_MODEL_IDS:
        return jsonify({"ok": False, "detail": "invalid model_id"}), 400
    ok, out = _sudo("/usr/local/sbin/asr-recreate.sh", model, timeout=120)
    return jsonify({"ok": ok, "detail": out})


# ── Image (diffusers) — conteneur `image` sur image_net ──────────────────────
# Liste blanche fermée : chaque id correspond à un dossier diffusers déjà
# présent sur l'hôte (cf. image-recreate.sh). Pas de repo HF arbitraire ici —
# même posture que l'OCR/voix/dictée.
_IMAGE_MODEL_IDS = {"black-forest-labs/FLUX.2-klein-4B"}


@app.route("/image/status")
def image_status():
    ok, out = _sudo("/usr/bin/docker", "inspect", "image")
    if not ok:
        return jsonify({"status": "unknown", "detail": out})
    try:
        state = json.loads(out)[0]["State"]
        return jsonify({"status": "running" if bool(state.get("Running")) else "stopped"})
    except Exception as e:
        return jsonify({"status": "unknown", "detail": str(e)})


@app.route("/image/start", methods=["POST"])
def image_start():
    ok, out = _sudo("/usr/bin/docker", "start", "image", timeout=60)
    return jsonify({"ok": ok, "detail": out})


@app.route("/image/stop", methods=["POST"])
def image_stop():
    ok, out = _sudo("/usr/bin/docker", "stop", "image", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@app.route("/image/launch", methods=["POST"])
def image_launch():
    data = request.get_json(silent=True) or {}
    model = (data.get("model_id") or "").strip()
    if model not in _IMAGE_MODEL_IDS:
        return jsonify({"ok": False, "detail": "invalid model_id"}), 400
    ok, out = _sudo("/usr/local/sbin/image-recreate.sh", model, timeout=180)
    return jsonify({"ok": ok, "detail": out})


# ── Musique (diffusers, MiniMax-Music3 & co) ─────────────────────────────────
# Contrairement à l'image (liste blanche fermée), le modèle est libre : l'admin
# colle un id HuggingFace, comme pour l'OCR. On valide donc la FORME de l'id
# avant tout appel sudo — le script hôte revalide de son côté, et l'argument
# part en argv (jamais interprété par un shell).
_HF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


@app.route("/music/status")
def music_status():
    ok, out = _sudo("/usr/bin/docker", "inspect", "music")
    if not ok:
        return jsonify({"status": "unknown", "detail": out})
    try:
        state = json.loads(out)[0]["State"]
        return jsonify({"status": "running" if bool(state.get("Running")) else "stopped"})
    except Exception as e:
        return jsonify({"status": "unknown", "detail": str(e)})


@app.route("/music/start", methods=["POST"])
def music_start():
    ok, out = _sudo("/usr/bin/docker", "start", "music", timeout=60)
    return jsonify({"ok": ok, "detail": out})


@app.route("/music/stop", methods=["POST"])
def music_stop():
    ok, out = _sudo("/usr/bin/docker", "stop", "music", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@app.route("/music/launch", methods=["POST"])
def music_launch():
    data = request.get_json(silent=True) or {}
    model = (data.get("model_id") or "").strip()
    if not _HF_ID_RE.fullmatch(model):
        return jsonify({"ok": False, "detail": "invalid model_id"}), 400
    ok, out = _sudo("/usr/local/sbin/music-recreate.sh", model, timeout=180)
    return jsonify({"ok": ok, "detail": out})


@app.route("/music/logs")
def music_logs():
    return _container_logs("music")


@app.route("/video/status")
def video_status():
    ok, out = _sudo("/usr/bin/systemctl", "is-active", "comfyui.service")
    return jsonify({"status": "running" if (ok and out.strip() == "active") else "stopped"})


@app.route("/video/start", methods=["POST"])
def video_start():
    ok, out = _sudo("/usr/bin/systemctl", "start", "comfyui.service", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@app.route("/video/stop", methods=["POST"])
def video_stop():
    ok, out = _sudo("/usr/bin/systemctl", "stop", "comfyui.service", timeout=30)
    return jsonify({"ok": ok, "detail": out})


# ── Sidecar logs (read-only) ──────────────────────────────────────────────────
# Fixed tail so the sudoers rules can be EXACT commands (no wildcards): the
# vllmrunner user may read only these containers' logs, not any container's
# (whose logs could contain secrets). dgx-portal itself has no docker access,
# so it relays these to the admin Logs viewer.
_LOGS_TAIL = 400

def _combined_sudo(*cmd, timeout=20):
    """Like _sudo but returns stdout AND stderr merged — docker/vLLM write logs
    to stderr, so returning only one stream would drop most of the output."""
    try:
        r = subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True, timeout=timeout)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return str(e)

def _container_logs(container):
    out = _combined_sudo("/usr/bin/docker", "logs", "--tail", str(_LOGS_TAIL), container)
    return jsonify({"logs": out.splitlines()})


@app.route("/ocr/logs")
def ocr_logs():
    return _container_logs("ocr")


@app.route("/voice/logs")
def voice_logs():
    return _container_logs("voice")


@app.route("/image/logs")
def image_logs():
    return _container_logs("image")


@app.route("/asr/logs")
def asr_logs():
    return _container_logs("asr")


@app.route("/video/logs")
def video_logs():
    out = _combined_sudo("/usr/bin/journalctl", "-u", "comfyui.service",
                         "-n", str(_LOGS_TAIL), "--no-pager")
    return jsonify({"logs": out.splitlines()})


# ── System metrics (host) ─────────────────────────────────────────────────────
def _cpu_pct():
    def snap():
        with open('/proc/stat') as f:
            v = list(map(int, f.readline().split()[1:]))
        idle = v[3] + (v[4] if len(v) > 4 else 0)   # idle + iowait
        return idle, sum(v)
    i1, t1 = snap(); time.sleep(0.2); i2, t2 = snap()
    dt, di = t2 - t1, i2 - i1
    return round((1 - di / dt) * 100, 1) if dt > 0 else 0.0

def _ram():
    info = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, _, rest = line.partition(':')
            info[k] = int(rest.split()[0])   # kB
    total = info.get('MemTotal', 0) / 1048576.0
    avail = info.get('MemAvailable', 0) / 1048576.0
    used = total - avail
    return {'used_gb': round(used, 1), 'total_gb': round(total, 1),
            'pct': round(used / total * 100, 1) if total else 0}

def _gpu():
    exe = shutil.which('nvidia-smi')
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, '--query-gpu=utilization.gpu,power.draw,temperature.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=4)
        row = out.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in row.split(',')]
        def num(x):
            try: return float(x)
            except Exception: return None
        return {'util': num(parts[0]), 'power': num(parts[1]), 'temp': num(parts[2])}
    except Exception:
        return None

@app.route("/metrics")
def metrics():
    return jsonify({'cpu_pct': _cpu_pct(), 'ram': _ram(), 'gpu': _gpu(),
                    'model': _model, 'model_status': _status})


def _watchdog():
    """Automatically resume the last launched model if it stops unexpectedly
    (crash, system update, reboot) — not after a deliberate /stop, which clears
    the persisted state. Capped at MAX_AUTO_RETRIES consecutive attempts so it
    doesn't loop forever on a broken config."""
    global _auto_retries
    while True:
        time.sleep(10)
        last = _load_last_launch()
        if not last:
            continue
        with _lock:
            already_running = _proc is not None and _proc.poll() is None
            mid_launch = _status == "starting"
            if already_running or mid_launch:
                continue
            if _auto_retries >= MAX_AUTO_RETRIES:
                continue
            eng = last.get("engine", "vllm")
            ok, extra_tokens = _validate_vllm_args(last.get("vllm_args", ""), eng)
            if not ok:
                _append(f"[runner] auto-resume impossible, invalid args: {extra_tokens}")
                _auto_retries = MAX_AUTO_RETRIES
                continue
            _auto_retries += 1
            attempt_msg = f"[runner] model stopped unexpectedly — auto-resume (attempt {_auto_retries}/{MAX_AUTO_RETRIES})…"
            _start_process(last["hf_model_id"], last["model_name"], extra_tokens, eng)
            _append(attempt_msg)  # after _start_process (which clears _logs) so it survives


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()

    _resume = _load_last_launch()
    if _resume:
        _eng = _resume.get("engine", "vllm")
        ok, extra_tokens = _validate_vllm_args(_resume.get("vllm_args", ""), _eng)
        if ok:
            with _lock:
                _append("[runner] resuming the last model at service startup…")
                _start_process(_resume["hf_model_id"], _resume["model_name"], extra_tokens, _eng)

    app.run(host="0.0.0.0", port=8001, debug=False, threaded=True)
