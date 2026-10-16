"""Side services of the runner: OCR and video (fixed systemd/docker commands),
voice, ASR and image (on-demand sidecar containers), and their logs.

Mounted on the runner's Flask app as a blueprint (see runner.py). Each route
runs a fixed command through the scoped sudo rules; nothing from the caller
reaches a shell. The main chat model stays in runner.py (process lifecycle).
"""
import json, re, subprocess

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("sidecars", __name__)

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


@bp.route("/ocr/status")
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


@bp.route("/ocr/start", methods=["POST"])
def ocr_start():
    ok, out = _sudo("/usr/bin/docker", "start", "ocr", timeout=60)
    return jsonify({"ok": ok, "detail": out})


@bp.route("/ocr/stop", methods=["POST"])
def ocr_stop():
    ok, out = _sudo("/usr/bin/docker", "stop", "ocr", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@bp.route("/ocr/launch", methods=["POST"])
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
    ok, tokens_or_err = current_app.config["VALIDATE_VLLM_ARGS"](data.get("vllm_args", "") or "", engine="ocr")
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
# Third engine (Kyutai TTS, CC-BY 4.0): the streaming voice of the playground's
# voice mode. Same container, port and closed-allowlist rule.
_VOICE_KYUTAI_IDS = {"kyutai/tts-1.6b-en_fr"}


@bp.route("/voice/status")
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


@bp.route("/voice/start", methods=["POST"])
def voice_start():
    ok, out = _sudo("/usr/bin/docker", "start", "voice", timeout=60)
    return jsonify({"ok": ok, "detail": out})


@bp.route("/voice/stop", methods=["POST"])
def voice_stop():
    ok, out = _sudo("/usr/bin/docker", "stop", "voice", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@bp.route("/voice/launch", methods=["POST"])
def voice_launch():
    """Recreate the voice container with a Chatterbox, Qwen3-TTS or Kyutai model.
    repo_id comes from a closed allowlist (no free argv like for OCR/vLLM): no
    command construction to validate here, just membership in _VOICE_REPO_IDS."""
    data = request.get_json(silent=True) or {}
    repo_id = (data.get("repo_id") or "").strip()
    if repo_id in _VOICE_REPO_IDS:
        script = "/usr/local/sbin/voice-recreate.sh"
    elif repo_id in _VOICE_QWEN_IDS:
        script = "/usr/local/sbin/voice-qwen-recreate.sh"
    elif repo_id in _VOICE_KYUTAI_IDS:
        script = "/usr/local/sbin/voice-kyutai-recreate.sh"
    else:
        return jsonify({"ok": False, "detail": "invalid repo_id"}), 400
    ok, out = _sudo(script, repo_id, timeout=120)
    return jsonify({"ok": ok, "detail": out})


# Transcription (dictation). Same closed allowlist as voice.
_ASR_MODEL_IDS = {
    "openai/whisper-large-v3-turbo", "openai/whisper-large-v3",
    "openai/whisper-medium", "openai/whisper-small",
}


@bp.route("/asr/status")
def asr_status():
    ok, out = _sudo("/usr/bin/docker", "inspect", "asr")
    if not ok:
        return jsonify({"status": "unknown", "detail": out})
    try:
        state = json.loads(out)[0]["State"]
        return jsonify({"status": "running" if bool(state.get("Running")) else "stopped"})
    except Exception as e:
        return jsonify({"status": "unknown", "detail": str(e)})


@bp.route("/asr/start", methods=["POST"])
def asr_start():
    ok, out = _sudo("/usr/bin/docker", "start", "asr", timeout=60)
    return jsonify({"ok": ok, "detail": out})


@bp.route("/asr/stop", methods=["POST"])
def asr_stop():
    ok, out = _sudo("/usr/bin/docker", "stop", "asr", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@bp.route("/asr/launch", methods=["POST"])
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


@bp.route("/image/status")
def image_status():
    ok, out = _sudo("/usr/bin/docker", "inspect", "image")
    if not ok:
        return jsonify({"status": "unknown", "detail": out})
    try:
        state = json.loads(out)[0]["State"]
        return jsonify({"status": "running" if bool(state.get("Running")) else "stopped"})
    except Exception as e:
        return jsonify({"status": "unknown", "detail": str(e)})


@bp.route("/image/start", methods=["POST"])
def image_start():
    ok, out = _sudo("/usr/bin/docker", "start", "image", timeout=60)
    return jsonify({"ok": ok, "detail": out})


@bp.route("/image/stop", methods=["POST"])
def image_stop():
    ok, out = _sudo("/usr/bin/docker", "stop", "image", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@bp.route("/image/launch", methods=["POST"])
def image_launch():
    data = request.get_json(silent=True) or {}
    model = (data.get("model_id") or "").strip()
    if model not in _IMAGE_MODEL_IDS:
        return jsonify({"ok": False, "detail": "invalid model_id"}), 400
    ok, out = _sudo("/usr/local/sbin/image-recreate.sh", model, timeout=180)
    return jsonify({"ok": ok, "detail": out})


# ── HF id shapes ────────────────────────────────────────────────────────────
# Where the model id is free (an admin pastes a HuggingFace id), validate its
# SHAPE before any sudo call — the argument goes in argv (never interpreted by
# a shell).
_HF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
# Local GGUF ("local:<name>", ds4/llamacpp) — same charset, no required slash.
_LOCAL_ID_RE = re.compile(r"^local:[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
# Absolute local path to an already downloaded HF snapshot (vllm engine) —
# e.g. /root/.cache/huggingface/hub/models--…/snapshots/<sha>. Accepted because
# vllm receives the id verbatim in argv (never in a shell); the risk is a local
# file read, not execution, and /launch already sits behind admin auth.
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+-]+(/[A-Za-z0-9._/@+-]+)*$")


@bp.route("/video/status")
def video_status():
    ok, out = _sudo("/usr/bin/systemctl", "is-active", "comfyui.service")
    return jsonify({"status": "running" if (ok and out.strip() == "active") else "stopped"})


@bp.route("/video/start", methods=["POST"])
def video_start():
    ok, out = _sudo("/usr/bin/systemctl", "start", "comfyui.service", timeout=30)
    return jsonify({"ok": ok, "detail": out})


@bp.route("/video/stop", methods=["POST"])
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


@bp.route("/ocr/logs")
def ocr_logs():
    return _container_logs("ocr")


@bp.route("/voice/logs")
def voice_logs():
    return _container_logs("voice")


@bp.route("/image/logs")
def image_logs():
    return _container_logs("image")


@bp.route("/asr/logs")
def asr_logs():
    return _container_logs("asr")


@bp.route("/video/logs")
def video_logs():
    out = _combined_sudo("/usr/bin/journalctl", "-u", "comfyui.service",
                         "-n", str(_LOGS_TAIL), "--no-pager")
    return jsonify({"logs": out.splitlines()})
