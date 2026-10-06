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
# vLLM 0.28.0 venv (same torch 2.13 cu130, verified on the GB10: sm_121 detected).
# Adds 11 architectures over 0.27.1, including BailingMoeV3 — Ling-3.0 becomes
# servable by vLLM and no longer only by llama.cpp. Still NO Qwen4Exp. Opt-in via
# --vllm-028, like the previous ones.
VLLM_BIN_028 = os.environ.get("VLLM_BIN_028", "/root/venvs/vllm-028/bin/vllm")
# vLLM nightly (main). Opt-in via --vllm-nightly, NEVER by default: it is a
# pre-release. Rationale: K2-Horizon (arch `k2_horizon`) was merged into vLLM main
# only on 3 September 2026 (PR #55063) and is present in NO release — 0.28.0
# predates it, and its registry of 378 architectures does not know
# K2HorizonForCausalLM (verified).
VLLM_BIN_NIGHTLY = os.environ.get("VLLM_BIN_NIGHTLY", "/root/venvs/vllm-nightly/bin/vllm")
LLAMA_BIN    = os.environ.get("LLAMA_BIN", "/root/llama.cpp/build/bin/llama-server")
# Recent upstream llama.cpp (0.3.0-dev, August 2026), built for the GB10. It brings
# `qwen4exp` (Qwen3.8-Flash-Next), absent from the two other builds, while keeping
# `bailingmoe3` and `deepseek4`. Opt-in via --llama-next and NOT by default: the
# Ling-3.0 GGUF published by AtomicChat uses `ssm_f`/`ssm_a` tensors that only the
# TurboQuant fork can read, upstream expects `ssm_f_a`. Making it the default would
# silently break that model.
LLAMA_BIN_NEXT = os.environ.get("LLAMA_BIN_NEXT", "/root/llama-cpp-upstream/build/bin/llama-server")
# MBZUAI-IFM fork, branch model/K2Horizon, built for the GB10. The only build that
# knows the `k2_horizon` architecture: the llama.cpp PR is still OPEN, neither
# upstream nor the TurboQuant fork read it (verified). Opt-in via --llama-k2 and
# never by default: it is a dev branch, not a release.
LLAMA_BIN_K2 = os.environ.get("LLAMA_BIN_K2", "/root/llama-cpp-k2horizon/build/bin/llama-server")
# Prism ML fork (branch `prism`), prebuilt VULKAN binary for aarch64. The only
# build that reads the TERNARY tensor type `pq2_0` (id 142): measurement of
# 2026-10-01, 402 of the 851 tensors of the PQ2_0 of Ternary-Bonsai-2-27B carry it,
# and `libggml-base.so` of our three other builds only knows `q1_0`/`q2_0`.
# Upstream and the TurboQuant fork therefore fail at load time. Opt-in via
# `--llama-prism` and never by default.
#
# COMPILED HERE IN CUDA on 2026-10-01 (`/root/llama-prism-cuda`, branch `prism`,
# sm_121). The vendor aarch64 release only offers CPU and Vulkan, and the VULKAN
# binary CANNOT serve this model: it crashes with SIGABRT as soon as the context is
# reserved on `pre-allocated tensor (cache_k_l3) in a buffer (Vulkan0) that cannot
# run the operation (NONE)`. The source, however, does have the CUDA ternary
# kernels (56 `pq2` symbols in libggml-cuda.so versus 106 `arr_dmmv_pq2_0_*` on the
# Vulkan side), hence this local build. `--list-devices` now reports
# `CUDA0: NVIDIA GB10` instead of `Vulkan0`. The prebuilt binaries stay in
# /root/llama-prism should a CPU fallback ever be needed.
LLAMA_BIN_PRISM = os.environ.get("LLAMA_BIN_PRISM", "/root/llama-prism-cuda/build/bin/llama-server")
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
# An EMPTY token would pass the comparison against a request WITHOUT an
# Authorization header (`compare_digest("", "")` is true): a `RUNNER_TOKEN=` in the
# environment would therefore turn the daemon into an OPEN service, while it owns
# the GPU and can launch any model. Refused at startup, like an absent one
# (audit of 2026-10-02). The token in service is 46 characters.
if len(RUNNER_TOKEN) < 16:
    raise SystemExit("RUNNER_TOKEN absent ou trop court (16 caracteres minimum)")

