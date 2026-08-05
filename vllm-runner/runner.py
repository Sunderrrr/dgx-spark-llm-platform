"""
Model Runner — daemon HTTP local sur le port 8001.
Gère un seul processus d'inférence à la fois (avec ses enfants), au choix :
  - vLLM      : poids safetensors (NVFP4 / FP8 / BF16)
  - llama.cpp : poids GGUF (llama-server, API OpenAI-compatible sur le même port)
Dans les deux cas le modèle est servi sur :8000 → le routage LiteLLM est identique.
"""
import hmac, json, os, re, shutil, signal, subprocess, threading, time, urllib.request
from flask import Flask, jsonify, request, Response

VLLM_BIN     = os.environ.get("VLLM_BIN", "/root/.local/bin/vllm")
# Venv séparé (vLLM 0.25.1 + FlashInfer nightly) pour les modèles qui exigent
# une version de vLLM plus récente que celle installée globalement — évite de
# faire une montée de version majeure qui casserait les modèles existants
# (nemotron/minimax/ornith tournent sur VLLM_BIN, testés et stables dessus).
# Activé par le pseudo-flag --vllm-025 dans vllm_args (voir _BIN_FLAGS).
VLLM_BIN_025 = os.environ.get("VLLM_BIN_025", "/root/venvs/vllm025/bin/vllm")
LLAMA_BIN    = os.environ.get("LLAMA_BIN", "/root/llama.cpp/build/bin/llama-server")
# Moteur ds4 : GGUF NVFP4 « multi-tenseurs » spécifique DGX Spark (DeepSeek-V4-Flash).
# Ni vLLM ni llama.cpp standard ne savent charger ce format.
DS4_BIN      = os.environ.get("DS4_BIN", "/root/ds4-nvfp4-spark/ds4-server")
HF_HOME      = os.environ.get("HF_HOME", "/root/.cache/huggingface")
# Répertoire de poids téléchargés hors du Hub (ex. HF throttle les gros GGUF en
# non-authentifié). Un modèle y est référencé par « local:<nom> » — le nom est
# assaini, donc pas de chemin arbitraire ni de traversée de répertoire.
MODELS_DIR   = os.environ.get("MODELS_DIR", "/root/models")
# Templates de chat corrigés (ex. neutraliser l'alternance stricte des modèles
# Mistral qui casse en usage agentique). Référencés par nom seul → pas de chemin
# arbitraire.
TEMPLATES_DIR = os.environ.get("TEMPLATES_DIR", "/root/models/templates")
RUNNER_TOKEN = os.environ["RUNNER_TOKEN"]  # requis — pas de défaut, le service doit échouer au démarrage si absent

ENGINES = ("vllm", "llamacpp", "ds4")
_ENGINE_BIN = {"vllm": VLLM_BIN, "llamacpp": LLAMA_BIN, "ds4": DS4_BIN}

# Persiste le dernier lancement réussi pour pouvoir le reprendre automatiquement
# après un redémarrage du service (mise à jour système, reboot, crash) — sauf
# arrêt volontaire via /stop, qui efface ce fichier.
STATE_FILE = os.path.join(os.environ.get("HOME", "/var/lib/vllm-runner"), "last_model.json")
MAX_AUTO_RETRIES = 3

app = Flask(__name__)

_lock   = threading.Lock()
_proc   = None
_model  = None
_engine = None        # moteur du modèle courant : 'vllm' | 'llamacpp'
_logs   = []
_status = "stopped"   # stopped | starting | running | error
_auto_retries = 0     # tentatives de relance automatique consécutives échouées

# ── Auth ─────────────────────────────────────────────────────────────────
# Toutes les routes nécessitent "Authorization: Bearer <RUNNER_TOKEN>".
# Cette API pilote un process root et lance des modèles arbitraires : elle ne doit
# jamais être appelable sans preuve que l'appelant est bien dgx-portal.
@app.before_request
def _check_auth():
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    token  = header[len(prefix):] if header.startswith(prefix) else ""
    if not hmac.compare_digest(token, RUNNER_TOKEN):
        return jsonify({"error": "unauthorized"}), 401


