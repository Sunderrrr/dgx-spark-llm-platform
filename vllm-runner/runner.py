"""
vLLM Runner — daemon HTTP local sur le port 8001.
Gère un seul processus vLLM à la fois (avec ses enfants).
"""
import hmac, os, signal, subprocess, threading, time
from flask import Flask, jsonify, request, Response

VLLM_BIN     = os.environ.get("VLLM_BIN", "/root/.local/bin/vllm")
HF_HOME      = os.environ.get("HF_HOME", "/root/.cache/huggingface")
RUNNER_TOKEN = os.environ["RUNNER_TOKEN"]  # requis — pas de défaut, le service doit échouer au démarrage si absent

app = Flask(__name__)

_lock   = threading.Lock()
_proc   = None
_model  = None
_logs   = []
_status = "stopped"   # stopped | starting | running | error

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
}
_VALUE_FLAGS = {
    "--tool-call-parser", "--dtype", "--max-model-len",
    "--gpu-memory-utilization", "--max-num-seqs", "--kv-cache-dtype",
    "--max-num-batched-tokens", "--block-size", "--swap-space",
    "--quantization", "--tensor-parallel-size", "--pipeline-parallel-size",
    "--reasoning-parser",
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
    global _status, _proc, _model
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
    except Exception as e:
        _append(f"[runner] lecture interrompue : {e}")
    proc.wait()
    with _lock:
        if proc is _proc and _status != "stopped":
            _status = "error" if proc.returncode not in (0, -15, -9) else "stopped"
        _append(f"[runner] Processus terminé (code {proc.returncode})")


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


@app.route("/launch", methods=["POST"])
def launch():
    global _proc, _model, _logs, _status
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
    return jsonify({"status": "starting", "model": name, "pid": _proc.pid})


@app.route("/stop", methods=["POST"])
def stop():
    global _proc, _model, _status
    with _lock:
        if _proc and _proc.poll() is None:
            _append("[runner] Arrêt demandé.")
            _kill(_proc)
            _status = "stopped"
            _model  = None
            return jsonify({"status": "stopped"})
    return jsonify({"status": "already_stopped"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=False, threaded=True)
