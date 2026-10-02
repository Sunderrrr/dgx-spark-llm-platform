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
# vLLM 0.28.0 venv (meme torch 2.13 cu130, verifie sur le GB10 : sm_121 detecte).
# Ajoute 11 architectures par rapport a 0.27.1, dont BailingMoeV3 — Ling-3.0
# devient servable par vLLM et non plus seulement par llama.cpp. Toujours PAS de
# Qwen4Exp. Opt-in via --vllm-028, comme les precedentes.
VLLM_BIN_028 = os.environ.get("VLLM_BIN_028", "/root/venvs/vllm-028/bin/vllm")
# vLLM nightly (main). Opt-in via --vllm-nightly, JAMAIS par defaut : c'est une
# pre-release. Raison d'etre : K2-Horizon (arch `k2_horizon`) n'a ete fusionne dans
# vLLM main que le 3 septembre 2026 (PR #55063) et n'est present dans AUCUNE
# release — 0.28.0 lui est anterieure, son registre de 378 architectures ne connait
# pas K2HorizonForCausalLM (verifie).
VLLM_BIN_NIGHTLY = os.environ.get("VLLM_BIN_NIGHTLY", "/root/venvs/vllm-nightly/bin/vllm")
LLAMA_BIN    = os.environ.get("LLAMA_BIN", "/root/llama.cpp/build/bin/llama-server")
# llama.cpp amont recent (0.3.0-dev, aout 2026), compile pour le GB10. Il apporte
# `qwen4exp` (Qwen3.8-Flash-Next), absent des deux autres builds, tout en gardant
# `bailingmoe3` et `deepseek4`. Opt-in via --llama-next et NON par defaut : le
# GGUF de Ling-3.0 publie par AtomicChat utilise des tenseurs `ssm_f`/`ssm_a` que
# seul le fork TurboQuant sait lire, l'amont attend `ssm_f_a`. Basculer par
# defaut casserait ce modele en silence.
LLAMA_BIN_NEXT = os.environ.get("LLAMA_BIN_NEXT", "/root/llama-cpp-upstream/build/bin/llama-server")
# Fork MBZUAI-IFM branche model/K2Horizon, compile pour le GB10. Seul build qui
# connaisse l'architecture `k2_horizon` : la PR llama.cpp est encore OUVERTE, ni
# l'amont ni le fork TurboQuant ne la lisent (verifie). Opt-in par --llama-k2 et
# jamais par defaut : c'est une branche de dev, pas une release.
LLAMA_BIN_K2 = os.environ.get("LLAMA_BIN_K2", "/root/llama-cpp-k2horizon/build/bin/llama-server")
# Fork Prism ML (branche `prism`), binaire VULKAN preconstruit pour aarch64. Seul
# build qui lise le type de tenseur TERNAIRE `pq2_0` (id 142) : mesure du
# 2026-10-01, 402 des 851 tenseurs du PQ2_0 de Ternary-Bonsai-2-27B le portent, et
# `libggml-base.so` de nos trois autres builds ne connait que `q1_0`/`q2_0`.
# L'amont et le fork TurboQuant echouent donc au chargement. Opt-in par
# `--llama-prism` et jamais par defaut.
#
# COMPILE ICI EN CUDA le 2026-10-01 (`/root/llama-prism-cuda`, branche `prism`,
# sm_121). La release aarch64 de l'editeur n'offre que CPU et Vulkan, et le binaire
# VULKAN ne peut PAS servir ce modele : il plante en SIGABRT des la reservation du
# contexte sur `pre-allocated tensor (cache_k_l3) in a buffer (Vulkan0) that cannot
# run the operation (NONE)`. Le source, lui, a bien les noyaux ternaires CUDA
# (56 symboles `pq2` dans libggml-cuda.so contre 106 `arr_dmmv_pq2_0_*` cote
# Vulkan), d'ou cette compilation locale. `--list-devices` annonce desormais
# `CUDA0: NVIDIA GB10` et non plus `Vulkan0`. Les binaires precompilies restent
# dans /root/llama-prism si un repli CPU est un jour necessaire.
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
# Un jeton VIDE passerait la comparaison avec une requête SANS en-tête
# Authorization (`compare_digest("", "")` est vrai) : un `RUNNER_TOKEN=` dans
# l'environnement transformait donc le daemon en service OUVERT, alors qu'il
# possède le GPU et peut lancer n'importe quel modèle. On refuse au démarrage,
# comme pour l'absence (audit du 2026-10-02). Le jeton en service en fait 46.
if len(RUNNER_TOKEN) < 16:
    raise SystemExit("RUNNER_TOKEN absent ou trop court (16 caracteres minimum)")