# ── Whitelist des flags vLLM autorisés dans vllm_args ──────────────────────
# Allowlist stricte (pas denylist) : tout flag non listé est refusé.
# Volontairement absents : --trust-remote-code (RCE via code du repo HF),
# --download-dir / --chat-template / --tokenizer (lecture fichier arbitraire /
# SSTI Jinja2), --model / --host / --port / --served-model-name / --api-key
# (déjà fixés par le runner, ne doivent pas être écrasables).
_BOOL_FLAGS = {
    "--enable-auto-tool-choice", "--enforce-eager",
    "--disable-log-requests", "--disable-log-stats",
    "--skip-mm-profiling",
    # KAT-Coder-V2.5 (et autres releases Qwen3.5-MoE "text-only") : sans ce
    # flag vLLM résout un config multimodal (Qwen3_5MoeConfig) au lieu du
    # config texte (Qwen3_5MoeTextConfig) que ce poids attend réellement →
    # TypeError au chargement. Obligatoire d'après la carte du modèle.
    "--language-model-only",
}

# Pseudo-flags : ne sont PAS passés au moteur, ils positionnent une variable
# d'environnement pour CE modèle uniquement. Allowlist fermée → pas d'injection
# d'env arbitraire. Utile quand un modèle exige un chemin kernel particulier
# qu'on ne veut surtout pas imposer globalement aux autres modèles.
_ENV_FLAGS = {
    # MiniMax-M2 (quant mixte NVFP4+FP8) : vLLM ne trouve pas de kernel FP8
    # ScaledMM sur GB10 et demande explicitement ce fallback Marlin.
    "--force-fp8-marlin": ("VLLM_TEST_FORCE_FP8_MARLIN", "1"),
    # Laguna S 2.1 (NVFP4 natif via FlashInfer) : chaîne d'architecture requise
    # par le JIT des kernels FP4 sur GB10 (recette officielle poolside).
    "--cute-dsl-arch-sm121a": ("CUTE_DSL_ARCH", "sm_121a"),
}
_BOOL_FLAGS |= set(_ENV_FLAGS)

# Pseudo-flag séparé (pas un simple env var) : bascule le binaire vLLM utilisé
# pour CE lancement uniquement, sans toucher à VLLM_BIN (donc sans risque pour
# les autres modèles vllm). Retiré de extra_tokens dans _start_process, comme
# les entrées de _ENV_FLAGS.
_BIN_FLAGS = {
    "--vllm-025": VLLM_BIN_025,
}
_BOOL_FLAGS |= set(_BIN_FLAGS)
_VALUE_FLAGS = {
    "--tool-call-parser", "--dtype", "--max-model-len",
    "--gpu-memory-utilization", "--max-num-seqs", "--kv-cache-dtype",
    "--max-num-batched-tokens", "--block-size", "--swap-space",
    "--quantization", "--tensor-parallel-size", "--pipeline-parallel-size",
    "--reasoning-parser", "--limit-mm-per-prompt",
    "--uvicorn-log-level",
    # Valeur énumérée (auto|slow|mistral|custom), jamais un chemin → sans risque.
    # Nécessaire pour les modèles Mistral (tekken) : l'auto-détection de vLLM 0.24
    # tombe sur un backend cassé ("CachedMistralCommonBackend has no attribute
    # is_fast"), alors que --tokenizer-mode mistral fonctionne.
    "--tokenizer-mode",
}