# ExLlamaV3 engine, served by TabbyAPI (OpenAI API). Added 2026-10-01 for
# MiMo-V2.6-Flash-RL in EXL3 2.27 bpw: 85 GiB for the 309B, the only complete
# version of this model that fits on the GB10 (the smallest GGUF is 126 GB).
# No published aarch64 wheel: exllamav3 is compiled locally in this venv.
EXL3_PY   = os.environ.get("EXL3_PY", "/root/venvs/exl3/bin/python")
# Symlink directories the runner republishes at every launch (see
# _exl3_served_layout). Under its HOME: the only place it can write.
EXL3_RUN_DIR = os.path.join(os.environ.get("HOME", "/var/lib/vllm-runner"), "exl3")
TABBY_DIR = os.environ.get("TABBY_DIR", "/root/tabbyAPI")
ENGINES = ("vllm", "llamacpp", "ds4", "exllamav3")
_ENGINE_BIN = {"vllm": VLLM_BIN, "llamacpp": LLAMA_BIN, "ds4": DS4_BIN,
               "exllamav3": os.path.join(TABBY_DIR, "main.py")}

# Persist the last successful launch so it can be resumed automatically after a
# service restart (system update, reboot, crash) — except on a deliberate /stop,
# which clears this file.
STATE_FILE = os.path.join(os.environ.get("HOME", "/var/lib/vllm-runner"), "last_model.json")
MAX_AUTO_RETRIES = 3

# --- Memory guards (2026-10-01, after a crash loop) ---------------------------
# On the GB10 the GPU memory is taken from system RAM but APPEARS IN NO PROCESS
# and in no cgroup: a 26 GB model counts for only 2 GB of anon there (measured).
# The kernel's OOM killer is therefore blind to the real cause — on 2026-10-01 it
# killed 443 processes without freeing anything, while ~119 GB stayed held by the
# driver (Mem-Info tally: 0.77 GB free, almost nothing in anon/cache), and the
# machine froze then rebooted twice. A MemoryMax on the unit would therefore be
# illusory. The only reliable signal is MemAvailable, which GPU memory does lower:
# that is what the guards below watch.
#
# Floor to LAUNCH a model: below it, the previous instance's memory has not been
# returned (the driver frees it with a delay) and we would pile up on top of it.
MIN_FREE_TO_LAUNCH_GIB = float(os.environ.get("MIN_FREE_TO_LAUNCH_GIB", "30"))
# Critical threshold WHILE a model runs: we kill our own llama-server before the
# kernel goes into a global OOM and takes the whole machine down.
CRITICAL_FREE_GIB = float(os.environ.get("CRITICAL_FREE_GIB", "2"))
# A model must hold for this time WITHOUT interruption to reset the failure
# counter. Answering HTTP 200 is not enough: in the 2026-10-01 loop the model
# answered, the counter reset, then it was killed a minute later — so
# MAX_AUTO_RETRIES was never reached.
STABLE_SECONDS = int(os.environ.get("RESUME_STABLE_SECONDS", "600"))
# Resume failure counter PERSISTED to disk: in RAM it reset on every service
# restart (OOMPolicy=stop restarts the runner on every OOM), which made the loop
# infinite, even across reboots.
RESUME_FAILS_FILE = os.path.join(os.environ.get("HOME", "/var/lib/vllm-runner"),
                                 "resume_failures")

app = Flask(__name__)

