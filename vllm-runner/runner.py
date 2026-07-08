"""
vLLM Runner — daemon HTTP local sur le port 8001.
Gère un seul processus vLLM à la fois (avec ses enfants).
"""
import hmac, json, os, shutil, signal, subprocess, threading, time, urllib.request
from flask import Flask, jsonify, request, Response

VLLM_BIN     = os.environ.get("VLLM_BIN", "/root/.local/bin/vllm")
HF_HOME      = os.environ.get("HF_HOME", "/root/.cache/huggingface")
RUNNER_TOKEN = os.environ["RUNNER_TOKEN"]  # requis — pas de défaut, le service doit échouer au démarrage si absent

# Persiste le dernier lancement réussi pour pouvoir le reprendre automatiquement
# après un redémarrage du service (mise à jour système, reboot, crash) — sauf
# arrêt volontaire via /stop, qui efface ce fichier.
STATE_FILE = os.path.join(os.environ.get("HOME", "/var/lib/vllm-runner"), "last_model.json")
MAX_AUTO_RETRIES = 3

app = Flask(__name__)

_lock   = threading.Lock()
_proc   = None
_model  = None
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
}
_VALUE_FLAGS = {
    "--tool-call-parser", "--dtype", "--max-model-len",
    "--gpu-memory-utilization", "--max-num-seqs", "--kv-cache-dtype",
    "--max-num-batched-tokens", "--block-size", "--swap-space",
    "--quantization", "--tensor-parallel-size", "--pipeline-parallel-size",
    "--reasoning-parser", "--limit-mm-per-prompt",
    "--uvicorn-log-level",
}


def _validate_vllm_args(extra):
    """Retourne (ok, tokens_ou_message_erreur)."""
    tokens = extra.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _BOOL_FLAGS:
            i += 1
        elif tok in _VALUE_FLAGS:
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


def _save_last_launch(hf_id, name, extra_tokens):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"hf_model_id": hf_id, "model_name": name, "vllm_args": " ".join(extra_tokens)}, f)
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
    return jsonify({"status": _status, "model": _model, "pid": _proc.pid if _proc else None})


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


def _start_process(hf_id, name, extra_tokens):
    """Lance vLLM. Doit être appelé avec _lock déjà tenu."""
    global _proc, _model, _status

    if _proc and _proc.poll() is None:
        _append("[runner] Arrêt du modèle précédent…")
        _kill(_proc)

    _logs.clear()
    _model  = name
    _status = "starting"

    cmd = [VLLM_BIN, "serve", hf_id,
           "--port", "8000", "--host", "0.0.0.0",
           "--served-model-name", name,
           ] + extra_tokens

    _append(f"[runner] $ {' '.join(cmd)}")

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
    }
    if os.environ.get("HF_TOKEN"):
        env["HF_TOKEN"] = os.environ["HF_TOKEN"]

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
    _save_last_launch(hf_id, name, extra_tokens)
    return _proc


@app.route("/launch", methods=["POST"])
def launch():
    global _auto_retries
    data     = request.get_json(silent=True) or {}
    hf_id    = data.get("hf_model_id", "").strip()
    name     = data.get("model_name", hf_id).strip()
    extra    = data.get("vllm_args", "").strip()

    if not hf_id:
        return jsonify({"error": "hf_model_id requis"}), 400

    ok, result = _validate_vllm_args(extra)
    if not ok:
        return jsonify({"error": result}), 400
    extra_tokens = result

    with _lock:
        proc = _start_process(hf_id, name, extra_tokens)
        _auto_retries = 0

    return jsonify({"status": "starting", "model": name, "pid": proc.pid})


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
            ok, extra_tokens = _validate_vllm_args(last.get("vllm_args", ""))
            if not ok:
                _append(f"[runner] reprise auto impossible, args invalides : {extra_tokens}")
                _auto_retries = MAX_AUTO_RETRIES
                continue
            _auto_retries += 1
            attempt_msg = f"[runner] modèle arrêté de façon inattendue — reprise automatique (tentative {_auto_retries}/{MAX_AUTO_RETRIES})…"
            _start_process(last["hf_model_id"], last["model_name"], extra_tokens)
            _append(attempt_msg)  # après _start_process (qui vide _logs) pour qu'il survive


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()

    _resume = _load_last_launch()
    if _resume:
        ok, extra_tokens = _validate_vllm_args(_resume.get("vllm_args", ""))
        if ok:
            with _lock:
                _append("[runner] reprise du dernier modèle au démarrage du service…")
                _start_process(_resume["hf_model_id"], _resume["model_name"], extra_tokens)

    app.run(host="0.0.0.0", port=8001, debug=False, threaded=True)