# ── Whitelist OCR (conteneur docker dédié, PAS le process hôte principal) ──
# Plus permissive que _BOOL_FLAGS/_VALUE_FLAGS : --trust-remote-code et
# --logits_processors sont nécessaires à Unlimited-OCR (processeur de logits
# custom du repo) et probablement à d'autres VLM OCR. Risque RCE réel si
# l'admin pointe vers un repo HF malveillant — accepté ici : (1) admin-only,
# même niveau de confiance que le catalogue de chat principal, qui contrôle
# déjà entièrement ce qui tourne côté hôte ; (2) ce conteneur est isolé
# (réseau docker dédié, pas de docker.sock, pas d'accès aux autres services).
_OCR_BOOL_FLAGS = _BOOL_FLAGS | {"--trust-remote-code", "--no-enable-prefix-caching"}
_OCR_VALUE_FLAGS = _VALUE_FLAGS | {"--logits_processors", "--mm-processor-cache-gb"}

# ── Whitelist des flags llama.cpp (llama-server) ───────────────────────────
# Même principe : allowlist stricte. Volontairement absents :
# --model / --hf-repo / --host / --port / --alias (fixés par le runner),
# --chat-template-file & --grammar-file & --lora (lecture de fichier arbitraire),
# --chat-template (accepte un template Jinja complet → surface d'injection).
_LLAMA_BOOL_FLAGS = {
    "--no-mmap", "--mlock", "--jinja", "--cont-batching",
    "--no-kv-offload", "--metrics", "--no-warmup",
    # Tronque les vieux tokens quand un slot est plein au lieu d'ERREUR (sinon un
    # client comme OpenCode réessaie la même requête trop longue → crash).
    "--context-shift", "--no-context-shift",
}
_LLAMA_VALUE_FLAGS = {
    "--ctx-size", "--n-gpu-layers", "--parallel", "--threads", "--threads-batch",
    "--batch-size", "--ubatch-size", "--cache-type-k", "--cache-type-v",
    "--n-predict", "--rope-scaling", "--rope-freq-base", "--rope-freq-scale",
    "--split-mode", "--main-gpu", "--seed", "--defrag-thold", "--log-verbosity",
    "--reasoning-format", "--chat-template-kwargs",
    # Attention : --flash-attn prend une VALEUR (on|off|auto) dans llama.cpp
    # récent — le traiter comme un booléen lui fait avaler le flag suivant.
    "--flash-attn",
    # Valeur = nom de fichier seul, résolu sous TEMPLATES_DIR (pas de chemin
    # arbitraire, cf. _resolve_template) → sert à corriger un template embarqué.
    "--chat-template-file",
}


# ── Whitelist des flags ds4 (ds4-server) ───────────────────────────────────
# -m / --host / --port sont fixés par le runner. Pas de flag prenant un chemin
# (--kv-disk-dir, --dir-steering-file) → aucune lecture/écriture arbitraire.
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
    """Résout un GGUF local (les moteurs ds4/llama.cpp veulent un fichier via -m).

    Deux sources, toutes deux SOUS UN RÉPERTOIRE CONTRÔLÉ — jamais un chemin
    arbitraire venant de l'API :
      - "local:<nom>"  → MODELS_DIR/<nom>/  (nom assaini)
      - "user/repo"    → cache HF du repo
    Retourne le 1er shard s'il est éclaté, sinon le plus gros .gguf.
    """
    if hf_id.startswith("local:"):
        slug = re.sub(r'[^A-Za-z0-9._-]', '', hf_id[len("local:"):])
        snaps = os.path.join(MODELS_DIR, slug)
        if not slug or not os.path.isdir(snaps):
            raise FileNotFoundError(f"modèle local « {slug} » introuvable dans {MODELS_DIR}")
    else:
        snaps = os.path.join(HF_HOME, "hub",
                             "models--" + hf_id.replace("/", "--"), "snapshots")
        if not os.path.isdir(snaps):
            raise FileNotFoundError(f"modèle {hf_id} absent du cache HF — télécharge-le d'abord")
    candidates = []
    for root, _dirs, files in os.walk(snaps):
        for f in files:
            if f.endswith(".gguf") and "mmproj" not in f and "imatrix" not in f:
                p = os.path.join(root, f)
                candidates.append((os.path.getsize(os.path.realpath(p)), f, p))
    if not candidates:
        raise FileNotFoundError(f"aucun .gguf trouvé pour {hf_id}")
    # Modèle éclaté en shards → toujours pointer le premier (00001-of-000NN),
    # le moteur charge les suivants tout seul.
    shards = sorted(p for _s, f, p in candidates if "-00001-of-" in f)
    if shards:
        return shards[0]
    return max(candidates)[2]