_lock   = threading.Lock()
_proc   = None
_model  = None
_engine = None        # engine of the current model: 'vllm' | 'llamacpp'
_logs   = []
_status = "stopped"   # stopped | starting | running | error
_auto_retries = 0     # consecutive failed automatic relaunch attempts
_running_since = None # instant when the current model went "running"

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
    "--vllm-028": VLLM_BIN_028,
    "--vllm-nightly": VLLM_BIN_NIGHTLY,
    "--llama-next": LLAMA_BIN_NEXT,
    "--llama-k2": LLAMA_BIN_K2,
    "--llama-prism": LLAMA_BIN_PRISM,
}
# Each pseudo-flag is accepted only for ITS engine: offering --vllm-028 to a
# llama.cpp launch makes no sense, and accepting it would point llama.cpp at a
# vLLM binary. The split lives here, the allowlists use it below.
_LLAMA_BIN_FLAGS = {f for f in _BIN_FLAGS if f.startswith("--llama-")}
_VLLM_BIN_FLAGS  = set(_BIN_FLAGS) - _LLAMA_BIN_FLAGS
_BOOL_FLAGS |= _VLLM_BIN_FLAGS
_VALUE_FLAGS = {
    "--tool-call-parser", "--dtype", "--max-model-len",
    "--gpu-memory-utilization", "--max-num-seqs", "--kv-cache-dtype",
    "--max-num-batched-tokens", "--block-size", "--swap-space",
    "--quantization", "--tensor-parallel-size", "--pipeline-parallel-size",
    "--reasoning-parser",
    # Attention kernel choice. Needed on GB10: vLLM prefers FLASHINFER, but this
    # card's capability is sm_121 and the required FlashInfer paths are gated by
    # `is_sm100a_supported()` — which returns False here. The engine then fails at
    # memory profiling on "FlashInfer backend is not available", even though the
    # package IS installed. TRITON_ATTN is the alternative the engine itself
    # advertises among its potential backends.
    "--attention-backend",
    # Default values for the template variables, merged UNDER those of each
    # request. On K2-Horizon they set the default `reasoning_effort`
    # (high|medium|low), i.e. the tag pair the parser expects when the client does
    # not ask for one. Careful: on the vLLM side the flag is called
    # --default-chat-template-kwargs ; --chat-template-kwargs is the llama.cpp one
    # and makes `vllm serve` fail with "unrecognized arguments".
    "--default-chat-template-kwargs", "--limit-mm-per-prompt",
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
    # KV unified: a single buffer shared by the slots. Without it, --parallel N
    # DIVIDES the context (262k/8 = 32k per session); with it, each session can
    # grow up to the full pool when the others are idle.
    "--kv-unified", "--no-kv-unified",
    # Truncates old tokens when a slot is full instead of ERRORing (otherwise a
    # client like OpenCode retries the same over-long request → crash).
    "--context-shift", "--no-context-shift",
}
# Without this line the binary pseudo-flags were refused by validation BEFORE
# reaching _start_process: --llama-next was therefore unusable all along
# ("flag not allowed"), even though _build_cmd does know how to use it.
_LLAMA_BOOL_FLAGS |= _LLAMA_BIN_FLAGS
_LLAMA_VALUE_FLAGS = {
    # 0.5.0: `--ctx-size` sizes the KV cache PER SLOT (measurement of
    # 2026-09-25: 4 private 262k slots = 4 M tokens of cache, ~124 GiB, OOM,
    # where the old binary held the same service within 109 GiB).
    # `--kv-unified-per-slot N` declares the window PER SESSION and lets the
    # pool size itself to n_parallel x N: that is how to ask for 4 x 256k with
    # this binary. The portal reads this flag to advertise the right input limit
    # (see `effective_ctx`).
    "--kv-unified-per-slot",
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
    # Vision projector (multimodal). Same rule as above: file name only,
    # resolved NEXT TO the weights (see _resolve_mmproj_tokens) — a projector
    # only makes sense with the quantization it was produced for.
    "--mmproj",
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


# Chat models APPROVED BY NAME for --trust-remote-code. The flag stays absent from
# _BOOL_FLAGS (RCE via the HF repo code): this is a per-model exception, not an
# opening. Listing a model here is a security decision — launching that model runs
# the Python code of its repository.
_TRUST_RC_MODELS = {
    # Empty: no active exception. K2-Horizon was listed here on 2026-09-07 then
    # removed along with the model — keeping an approval for a model we no longer
    # serve widens the surface without bringing anything.
}


def _repo_id_of(hf_id):
    """HF repository matching `hf_id`, whether given as an id or as a cache
    snapshot path (.../models--org--name/snapshots/<sha>).

    Without this, approving "org/name" would not recognize the same model when
    referenced by its cache path — and the exception would fail in a way nobody
    could understand.
    """
    m = re.search(r'models--([^/]+?)--(.+?)/snapshots/', hf_id or '')
    return f"{m.group(1)}/{m.group(2)}" if m else (hf_id or '')


# ── exllamav3 allowlist (TabbyAPI options) ─────────────────────────────────
# --host/--port/--disable-auth/--model-dir/--draft-model-dir are set by the
# runner. No flag taking a PATH (--config, lora/embeddings folders): the model
# and drafter names are accepted ALONE and resolved under the controlled
# `local:` directory (see _resolve_exl3_tokens), like --mmproj.
_EXL3_BOOL_FLAGS = set()          # TabbyAPI takes true/false as a VALUE
_EXL3_VALUE_FLAGS = {
    "--model-name", "--draft-model-name", "--max-seq-len", "--cache-size",
    "--cache-mode", "--chunk-size", "--max-batch-size", "--vision", "--reasoning",
    "--draft-mode", "--draft-num-tokens", "--draft-cache-mode", "--gpu-split-auto",
}


def _flags_for(engine):
    if engine == "exllamav3":
        return _EXL3_BOOL_FLAGS, _EXL3_VALUE_FLAGS
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


def _validate_vllm_args(extra, engine="vllm", hf_id=""):
    """Return (ok, tokens_or_error_message). The allowlist depends on the engine.

    `hf_id` is only used for --trust-remote-code: this flag is accepted for a chat
    model only if the repository is listed in _TRUST_RC_MODELS (OCR already carries
    it in its own allowlist).
    """
    bool_flags, value_flags = _flags_for(engine)
    tokens = extra.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in bool_flags:
            i += 1
        elif tok == "--trust-remote-code" and _repo_id_of(hf_id) in _TRUST_RC_MODELS:
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


# --- Startup journal on disk ------------------------------------------------
# The memory buffer alone is not enough to diagnose a startup failure:
# ~2000 lines, and `_start_process` empties it at EVERY attempt, so with the 3
# auto-resumes the root cause was overwritten before being read (finding recorded
# on 2026-09-13). So we double the stream with a file PER STARTUP.
#
# Absolute rule: this journal must NEVER prevent a launch. Any error (full disk,
# missing folder, permissions) leaves `journal = None` and the model starts as
# before. The runner runs as `vllmrunner`: this folder belongs to it, it is
# created outside the service (no restart needed).
LOG_DIR = os.environ.get("RUNNER_LOG_DIR", "/var/lib/vllm-runner/logs")
_LOG_MAX_BYTES = 20 * 1024 * 1024
_LOG_KEEP = 12


class _Journal:
    """Size-capped startup file, silent on failure.

    A class rather than a bare file handle: it must remember the bytes already
    written to stop at the cap without `tell()` on every line, and stop writing
    for good if the disk refuses — without ever raising, since the caller is the
    loop reading the engine's output.
    """

    def __init__(self, chemin):
        self.chemin = chemin
        self._f = open(chemin, "w", encoding="utf-8", errors="replace")
        self._ecrits = 0
        self._ouvert = True

    def ecrire(self, ligne):
        if not self._ouvert:
            return
        try:
            self._f.write(ligne + "\n")
            self._ecrits += len(ligne) + 1
            if self._ecrits > _LOG_MAX_BYTES:
                self._f.write(f"[runner] journal tronqué à {_LOG_MAX_BYTES // 1024 ** 2} Mo\n")
                self._fermer()
        except Exception:
            self._fermer()

    def _fermer(self):
        self._ouvert = False
        try:
            self._f.close()
        except Exception:
            pass


def _elague_journaux():
    """Keep only the latest startups (the disk is shared with the weights)."""
    try:
        fichiers = [os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
                    if f.endswith(".log")]
        fichiers.sort(key=lambda p: os.stat(p).st_mtime, reverse=True)
        for vieux in fichiers[_LOG_KEEP:]:
            os.remove(vieux)
    except Exception:
        pass


def _ouvre_journal(name, engine):
    """Open the journal for THIS startup, or None if that is not possible."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        propre = re.sub(r"[^A-Za-z0-9._-]", "_", name or "modele")[:60] or "modele"
        chemin = os.path.join(LOG_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{engine}-{propre}.log")
        journal = _Journal(chemin)
        _elague_journaux()
        return journal
    except Exception:
        return None


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


def _read_resume_fails():
    try:
        with open(RESUME_FAILS_FILE) as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _write_resume_fails(n):
    try:
        with open(RESUME_FAILS_FILE, "w") as f:
            f.write(str(int(n)))
    except Exception:
        pass


def _launch_memory_ok(context):
    """Wait until the previous instance's memory is released, then REFUSE to
    launch if less than MIN_FREE_TO_LAUNCH_GIB remains. To be called OUTSIDE the
    lock: the wait can last a minute and would block /status."""
    _wait_mem_release()
    free = _mem_available_gib()
    if free is not None and free < MIN_FREE_TO_LAUNCH_GIB:
        _append(f"[runner] {context} REFUSE : {free:.1f} GiB disponibles seulement "
                f"(plancher {MIN_FREE_TO_LAUNCH_GIB:.0f}). La memoire d'un modele "
                f"precedent n'a pas ete rendue — lancer maintenant ferait planter la machine.")
        return False
    return True


def _memory_guard(proc):
    """As long as OUR model runs: if MemAvailable drops below CRITICAL_FREE_GIB,
    we kill it ourselves. It is the only way to get there before the kernel, whose
    OOM killer cannot see GPU memory and would kill everything else instead."""
    while proc is _proc and proc.poll() is None:
        free = _mem_available_gib()
        if free is not None and free < CRITICAL_FREE_GIB:
            _append(f"[runner] MEMOIRE CRITIQUE : {free:.1f} GiB disponibles — arret "
                    f"force du modele pour eviter un OOM global de la machine.")
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            return
        time.sleep(0.5)


def _reader(proc, journal=None):
    global _status, _proc, _model, _auto_retries, _running_since
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            _append(line)
            if journal is not None:
                journal.ecrire(line)
            # vLLM is ready when it prints "Application startup complete"
            # (only touch the global status if this process is still the active one —
            # otherwise an old reader thread, still draining a process killed by
            # /launch, could overwrite the status of the NEW process that is starting)
            if "Application startup complete" in line and proc is _proc:
                _status = "running"
                _running_since = time.time()
                _auto_retries = 0  # this launch worked, restart with a fresh retry budget
    except Exception as e:
        _append(f"[runner] read interrupted: {e}")
        if journal is not None:
            journal.ecrire(f"[runner] read interrupted: {e}")
    proc.wait()
    with _lock:
        if proc is _proc and _status != "stopped":
            _status = "error" if proc.returncode not in (0, -15, -9) else "stopped"
        _append(f"[runner] Process exited (code {proc.returncode})")


def _health_watch(proc):
    """Flip the status to 'running' as soon as vLLM actually responds, without
    relying on logs: --uvicorn-log-level warning hides "Application startup
    complete", which left the status stuck on 'starting' while the model served."""
    global _status, _auto_retries, _running_since
    url = "http://127.0.0.1:8000/v1/models"
    while proc is _proc and proc.poll() is None:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200 and proc is _proc:
                    _status = "running"
                    _running_since = time.time()
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


def _resolve_mmproj_tokens(tokens, hf_id):
    """Replace the value of --mmproj (file name only) with the absolute path,
    resolved NEXT TO the model's own weights.

    Same rule as --chat-template-file: no separator, no "..", so no arbitrary
    file can be handed to the engine. A projector only makes sense alongside the
    quantisation it was produced against, so there is no legitimate reason to
    accept a path pointing anywhere else — which is what keeps this flag from
    becoming a way to read files outside MODELS_DIR.
    """
    out = list(tokens)
    for i, t in enumerate(out):
        if t == "--mmproj" and i + 1 < len(out):
            raw = out[i + 1]
            if "/" in raw or "\\" in raw or ".." in raw:
                raise ValueError("invalid mmproj name")
            if not hf_id.startswith("local:"):
                raise ValueError("--mmproj requires a local: model")
            path = os.path.join(os.path.dirname(_resolve_gguf(hf_id)), raw)
            if not os.path.isfile(path):
                raise FileNotFoundError(
                    f'mmproj "{raw}" not found next to the weights')
            out[i + 1] = path
    return out


def _resolve_exl3_tokens(tokens, hf_id):
    """`local:` directory of the model + name checks for --model-name /
    --draft-model-name: name ALONE (no separator, no ".."), existing subdirectory.
    Same guard as _resolve_gguf: the directory stays strictly under MODELS_DIR."""
    if not hf_id.startswith("local:"):
        raise ValueError("exllamav3 requires a local: model")
    slug = re.sub(r'[^A-Za-z0-9._-]', '', hf_id[len("local:"):])
    base = os.path.join(MODELS_DIR, slug)
    _root = os.path.realpath(MODELS_DIR)
    if slug in ("", ".", "..") or not os.path.realpath(base).startswith(_root + os.sep) \
            or not os.path.isdir(base):
        raise FileNotFoundError(f"local model \"{slug}\" not found in {MODELS_DIR}")
    out = list(tokens)
    for i, t in enumerate(out):
        if t in ("--model-name", "--draft-model-name") and i + 1 < len(out):
            raw = out[i + 1]
            if not raw or "/" in raw or "\\" in raw or ".." in raw:
                raise ValueError(f"invalid name for {t}")
            if not os.path.isdir(os.path.join(base, raw)):
                raise FileNotFoundError(f"{t} \"{raw}\" not found in {base}")
    if "--model-name" not in out:
        raise ValueError("exllamav3: --model-name is required")
    return base, out


def _exl3_served_layout(base, name, toks):
    """Publish the model UNDER ITS CATALOG NAME for TabbyAPI.

    The portal expects /v1/models to return the catalog name, and that name only
    (llama.cpp gives it via --alias, vLLM via --served-model-name). TabbyAPI, on
    the other hand, lists the FOLDER NAMES of --model-dir, and all its requests are
    « admin » auth off: it returned both the model folder AND the drafter folder.
    The portal took the first one (the drafter), did not find it in the catalog and
    fell back to the `vllm` engine — false health, and about fifteen callers of
    get_running_models() disturbed. TabbyAPI names an entry after the LINK name
    (path.name), not its target: a folder holding only a <catalog> -> model link,
    and the drafter in a separate folder, is enough.
    """
    alias = re.sub(r'[^A-Za-z0-9._-]', '', name or '')
    if alias in ("", ".", ".."):
        raise ValueError("invalid served model name")
    out = list(toks)
    mdir = os.path.join(EXL3_RUN_DIR, "model")
    ddir = os.path.join(EXL3_RUN_DIR, "draft")
    for d in (mdir, ddir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    i = out.index("--model-name")
    os.symlink(os.path.join(base, out[i + 1]), os.path.join(mdir, alias))
    out[i + 1] = alias
    if "--draft-model-name" in out:
        j = out.index("--draft-model-name")
        os.symlink(os.path.join(base, out[j + 1]), os.path.join(ddir, out[j + 1]))
    return mdir, ddir, out


def _build_cmd(hf_id, name, extra_tokens, engine, bin_override=None):
    """Engine command line. They all serve an OpenAI API on :8000, so nothing
    downstream changes (LiteLLM, portal, playground)."""
    if engine == "exllamav3":
        # TabbyAPI: same OpenAI API on :8000, nothing changes downstream. Auth is
        # off as for the other engines: :8000 is reachable only from the docker
        # network (vllm-restrict.service).
        base, toks = _resolve_exl3_tokens(extra_tokens, hf_id)
        mdir, ddir, toks = _exl3_served_layout(base, name, toks)
        # Two HARDCODED settings, out of reach of vllm_args:
        #  --disable-fetch-requests: by default TabbyAPI itself fetches the images
        #    given by URL. Any caller of the API could then make the DGX query
        #    internal addresses (admin garage, Traefik, NAS) — an SSRF, which
        #    websearch.url_publique forbids elsewhere. base64 images (the
        #    playground ones) do not go through this path.
        #  --allowed-origins: with auth off, the default CORS "*" would let any web
        #    page open on the machine (there is a desktop session) read the
        #    responses, admin endpoints included. Nobody calls :8000 from a
        #    browser: we allow only a nonexistent origin (.invalid, reserved TLD),
        #    the option requiring at least one value.
        cmd = [EXL3_PY, os.path.join(TABBY_DIR, "main.py"),
               "--host", "0.0.0.0", "--port", "8000", "--disable-auth", "true",
               "--disable-fetch-requests", "true",
               "--allowed-origins", "https://no-browser.invalid",
               "--model-dir", mdir]
        if "--draft-model-name" in toks:
            cmd += ["--draft-model-dir", ddir]
        return cmd + toks
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
        extra_tokens = _resolve_mmproj_tokens(extra_tokens, hf_id)
        # bin_override: set by --llama-next (see _BIN_FLAGS). Without it the flag
        # would be removed from the argv but have NO effect, the binary being
        # hardcoded here — silent failure.
        return [bin_override or LLAMA_BIN] + src + [
                "--host", "0.0.0.0", "--port", "8000",
                "--alias", name,
                "--metrics"] + extra_tokens
    return [bin_override or VLLM_BIN, "serve", hf_id,
            "--port", "8000", "--host", "0.0.0.0",
            "--served-model-name", name] + extra_tokens


def _start_process(hf_id, name, extra_tokens, engine="vllm"):
    """Launch the inference engine. Must be called with _lock already held."""
    global _proc, _model, _status, _engine

    # The command is built BEFORE stopping the running model (audit of
    # 2026-10-02). `_build_cmd` validates and resolves (local model, template,
    # mmproj) and can FAIL: the previous order therefore killed the SERVED model
    # before discovering that the relaunch was impossible — a typo left the
    # platform with no model at all, while the runner only serves one at a time.
    # Here the failure surfaces before any stop and the service stays as it is.
    #
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
    bin_override = None
    for flag, bin_path in _BIN_FLAGS.items():
        if flag in extra_tokens:
            extra_tokens = [t for t in extra_tokens if t != flag]
            bin_override = bin_path

    cmd = _build_cmd(hf_id, name, extra_tokens, engine, bin_override=bin_override)

    killed = bool(_proc and _proc.poll() is None)
    if killed:
        _append("[runner] Stopping previous model…")
        _kill(_proc)

    _logs.clear()
    _model  = name
    _engine = engine
    _status = "starting"
    # On-disk journal of the startup: the ONLY place where the root cause of a
    # failure will survive, since this clear() wipes the memory buffer at every
    # attempt (including at every auto-resume).
    journal = _ouvre_journal(name, engine)
    if journal is not None:
        journal.ecrire(f"[runner] {time.strftime('%Y-%m-%dT%H:%M:%S')} démarrage "
                       f"{engine} — {name} ({hf_id})")

    # The previous model was just killed: wait for the driver to release the
    # unified memory, otherwise the new process OOMs at startup.
    if killed:
        _wait_mem_release()

    # Here we only LOG the command already built above (before the previous model
    # was stopped): rebuilding it here after the kill was exactly the flaw fixed
    # on 2026-10-02.
    _append(f"[runner] ({engine}) $ {' '.join(cmd)}")
    if journal is not None:
        journal.ecrire(f"$ {' '.join(cmd)}")
    if model_env:
        _append(f"[runner] model-specific env: {model_env}")
        if journal is not None:
            journal.ecrire(f"[runner] model-specific env: {model_env}")

    # Explicit minimal env rather than **os.environ — avoids leaking the full
    # root environment (assorted secrets) into /logs and /stream.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "HF_HOME": HF_HOME,
        "PYTHONUNBUFFERED": "1",
        # FlashInfer ships its kernels in a companion package (`flashinfer-cubin`)
        # whose version trails the Python one (0.6.13 vs 0.7.0.post1 — there is
        # no 0.7 cubin on PyPI). Their version gate is checked at IMPORT and
        # kills vLLM outright, while the kernels we use on GB10 are the CUTLASS
        # paths, not those cubins. Bypass the check rather than pin a stale
        # flashinfer-python (that dragged torch back to 2.10 and broke vLLM).
        "FLASHINFER_DISABLE_VERSION_CHECK": "1",
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
        cwd=TABBY_DIR if engine == "exllamav3" else None,
    )
    threading.Thread(target=_reader, args=(_proc, journal), daemon=True).start()
    threading.Thread(target=_health_watch, args=(_proc,), daemon=True).start()
    threading.Thread(target=_memory_guard, args=(_proc,), daemon=True).start()
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
    # Shape-guard the model id (audit M6) : a HF repo id ("org/name"), a local
    # GGUF alias ("local:<name>"), or an absolute path to a downloaded snapshot
    # (vllm) — all charset-bound — reject anything else BEFORE it flows into
    # _start_process/_resolve_gguf (argv + path). No control chars / quotes.
    if not (_HF_ID_RE.fullmatch(hf_id) or _LOCAL_ID_RE.fullmatch(hf_id) or _ABS_PATH_RE.fullmatch(hf_id)):
        return jsonify({"error": "invalid hf_model_id"}), 400
    # `_ABS_PATH_RE` allows the DOT, so `/models/../../etc/shadow` passed as-is:
    # the path goes into the engine's argv (`-m`), which reads the file. The
    # runner does not open it itself, but nothing justifies accepting `..` in a
    # path we claim is "absolute and bounded" (audit of 2026-10-02).
    if any(part == ".." for part in hf_id.split("/")):
        return jsonify({"error": "invalid hf_model_id (.. refused)"}), 400
    if engine not in ENGINES:
        return jsonify({"error": f"unknown engine: {engine}"}), 400
    if engine != "vllm" and not os.path.exists(_ENGINE_BIN[engine]):
        return jsonify({"error": f"engine {engine} not installed on this machine"}), 400

    ok, result = _validate_vllm_args(extra, engine, hf_id)
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


@app.route("/models/delete-files", methods=["POST"])
def delete_model_files():
    """Delete from disk the weights of a model removed from the catalog.

    The files belong to root: deletion goes through
    /usr/local/sbin/model-files-rm.sh (scoped sudo), which revalidates everything.
    Here we refuse upstream what must never reach it: a malformed id, an absolute
    path (never deleted remotely), and the SERVED model or one awaiting resume —
    deleting its weights under an engine that reads them (mmap) would crash the
    generation, and the watchdog would relaunch a model without files.
    """
    data  = request.get_json(silent=True) or {}
    hf_id = (data.get("hf_model_id") or "").strip()
    if _LOCAL_ID_RE.fullmatch(hf_id):
        kind, ident = "local", hf_id[len("local:"):]
    elif _HF_ID_RE.fullmatch(hf_id):
        kind, ident = "hf", hf_id
    else:
        return jsonify({"error": "hf_model_id invalide (local:<dossier> ou org/nom)"}), 400
    if ".." in ident:
        return jsonify({"error": "hf_model_id invalide (.. refusé)"}), 400
    with _lock:
        try:
            with open(STATE_FILE) as f:
                en_cours = (json.load(f) or {}).get("hf_model_id")
        except (OSError, ValueError):
            en_cours = None
        if en_cours == hf_id:
            return jsonify({"error": "modèle servi (ou en attente de reprise) : "
                                     "arrête-le avant d'effacer ses fichiers"}), 409
        ok, out = _sudo("/usr/local/sbin/model-files-rm.sh", kind, ident, timeout=600)
    if not ok:
        absent = "absent" in out
        _append(f"[runner] Effacement des fichiers de {hf_id} : {'absents' if absent else 'ÉCHEC'} ({out[-200:]})")
        return jsonify({"error": out[-300:] or "échec", "absent": absent}), 404 if absent else 500
    try:
        octets = int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        octets = 0
    _append(f"[runner] Fichiers de {hf_id} effacés ({octets / 2**30:.1f} Gio libérés).")
    return jsonify({"status": "deleted", "bytes": octets})


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
    # Same SHAPE check as /launch (audit of 2026-10-02): without it, a
    # `"hf_model_id": "--model=/chemin/local"` flowed through to `vllm serve`, which
    # interprets it as an OPTION — the caller was driving the OCR container's argv
    # instead of a repository id, and the shape check of `_validate_vllm_args` was
    # bypassed by the argument itself. The portal catalog only produces
    # `org/name` (already validated by this expression).
    if not _HF_ID_RE.fullmatch(hf_id):
        return jsonify({"ok": False, "detail": "invalid hf_model_id"}), 400
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


# ── Image (diffusers) — `image` container on image_net ──────────────────────
# Closed allowlist: each id maps to a diffusers folder already present on the
# host (see image-recreate.sh). No arbitrary HF repo here — same posture as
# OCR/voice/dictation.
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


# ── Music (diffusers, MiniMax-Music3 & co) ──────────────────────────────────
# Unlike image (closed allowlist), the model is free: the admin pastes a
# HuggingFace id, as for OCR. So we validate the SHAPE of the id before any sudo
# call — the host script revalidates on its side, and the argument goes in argv
# (never interpreted by a shell).
_HF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
# Local GGUF ("local:<name>", ds4/llamacpp) — same charset, no required slash.
_LOCAL_ID_RE = re.compile(r"^local:[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
# Absolute local path to an already downloaded HF snapshot (vllm engine) —
# e.g. /root/.cache/huggingface/hub/models--…/snapshots/<sha>. Accepted because
# vllm receives the id verbatim in argv (never in a shell); the risk is a local
# file read, not execution, and /launch already sits behind admin auth.
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+-]+(/[A-Za-z0-9._/@+-]+)*$")


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
_cpu_prev = None  # (idle, total, monotonic ts) from the previous /metrics call

def _cpu_pct():
    """Host CPU %, measured over the interval since the last call instead of
    sleeping 0.2 s inside the request. time.sleep(0.2) here made /metrics — and
    therefore /api/home, which calls it every ~5 s — take ~228 ms per request,
    which is what made the portal home page feel slow. The delta over the poll
    interval (~5 s) is just as accurate for a dashboard and returns in ~0 ms."""
    global _cpu_prev
    def snap():
        with open('/proc/stat') as f:
            v = list(map(int, f.readline().split()[1:]))
        idle = v[3] + (v[4] if len(v) > 4 else 0)   # idle + iowait
        return idle, sum(v)
    idle, total = snap()
    di = dt = 0
    if _cpu_prev is not None:
        p_idle, p_total, _ = _cpu_prev
        di = idle - p_idle
        dt = total - p_total
    _cpu_prev = (idle, total, time.monotonic())
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
    desactive_annonce = False
    while True:
        time.sleep(10)
        # A model that holds STABLE_SECONDS without interruption resets the
        # failure counter — and only it (see STABLE_SECONDS).
        if (_status == "running" and _running_since
                and time.time() - _running_since >= STABLE_SECONDS
                and _read_resume_fails()):
            _write_resume_fails(0)
            _auto_retries = 0
            desactive_annonce = False
            _append("[runner] model stable — auto-resume failure counter reset")
        last = _load_last_launch()
        if not last:
            continue
        with _lock:
            already_running = _proc is not None and _proc.poll() is None
            mid_launch = _status == "starting"
        if already_running or mid_launch:
            continue
        fails = _read_resume_fails()
        if fails >= MAX_AUTO_RETRIES:
            if not desactive_annonce:
                _append(f"[runner] auto-resume DESACTIVE apres {fails} echecs consecutifs "
                        f"— relancer manuellement depuis l'Admin.")
                desactive_annonce = True
            continue
        eng = last.get("engine", "vllm")
        ok, extra_tokens = _validate_vllm_args(last.get("vllm_args", ""), eng,
                                               last.get("hf_model_id", ""))
        if not ok:
            _append(f"[runner] auto-resume impossible, invalid args: {extra_tokens}")
            _write_resume_fails(MAX_AUTO_RETRIES)
            continue
        # Memory wait/floor OUTSIDE the lock. A refusal counts as a failure:
        # that is what eventually stops a loop instead of feeding it.
        if not _launch_memory_ok("auto-resume"):
            _write_resume_fails(fails + 1)
            continue
        with _lock:
            if _proc is not None and _proc.poll() is None:
                continue          # launched meanwhile (Admin): nothing to do
            _write_resume_fails(fails + 1)
            _auto_retries = fails + 1
            attempt_msg = (f"[runner] model stopped unexpectedly — auto-resume "
                           f"(attempt {fails + 1}/{MAX_AUTO_RETRIES})…")
            _start_process(last["hf_model_id"], last["model_name"], extra_tokens, eng)
            _append(attempt_msg)  # after _start_process (which clears _logs) so it survives


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()

    # Startup resume is left to the watchdog (first pass ~10 s later).
    # It relaunched the model IMMEDIATELY, without waiting for the driver to
    # return the killed instance's GPU memory: after an OOM, the new model piled
    # up on top of it and crashed again — infinite loop, even across reboots. The
    # watchdog, on the other hand, applies the memory wait, the floor and the
    # persisted counter. Doing it here TOO would open a race: the memory wait can
    # last a minute, during which the watchdog would start a second instance.
    if _load_last_launch():
        _append("[runner] service started — the watchdog will resume the last model "
                "once memory is confirmed free")

    app.run(host="0.0.0.0", port=8001, debug=False, threaded=True)