# Moteur ExLlamaV3, servi par TabbyAPI (API OpenAI). Ajoute le 2026-10-01 pour
# MiMo-V2.6-Flash-RL en EXL3 2,27 bpw : 85 Gio pour les 309B, la seule version
# complete de ce modele qui tienne sur le GB10 (le GGUF le plus petit fait 126 Go).
# Pas de roue aarch64 publiee : exllamav3 est compile localement dans ce venv.
EXL3_PY   = os.environ.get("EXL3_PY", "/root/venvs/exl3/bin/python")
# Dossiers de liens symboliques que le runner republie a chaque lancement (cf.
# _exl3_served_layout). Sous son HOME : c'est le seul endroit ou il peut ecrire.
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

# --- Garde-fous memoire (2026-10-01, apres une boucle de plantages) -----------
# Sur le GB10 la memoire GPU est prise dans la RAM systeme mais N'APPARAIT DANS
# AUCUN PROCESSUS ni dans aucun cgroup : un modele de 26 Go n'y compte que 2 Go
# d'anon (mesure). Le tueur OOM du noyau est donc aveugle a la vraie cause — le
# 2026-10-01 il a tue 443 processus sans rien liberer, pendant que ~119 Go restaient
# tenus par le pilote (bilan Mem-Info : 0,77 Go libre, presque rien en anon/cache),
# et la machine a gele puis redemarre deux fois. Un MemoryMax sur l'unite serait
# donc illusoire. Le seul signal fiable est MemAvailable, que la memoire GPU fait
# bien baisser : c'est lui que surveillent les garde-fous ci-dessous.
#
# Plancher pour LANCER un modele : en dessous, la memoire de l'instance precedente
# n'est pas rendue (le pilote la libere avec retard) et on s'empilerait dessus.
MIN_FREE_TO_LAUNCH_GIB = float(os.environ.get("MIN_FREE_TO_LAUNCH_GIB", "30"))
# Seuil critique PENDANT qu'un modele tourne : on tue nous-memes notre llama-server
# avant que le noyau ne parte en OOM global et n'emporte toute la machine.
CRITICAL_FREE_GIB = float(os.environ.get("CRITICAL_FREE_GIB", "2"))
# Un modele doit tenir ce temps SANS interruption pour remettre le compteur
# d'echecs a zero. Repondre en HTTP 200 ne suffit pas : dans la boucle du
# 2026-10-01 le modele repondait, le compteur repartait a zero, puis il etait
# tue une minute plus tard — MAX_AUTO_RETRIES n'etait donc jamais atteint.
STABLE_SECONDS = int(os.environ.get("RESUME_STABLE_SECONDS", "600"))
# Compteur d'echecs de reprise PERSISTE sur disque : en memoire vive il repartait
# a zero a chaque redemarrage du service (OOMPolicy=stop redemarre le runner a
# chaque OOM), ce qui rendait la boucle infinie, y compris a travers les reboots.
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
_running_since = None # instant ou le modele courant est passe "running"

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
# Chaque pseudo-flag n'est admis que pour SON moteur : proposer --vllm-028 a un
# lancement llama.cpp n'a aucun sens, et l'accepter ferait pointer llama.cpp vers
# un binaire vLLM. Le decoupage vit ici, les allow-lists s'en servent plus bas.
_LLAMA_BIN_FLAGS = {f for f in _BIN_FLAGS if f.startswith("--llama-")}
_VLLM_BIN_FLAGS  = set(_BIN_FLAGS) - _LLAMA_BIN_FLAGS
_BOOL_FLAGS |= _VLLM_BIN_FLAGS
_VALUE_FLAGS = {
    "--tool-call-parser", "--dtype", "--max-model-len",
    "--gpu-memory-utilization", "--max-num-seqs", "--kv-cache-dtype",
    "--max-num-batched-tokens", "--block-size", "--swap-space",
    "--quantization", "--tensor-parallel-size", "--pipeline-parallel-size",
    "--reasoning-parser",
    # Choix du noyau d'attention. Necessaire sur GB10 : vLLM prefere FLASHINFER,
    # mais la capability de cette carte est sm_121 et les chemins FlashInfer requis
    # sont gardes par `is_sm100a_supported()` — qui renvoie False ici. Le moteur
    # echoue alors au profilage memoire sur "FlashInfer backend is not available",
    # alors que le paquet EST installe. TRITON_ATTN est l'alternative annoncee par
    # le moteur lui-meme dans ses backends potentiels.
    "--attention-backend",
    # Valeurs par defaut des variables de template, fusionnees SOUS celles de
    # chaque requete. Sur K2-Horizon elles fixent le `reasoning_effort` par defaut
    # (high|medium|low), donc la paire de balises que le parser attend quand le
    # client n'en demande pas. Attention : cote vLLM le flag s'appelle
    # --default-chat-template-kwargs ; --chat-template-kwargs est celui de
    # llama.cpp et fait echouer `vllm serve` avec "unrecognized arguments".
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
    # KV unifié : un seul buffer partagé par les slots. Sans lui, --parallel N
    # DIVISE le contexte (262k/8 = 32k par session) ; avec, chaque session peut
    # monter jusqu'au pool complet quand les autres sont inoccupées.
    "--kv-unified", "--no-kv-unified",
    # Truncates old tokens when a slot is full instead of ERRORing (otherwise a
    # client like OpenCode retries the same over-long request → crash).
    "--context-shift", "--no-context-shift",
}
# Sans cette ligne les pseudo-flags de binaire etaient refuses par la validation
# AVANT d'atteindre _start_process : --llama-next etait donc inutilisable depuis
# toujours ("flag not allowed"), quand bien meme _build_cmd sait s'en servir.
_LLAMA_BOOL_FLAGS |= _LLAMA_BIN_FLAGS
_LLAMA_VALUE_FLAGS = {
    # 0.5.0 : `--ctx-size` dimensionne le cache KV PAR SLOT (mesure du
    # 2026-09-25 : 4 slots prives de 262k = 4 M de tokens de cache, ~124 Gio,
    # OOM, la ou l'ancien binaire tenait dans 109 Gio pour le meme service).
    # `--kv-unified-per-slot N` declare la fenetre PAR SESSION et laisse le
    # reservoir se dimensionner a n_parallel x N : c'est la facon de demander
    # 4 x 256k avec ce binaire. Le portail lit ce drapeau pour annoncer la
    # bonne limite d'entree (cf. `effective_ctx`).
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
    # Projecteur vision (multimodal). Meme regle que ci-dessus : nom de fichier
    # seul, resolu A COTE des poids (cf. _resolve_mmproj_tokens) — un projecteur
    # n'a de sens qu'avec la quantification contre laquelle il a ete produit.
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


# Modeles de chat NOMMEMENT approuves pour --trust-remote-code. Le flag reste
# absent de _BOOL_FLAGS (RCE via le code du depot HF) : ceci est une exception par
# modele, pas une ouverture. Y inscrire un modele est une decision de securite —
# lancer ce modele execute le code Python de son depot.
_TRUST_RC_MODELS = {
    # Vide : aucune exception active. K2-Horizon y a figure le 2026-09-07 puis en a
    # ete retire avec le modele — garder une approbation pour un modele qu'on ne
    # sert plus elargit la surface sans rien apporter.
}


def _repo_id_of(hf_id):
    """Depot HF correspondant a `hf_id`, qu'il soit donne par identifiant ou par
    chemin de snapshot du cache (.../models--org--nom/snapshots/<sha>).

    Sans cela, approuver "org/nom" ne reconnaitrait pas le meme modele reference
    par son chemin de cache — et l'exception echouerait de facon incomprehensible.
    """
    m = re.search(r'models--([^/]+?)--(.+?)/snapshots/', hf_id or '')
    return f"{m.group(1)}/{m.group(2)}" if m else (hf_id or '')


# ── Whitelist exllamav3 (options de TabbyAPI) ───────────────────────────────
# --host/--port/--disable-auth/--model-dir/--draft-model-dir sont poses par le
# runner. Aucun drapeau prenant un CHEMIN (--config, dossiers lora/embeddings) :
# les noms du modele et du drafter sont acceptes SEULS et resolus sous le dossier
# `local:` controle (cf. _resolve_exl3_tokens), comme --mmproj.
_EXL3_BOOL_FLAGS = set()          # TabbyAPI prend true/false en VALEUR
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

    `hf_id` ne sert qu'a --trust-remote-code : ce flag n'est accepte pour un modele
    de chat que si le depot figure dans _TRUST_RC_MODELS (l'OCR, lui, le porte deja
    dans son propre allowlist).
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


# --- Journal de démarrage sur disque ---------------------------------------
# Le tampon mémoire seul ne suffit pas à diagnostiquer un échec de démarrage :
# ~2000 lignes, et `_start_process` le vide à CHAQUE tentative, si bien qu'avec
# les 3 auto-résumes la cause racine était écrasée avant d'être lue (constat
# consigné le 2026-09-13). On double donc le flux d'un fichier PAR DÉMARRAGE.
#
# Règle absolue : ce journal ne doit JAMAIS empêcher un lancement. Toute erreur
# (disque plein, dossier absent, droits) laisse `journal = None` et le modèle
# démarre comme avant. Le runner tourne en `vllmrunner` : ce dossier lui
# appartient, il est créé hors service (aucun redémarrage nécessaire).
LOG_DIR = os.environ.get("RUNNER_LOG_DIR", "/var/lib/vllm-runner/logs")
_LOG_MAX_BYTES = 20 * 1024 * 1024
_LOG_KEEP = 12


class _Journal:
    """Fichier de démarrage borné en taille, silencieux en cas d'échec.

    Une classe plutôt qu'un descripteur nu : il faut retenir les octets déjà
    écrits pour s'arrêter à la borne sans `tell()` à chaque ligne, et cesser
    définitivement d'écrire si le disque refuse — sans jamais lever, puisque
    l'appelant est la boucle qui lit la sortie du moteur.
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
    """Ne garde que les derniers démarrages (le disque est partagé avec les poids)."""
    try:
        fichiers = [os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
                    if f.endswith(".log")]
        fichiers.sort(key=lambda p: os.stat(p).st_mtime, reverse=True)
        for vieux in fichiers[_LOG_KEEP:]:
            os.remove(vieux)
    except Exception:
        pass


def _ouvre_journal(name, engine):
    """Ouvre le journal de CE démarrage, ou None si ce n'est pas possible."""
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
    """Attend que la memoire de l'instance precedente soit rendue, puis REFUSE de
    lancer s'il reste moins de MIN_FREE_TO_LAUNCH_GIB. A appeler HORS du verrou :
    l'attente peut durer une minute et bloquerait /status."""
    _wait_mem_release()
    free = _mem_available_gib()
    if free is not None and free < MIN_FREE_TO_LAUNCH_GIB:
        _append(f"[runner] {context} REFUSE : {free:.1f} GiB disponibles seulement "
                f"(plancher {MIN_FREE_TO_LAUNCH_GIB:.0f}). La memoire d'un modele "
                f"precedent n'a pas ete rendue — lancer maintenant ferait planter la machine.")
        return False
    return True


def _memory_guard(proc):
    """Tant que NOTRE modele tourne : si MemAvailable passe sous CRITICAL_FREE_GIB,
    on le tue nous-memes. C'est la seule facon d'arriver avant le noyau, dont le
    tueur OOM ne voit pas la memoire GPU et tuerait tout le reste a la place."""
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
    """Dossier `local:` du modele + verification des noms --model-name /
    --draft-model-name : nom SEUL (ni separateur ni ".."), sous-dossier existant.
    Meme garde que _resolve_gguf : le dossier reste strictement sous MODELS_DIR."""
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
    """Publie le modele SOUS SON NOM DE CATALOGUE pour TabbyAPI.

    Le portail attend de /v1/models le nom du catalogue, et lui seul (llama.cpp le
    donne via --alias, vLLM via --served-model-name). TabbyAPI, lui, liste les
    NOMS DE DOSSIERS de --model-dir, et toutes ses requetes sont « admin » auth
    coupee : il renvoyait donc le dossier du modele ET celui du drafter. Le portail
    prenait le premier (le drafter), ne le trouvait pas au catalogue et retombait
    sur le moteur `vllm` — sante fausse, et une quinzaine d'appelants de
    get_running_models() perturbes. TabbyAPI nomme une entree d'apres le nom du
    LIEN (path.name), pas sa cible : un dossier ne contenant qu'un lien
    <catalogue> -> modele, et le drafter dans un dossier a part, suffisent.
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
        # TabbyAPI : meme API OpenAI sur :8000, rien ne change en aval. L'auth est
        # coupee comme pour les autres moteurs : :8000 n'est joignable que depuis le
        # reseau docker (vllm-restrict.service).
        base, toks = _resolve_exl3_tokens(extra_tokens, hf_id)
        mdir, ddir, toks = _exl3_served_layout(base, name, toks)
        # Deux reglages FIGES, hors de portee de vllm_args :
        #  --disable-fetch-requests : par defaut TabbyAPI va chercher lui-meme les
        #    images donnees par URL. N'importe quel appelant de l'API ferait alors
        #    interroger par la DGX des adresses internes (admin garage, Traefik,
        #    NAS) — une SSRF, ce que websearch.url_publique interdit ailleurs. Les
        #    images en base64 (celles du playground) ne passent pas par la.
        #  --allowed-origins : l'auth etant coupee, le CORS "*" par defaut laisserait
        #    toute page web ouverte sur la machine (il y a une session de bureau)
        #    lire les reponses, endpoints d'admin compris. Personne n'appelle :8000
        #    depuis un navigateur : on n'autorise qu'une origine inexistante (.invalid,
        #    TLD reserve), l'option exigeant au moins une valeur.
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
        # bin_override : pose par --llama-next (cf. _BIN_FLAGS). Sans cela le
        # drapeau serait retire de l'argv mais n'aurait AUCUN effet, le binaire
        # etant code en dur ici — panne silencieuse.
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

    # La commande est construite AVANT l'arrêt du modèle en cours (audit du
    # 2026-10-02). `_build_cmd` valide et résout (modèle local, gabarit, mmproj)
    # et peut ÉCHOUER : l'ordre précédent tuait donc le modèle SERVI avant de
    # découvrir que la relance était impossible — une faute de saisie laissait la
    # plateforme sans aucun modèle, alors que le runner n'en sert qu'un à la fois.
    # Ici l'échec remonte avant tout arrêt et le service reste tel quel.
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
    # Journal sur disque du démarrage : c'est le SEUL endroit où survivra la
    # cause racine d'un échec, puisque ce clear() efface le tampon mémoire à
    # chaque tentative (y compris à chaque auto-résume).
    journal = _ouvre_journal(name, engine)
    if journal is not None:
        journal.ecrire(f"[runner] {time.strftime('%Y-%m-%dT%H:%M:%S')} démarrage "
                       f"{engine} — {name} ({hf_id})")

    # The previous model was just killed: wait for the driver to release the
    # unified memory, otherwise the new process OOMs at startup.
    if killed:
        _wait_mem_release()

    # Ici on ne fait que JOURNALISER la commande déjà construite plus haut (avant
    # l'arrêt du modèle précédent) : la reconstruire ici après le kill était
    # exactement le défaut corrigé le 2026-10-02.
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
    # `_ABS_PATH_RE` autorise le POINT, donc `/models/../../etc/shadow` passait tel
    # quel : le chemin part en argv de l'engine (`-m`), qui lit le fichier. Le
    # runner ne l'ouvre pas lui-même, mais rien ne justifie d'accepter `..` dans un
    # chemin qu'on prétend « absolu et borné » (audit du 2026-10-02).
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
    # Même contrôle de FORME que /launch (audit du 2026-10-02) : sans lui, un
    # `"hf_model_id": "--model=/chemin/local"` traversait jusqu'à `vllm serve`, qui
    # l'interprète comme une OPTION — l'appelant pilotait donc l'argv du conteneur
    # OCR au lieu d'un identifiant de dépôt, et le contrôle de forme de
    # `_validate_vllm_args` se contournait par l'argument lui-même. Le catalogue du
    # portail ne produit que des `org/name` (déjà validés par cette expression).
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
# GGUF local ("local:<name>", ds4/llamacpp) — même charset, sans slash requis.
_LOCAL_ID_RE = re.compile(r"^local:[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
# Chemin local absolu vers une snapshot HF déjà téléchargée (engine vllm) —
# p.ex. /root/.cache/huggingface/hub/models--…/snapshots/<sha>. Accepté car
# vllm reçoit l'id verbatim en argv (jamais en shell) ; le risque est un file
# read local, pas de l'exécution, et /launch est déjà derrière auth admin.
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
        # Un modele qui tient STABLE_SECONDS sans interruption remet le compteur
        # d'echecs a zero — et seulement lui (cf. STABLE_SECONDS).
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
        # Attente/plancher memoire HORS du verrou. Un refus compte comme un echec :
        # c'est ce qui finit par arreter une boucle au lieu de la nourrir.
        if not _launch_memory_ok("auto-resume"):
            _write_resume_fails(fails + 1)
            continue
        with _lock:
            if _proc is not None and _proc.poll() is None:
                continue          # lance entre-temps (Admin) : rien a faire
            _write_resume_fails(fails + 1)
            _auto_retries = fails + 1
            attempt_msg = (f"[runner] model stopped unexpectedly — auto-resume "
                           f"(attempt {fails + 1}/{MAX_AUTO_RETRIES})…")
            _start_process(last["hf_model_id"], last["model_name"], extra_tokens, eng)
            _append(attempt_msg)  # after _start_process (which clears _logs) so it survives


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()

    # La reprise au demarrage est confiee au watchdog (premier passage ~10 s apres).
    # Elle relancait le modele IMMEDIATEMENT, sans attendre que le pilote rende la
    # memoire GPU de l'instance tuee : apres un OOM, le nouveau modele s'empilait
    # dessus et replantait — boucle infinie, y compris a travers les reboots. Le
    # watchdog applique, lui, l'attente memoire, le plancher et le compteur persiste.
    # Le faire ici AUSSI ouvrirait une course : l'attente memoire peut durer une
    # minute, pendant laquelle le watchdog lancerait une seconde instance.
    if _load_last_launch():
        _append("[runner] service started — the watchdog will resume the last model "
                "once memory is confirmed free")

    app.run(host="0.0.0.0", port=8001, debug=False, threaded=True)