def _validate_vllm_args(extra, engine="vllm"):
    """Retourne (ok, tokens_ou_message_erreur). L'allowlist dépend du moteur."""
    bool_flags, value_flags = _flags_for(engine)
    tokens = extra.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in bool_flags:
            i += 1
        elif tok in value_flags:
            if i + 1 >= len(tokens) or tokens[i + 1].startswith("--"):
                return False, f"le flag {tok} nécessite une valeur"
            i += 2
        else:
            return False, f"flag non autorisé : {tok}"
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
        _append(f"[runner] impossible d'enregistrer l'état pour la reprise auto : {e}")


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
        _append(f"[runner] impossible d'effacer l'état de reprise auto : {e}")


def _kill(proc):
    """Tue le process ET tous ses enfants (process group)."""
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
    """Mémoire disponible (GiB). Sur GB10 la mémoire est UNIFIÉE (GPU + CPU) :
    /proc/meminfo est donc le bon indicateur — nvidia-smi ne rapporte pas la
    mémoire sur cette carte intégrée."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return None


def _wait_mem_release(timeout=60, settle=4.0):
    """Attend que la mémoire du modèle précédent soit réellement rendue.

    Le driver ne récupère pas la mémoire unifiée instantanément à la mort du
    process : spawner le nouveau vLLM trop tôt le fait échouer sur un OOM GPU
    (NVRM: NV_ERR_NO_MEMORY) — le modèle crashe puis part en auto-retry. On
    attend donc que MemAvailable cesse de remonter (plateau) avant de relancer.
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
        if cur - prev < 0.5:      # plus de libération notable
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0            # ça libère encore, on continue d'attendre
        prev = cur
    free = _mem_available_gib()
    _append(f"[runner] Mémoire rendue : {free:.1f} GiB dispo "
            f"(attente {time.time() - start:.0f}s) — relance du modèle")
    time.sleep(settle)            # petite marge pour le driver


def _reader(proc):
    global _status, _proc, _model, _auto_retries
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            _append(line)
            # vLLM est prêt quand il imprime "Application startup complete"
            # (ne touche au statut global que si ce process est toujours le process actif —
            # sinon un ancien reader thread, encore en train de drainer un process tué par
            # /launch, peut écraser le statut du NOUVEAU process en cours de démarrage)
            if "Application startup complete" in line and proc is _proc:
                _status = "running"
                _auto_retries = 0  # ce lancement a fonctionné, on repart avec un budget de retry frais
    except Exception as e:
        _append(f"[runner] lecture interrompue : {e}")
    proc.wait()
    with _lock:
        if proc is _proc and _status != "stopped":
            _status = "error" if proc.returncode not in (0, -15, -9) else "stopped"
        _append(f"[runner] Processus terminé (code {proc.returncode})")


def _health_watch(proc):
    """Bascule le statut en 'running' dès que vLLM répond réellement, sans dépendre
    des logs : --uvicorn-log-level warning masque « Application startup complete »,
    ce qui laissait le statut coincé sur 'starting' alors que le modèle servait."""
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
    """SSE — pousse les nouvelles lignes de log en temps réel."""
    def generate():
        # Envoie tous les logs existants d'un coup
        with _lock:
            snapshot = list(_logs)
        last = len(snapshot)
        for line in snapshot:
            yield f"data: {line}\n\n"

        while True:
            time.sleep(0.05)   # 50 ms → quasi temps réel
            with _lock:
                current_len = len(_logs)
                if current_len < last:
                    # _logs.clear() appelé par /launch → nouveau démarrage
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
                    yield ": ping\n\n"   # keep-alive (toutes les 50 ms)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return Response(generate(), mimetype="text/event-stream", headers=headers)


def _resolve_template_tokens(tokens):
    """Remplace la valeur de --chat-template-file (nom de fichier seul) par le
    chemin absolu sous TEMPLATES_DIR. Rejette tout ce qui contient un séparateur
    ou « .. » → impossible de lire un fichier hors du répertoire contrôlé."""
    out = list(tokens)
    for i, t in enumerate(out):
        if t == "--chat-template-file" and i + 1 < len(out):
            raw = out[i + 1]
            if "/" in raw or "\\" in raw or ".." in raw:
                raise ValueError("nom de template invalide")
            path = os.path.join(TEMPLATES_DIR, raw)
            if not os.path.isfile(path):
                raise FileNotFoundError(f"template « {raw} » introuvable dans {TEMPLATES_DIR}")
            out[i + 1] = path
    return out


def _build_cmd(hf_id, name, extra_tokens, engine, vllm_bin=None):
    """Ligne de commande du moteur. Tous servent une API OpenAI sur :8000,
    donc rien ne change en aval (LiteLLM, portail, playground)."""
    if engine == "ds4":
        # ds4-server prend un GGUF local ; --cuda est requis pour le GPU.
        cmd = [DS4_BIN, "-m", _resolve_gguf(hf_id),
               "--host", "0.0.0.0", "--port", "8000"] + extra_tokens
        if "--cpu" not in extra_tokens and "--cuda" not in extra_tokens:
            cmd.insert(1, "--cuda")
        return cmd
    if engine == "llamacpp":
        # "local:<nom>" → poids déjà sur disque, on pointe le fichier (-m).
        # Sinon -hf accepte "user/repo[:QUANT]" et llama.cpp télécharge lui-même.
        # --metrics expose /metrics (Prometheus) comme vLLM, pour le panneau santé.
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
    """Lance le moteur d'inférence. Doit être appelé avec _lock déjà tenu."""
    global _proc, _model, _status, _engine

    killed = bool(_proc and _proc.poll() is None)
    if killed:
        _append("[runner] Arrêt du modèle précédent…")
        _kill(_proc)

    _logs.clear()
    _model  = name
    _engine = engine
    _status = "starting"

    # Le modèle précédent vient d'être tué : on attend que le driver rende la
    # mémoire unifiée, sinon le nouveau process OOM au démarrage.
    if killed:
        _wait_mem_release()

    # Conservés pour la persistance (reprise auto) : les pseudo-flags (ex.
    # --vllm-025) sont retirés d'extra_tokens juste en dessous pour l'exécution,
    # mais une reprise auto qui les perdrait relancerait sur le mauvais binaire/
    # sans le contournement nécessaire — donc on sauvegarde la liste complète.
    original_tokens = list(extra_tokens)

    # Les pseudo-flags deviennent des variables d'env propres à ce modèle et
    # sont retirés de l'argv (le moteur ne les connaît pas).
    model_env = {}
    for flag, (var, val) in _ENV_FLAGS.items():
        if flag in extra_tokens:
            extra_tokens = [t for t in extra_tokens if t != flag]
            model_env[var] = val

    # --vllm-025 (voir _BIN_FLAGS) : bascule sur le venv vLLM 0.25.1 séparé
    # pour ce lancement, sans toucher au binaire par défaut des autres modèles.
    vllm_bin = None
    for flag, bin_path in _BIN_FLAGS.items():
        if flag in extra_tokens:
            extra_tokens = [t for t in extra_tokens if t != flag]
            vllm_bin = bin_path

    cmd = _build_cmd(hf_id, name, extra_tokens, engine, vllm_bin=vllm_bin)
    _append(f"[runner] ({engine}) $ {' '.join(cmd)}")
    if model_env:
        _append(f"[runner] env spécifique au modèle : {model_env}")

    # Env minimal explicite plutôt que **os.environ — évite de faire fuiter
    # l'environnement root complet (secrets divers) dans /logs et /stream.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "HF_HOME": HF_HOME,
        "PYTHONUNBUFFERED": "1",
        # DeepGEMM E8M0 casse le FP8 MoE sur Blackwell/GB10 ("Unknown SF
        # transformation") et dégrade la précision (vLLM l'auto-désactive
        # partiellement) → on le coupe complètement, fallback CUTLASS.
        "VLLM_USE_DEEP_GEMM": "0",
        # FlashInfer compile ses kernels NVFP4 en JIT au démarrage. Sans MAX_JOBS
        # il ne passe pas de -j à ninja, qui lance ~nproc+2 compilateurs `cicc` de
        # ~3 Go chacun — par-dessus les poids déjà chargés, ça déclenche l'OOM
        # killer et le modèle meurt à l'init (constaté sur Leanstral et Nemotron).
        # 4 jobs ≈ 12 Go de pic : compilation un peu plus lente, mais une seule
        # fois (les kernels sont ensuite mis en cache).
        "MAX_JOBS": os.environ.get("MAX_JOBS", "4"),
    }
    env.update(model_env)
    if os.environ.get("HF_TOKEN"):
        env["HF_TOKEN"] = os.environ["HF_TOKEN"]
    if engine == "ds4":
        # KV cache packé en FP8 → ~7 GiB économisés à 1M de contexte (cf. carte du modèle).
        env["DS4_KV_TURBO"] = "1"

    _proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,   # nouveau process group → killpg fonctionne
    )
    threading.Thread(target=_reader, args=(_proc,), daemon=True).start()
    threading.Thread(target=_health_watch, args=(_proc,), daemon=True).start()
    # Persiste systématiquement l'état (manuel, reprise au boot, watchdog) pour
    # que last_model.json reste toujours présent tant que le modèle doit tourner.
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
        return jsonify({"error": "hf_model_id requis"}), 400
    if engine not in ENGINES:
        return jsonify({"error": f"moteur inconnu : {engine}"}), 400
    if engine != "vllm" and not os.path.exists(_ENGINE_BIN[engine]):
        return jsonify({"error": f"moteur {engine} non installé sur cette machine"}), 400

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
    _clear_last_launch()  # arrêt volontaire : ne pas reprendre tout seul
    with _lock:
        if _proc and _proc.poll() is None:
            _append("[runner] Arrêt demandé.")
            _kill(_proc)
            _status = "stopped"
            _model  = None
            return jsonify({"status": "stopped"})
    return jsonify({"status": "already_stopped"})


# ── OCR (conteneur docker) / Vidéo (service systemd ComfyUI) ────────────────
# Deux services annexes, toujours actifs à côté du modèle de chat principal
# (pas de start/stop de la RAM/VRAM partagée en jeu ici, juste start/stop du
# service lui-même). Commandes fixes, sans aucun argument piloté par
# l'appelant → autorisées via sudoers NOPASSWD scoped (voir
# /etc/sudoers.d/vllmrunner-services), pas de docker.sock ni d'accès systemd
# général.
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
    """Recrée le conteneur OCR avec un autre modèle HF. hf_model_id est
    utilisé tel quel (comme _build_cmd pour le modèle principal — argv de
    liste, jamais interprété par un shell, donc pas d'injection possible même
    si la valeur est malformée) ; vllm_args passe par la même allowlist que
    les autres moteurs (voir _OCR_BOOL_FLAGS/_OCR_VALUE_FLAGS)."""
    data = request.get_json(silent=True) or {}
    hf_id = (data.get("hf_model_id") or "").strip()
    if not hf_id:
        return jsonify({"ok": False, "detail": "hf_model_id manquant"}), 400
    ok, tokens_or_err = _validate_vllm_args(data.get("vllm_args", "") or "", engine="ocr")
    if not ok:
        return jsonify({"ok": False, "detail": tokens_or_err}), 400
    ok, out = _sudo("/usr/local/sbin/ocr-recreate.sh", hf_id, *tokens_or_err, timeout=120)
    return jsonify({"ok": ok, "detail": out})


# Chatterbox n'a que ces trois variantes possibles (cf. model.repo_id dans
# leur config.yaml) — liste blanche fermée, pas un pattern de flags comme
# _validate_vllm_args : voice-recreate.sh fait à nouveau confiance à cette
# validation amont mais revalide aussi lui-même (défense en profondeur).
_VOICE_REPO_IDS = {"chatterbox", "chatterbox-turbo", "chatterbox-multilingual"}
# Second moteur voix (Qwen3-TTS, Apache 2.0). Même conteneur « voice » et même
# port : un seul backend voix à la fois, la mémoire unifiée du GB10 étant déjà
# partagée avec le chat, l'OCR et la vidéo. Liste blanche fermée là aussi.
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
    """Recrée le conteneur voix avec une des trois variantes Chatterbox.
    repo_id vient d'une liste blanche fermée (pas d'argv libre comme pour
    OCR/vLLM) : aucune construction de commande à valider ici, juste une
    appartenance à _VOICE_REPO_IDS."""
    data = request.get_json(silent=True) or {}
    repo_id = (data.get("repo_id") or "").strip()
    if repo_id in _VOICE_REPO_IDS:
        script = "/usr/local/sbin/voice-recreate.sh"
    elif repo_id in _VOICE_QWEN_IDS:
        script = "/usr/local/sbin/voice-qwen-recreate.sh"
    else:
        return jsonify({"ok": False, "detail": "repo_id invalide"}), 400
    ok, out = _sudo(script, repo_id, timeout=120)
    return jsonify({"ok": ok, "detail": out})


# Transcription (dictée). Même liste blanche fermée que la voix.
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
        return jsonify({"ok": False, "detail": "model_id invalide"}), 400
    ok, out = _sudo("/usr/local/sbin/asr-recreate.sh", model, timeout=120)
    return jsonify({"ok": ok, "detail": out})


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


# ── Métriques système (hôte) ─────────────────────────────────────────────────
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
    """Reprend automatiquement le dernier modèle lancé s'il s'arrête de façon
    inattendue (crash, update système, reboot) — pas après un /stop volontaire,
    qui efface l'état persisté. Limité à MAX_AUTO_RETRIES tentatives consécutives
    pour ne pas boucler indéfiniment sur une config cassée."""
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
                _append(f"[runner] reprise auto impossible, args invalides : {extra_tokens}")
                _auto_retries = MAX_AUTO_RETRIES
                continue
            _auto_retries += 1
            attempt_msg = f"[runner] modèle arrêté de façon inattendue — reprise automatique (tentative {_auto_retries}/{MAX_AUTO_RETRIES})…"
            _start_process(last["hf_model_id"], last["model_name"], extra_tokens, eng)
            _append(attempt_msg)  # après _start_process (qui vide _logs) pour qu'il survive


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()

    _resume = _load_last_launch()
    if _resume:
        _eng = _resume.get("engine", "vllm")
        ok, extra_tokens = _validate_vllm_args(_resume.get("vllm_args", ""), _eng)
        if ok:
            with _lock:
                _append("[runner] reprise du dernier modèle au démarrage du service…")
                _start_process(_resume["hf_model_id"], _resume["model_name"], extra_tokens, _eng)

    app.run(host="0.0.0.0", port=8001, debug=False, threaded=True)
