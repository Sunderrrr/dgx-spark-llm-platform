"""Runner and sidecar driving: state, launch, stop, logs, probes.

Extracted from app.py on 28/08, from the « Helpers » banner.

The availability PROBES (get_ocr_model, get_voice_model, asr_is_up,
image_ready, get_image_model, music_ready, get_music_model) live here and
not in the corresponding blueprints. I first put them there, wrongly: a
sidecar is probed independently of its routes, and leaving them there
created a CROSS dependency (sidecars needs the probes, the routes need
_sidecar_proc_status). Grouping them here makes everything one-way again.

`_log` replaces `app.logger`: it is the same object (logging.getLogger('app')),
checked. See litellm_client.py.
"""
import json
import logging
import os
import re
import threading
import time

import requests
from flask import current_app, jsonify, session

from announcements import _announce_launch
from comfyui_client import comfyui_is_up
from config import (ASR_URL, IMAGE_URL, MUSIC_URL, OCR_URL, RUNNER_TOKEN,
                    RUNNER_URL, VOICE_URL)
from db import log_audit, set_setting
from litellm_client import _point_auto_model, _register_litellm_model
from vllm_health import get_running_models

_log = logging.getLogger('app')

# « timeout » patterns: the runner did not answer in the allotted time.
# This is NOT a failure — the operation is probably under way. The caller
# must therefore neither alert the infra nor invite the operator to click
# again (relaunching a model being loaded kills it: golden rule).
_MOTIF_DELAI_LANCEMENT = (
    "Le runner n'a pas répondu dans le délai imparti (150 s) : le lancement est "
    "peut-être toujours en cours. Vérifie l'état du modèle AVANT de relancer — "
    "relancer tuerait un modèle en cours de chargement.")
_MOTIF_DELAI_ARRET = (
    "Le runner n'a pas répondu dans le délai imparti (45 s) : l'arrêt est "
    "peut-être toujours en cours. Vérifie l'état du modèle avant de réessayer.")

# Launchable voice variants. Must stay aligned with runner.py's
# allowlists (_VOICE_REPO_IDS / _VOICE_QWEN_IDS), which revalidate on their side.
VOICE_REPO_IDS = (
    'Qwen3-TTS-12Hz-1.7B-Base', 'Qwen3-TTS-12Hz-0.6B-Base',
    'chatterbox-multilingual', 'chatterbox-turbo', 'chatterbox',
)




_ocr_model_cache = {'t': 0.0, 'v': None}

def get_ocr_model():
    """Model served by the OCR container (baidu/Unlimited-OCR), a separate vLLM
    with its own /v1/models — never mixed with get_running_models() on which
    other routes (stop/relaunch from admin) depend to target only
    the main chat model.
    """
    now = time.time()
    if now - _ocr_model_cache['t'] < 5:
        return _ocr_model_cache['v']
    v = None
    # Do NOT attempt the HTTP call if the container isn't running: the sidecar
    # network silently DROPs packets to an absent service, so
    # requests would wait the full timeout (~3 s) — that's what dragged down the
    # admin page when OCR was stopped. Process state is cached for 5 s.
    if _sidecar_proc_status('ocr') == 'running':
        try:
            r = requests.get(f"{OCR_URL}/models", timeout=3)
            if r.ok:
                data = r.json().get('data', [])
                if data:
                    v = data[0]['id']
        except Exception:
            pass
    _ocr_model_cache.update(t=now, v=v)
    return v

_voice_model_cache = {'t': 0.0, 'v': None}

def get_voice_model():
    """Chatterbox variant currently loaded by the voice container, probed
    live via /api/model-info (never frozen: the admin can recreate this
    container with another variant, cf. the voice catalog /admin/voice/*).
    Returns the type ('original'|'turbo'|'multilingual') only once
    the model is actually loaded (the 'loaded' field), not just the process up.
    """
    now = time.time()
    if now - _voice_model_cache['t'] < 5:
        return _voice_model_cache['v']
    v = None
    # Same guard as get_ocr_model: no HTTP call if the voice container is
    # stopped (otherwise a full ~3 s timeout, sidecar network in DROP).
    if _sidecar_proc_status('voice') == 'running':
        try:
            r = requests.get(f"{VOICE_URL}/api/model-info", timeout=3)
            if r.ok:
                data = r.json()
                if data.get('loaded'):
                    v = data.get('type')
        except Exception:
            pass
    _voice_model_cache.update(t=now, v=v)
    return v

_asr_info_cache = {'t': 0.0, 'v': {}}

def _asr_info():
    """Response of the ASR sidecar's /api/model-info, cached 10 s.

    A single HTTP call now serves two needs (is the sidecar ready, and WHICH
    model is loaded): `/api/admin` is polled every 8 s, we do not want to
    double the requests to a service we are waking up.
    """
    now = time.time()
    if now - _asr_info_cache['t'] < 10:
        return _asr_info_cache['v']
    info = {}
    try:
        r = requests.get(f"{ASR_URL}/api/model-info", timeout=3)
        if r.ok:
            info = r.json() or {}
    except Exception:
        info = {}
    _asr_info_cache.update(t=now, v=info)
    return info

def asr_is_up():
    return bool(_asr_info().get('loaded'))

def asr_model_name():
    """Name of the model ACTUALLY loaded, or None if the sidecar serves nothing.

    The UI hardcoded « whisper-large-v3-turbo »: true today, a lie as soon as
    the sidecar changes model. We prefer saying nothing (None) rather than
    repeating a frozen value.
    """
    info = _asr_info()
    return info.get('type') if info.get('loaded') else None

def asr_load_error():
    """Reason of the dictation model's load failure, or None.

    The ASR sidecar starts its server EVEN when the weights could not be
    loaded: measured on 2026-10-01, `CUDA error: out of memory` during
    `model.to(device)` — unified memory is shared with the chat model.
    It then answers `loaded: false` while publishing the cause, which nobody
    read: the admin showed « Démarrage… » indefinitely. A failure that does
    not speak reads as slowness.
    """
    info = _asr_info()
    return None if info.get('loaded') else (info.get('error') or None)

def image_ready():
    try:
        r = requests.get(f"{IMAGE_URL}/health", timeout=3)
        return bool(r.ok and r.json().get('ready'))
    except Exception:
        return False

def get_image_model():
    try:
        r = requests.get(f"{IMAGE_URL}/model-info", timeout=3)
        if r.ok:
            return r.json().get('model')
    except Exception:
        pass
    return None

def music_ready():
    try:
        r = requests.get(f"{MUSIC_URL}/health", timeout=3)
        return bool(r.ok and r.json().get('ready'))
    except Exception:
        return False

def get_music_model():
    try:
        r = requests.get(f"{MUSIC_URL}/model-info", timeout=3)
        if r.ok:
            return r.json().get('model')
    except Exception:
        pass
    return None

def motif_refus(r):
    """Refusal reason from a runner response, `''` if there is nothing to read.

    The runner answers `{"detail": "..."}` when it refuses a launch; the rest
    of the time its body is not JSON. Seven calls copied this `try/except` —
    a single definition is enough, and the silent failure is explicit there
    instead of being repeated seven times.
    """
    try:
        return r.json().get('detail', '') or ''
    except Exception:                                    # noqa: BLE001
        return ''


def _runner_headers():
    return {'Authorization': f'Bearer {RUNNER_TOKEN}'}

def runner_status():
    try:
        r = requests.get(f"{RUNNER_URL}/status", headers=_runner_headers(), timeout=3)
        if r.ok:
            st = r.json()
            # The runner only switches to "running" on the log line
            # "Application startup complete", hidden by --uvicorn-log-level
            # warning. We make state reliable by checking vLLM actually serves
            # the model → no more "Starting…" status stuck on screen.
            if st.get('status') == 'starting' and st.get('model') in get_running_models():
                st['status'] = 'running'
            return st
    except Exception:
        pass
    return {'status': 'unreachable', 'model': None, 'pid': None}

def runner_launch(hf_model_id, model_name, vllm_args='', engine='vllm'):
    """Launches a model. Returns (ok, motif, incertain).

    `incertain` distinguishes a REFUSAL (the runner answered no: nothing
    starts, `motif` carries its exact reason) from a TIMEOUT (no answer in
    the allotted time, while the loading may well be under way).
    The caller must NOT alert the infra on a doubt: a fake « echec de
    lancement » gets the administrator used to ignoring alerts and invites
    him to click again — which kills a model being loaded.

    Long timeout: when a model is already running, the runner waits for the
    driver to return the unified memory before launching the next one
    (anti-OOM). /launch thus takes 10-60 s generally, and up to ~100 s in
    the worst case (SIGTERM 30 s + SIGKILL 5 s + release wait 60 s +
    stabilization). At 90 s, a perfectly normal launch was reported as a failure.
    """
    def _once():
        r = requests.post(f"{RUNNER_URL}/launch",
                          headers=_runner_headers(),
                          json={'hf_model_id': hf_model_id, 'model_name': model_name,
                                'vllm_args': vllm_args, 'engine': engine or 'vllm'},
                          timeout=150)
        # Launch accepted → the `auto-model` alias follows the new model.
        if r.ok:
            # And the REAL model is re-registered: its limits advertised to LiteLLM
            # (max_input/max_output, computed by ctx_split) depend on THIS launch's
            # args. Without it, only `auto-model` followed a context change and the
            # real name kept the old values — observed on 04/09: auto-model advertised
            # 737856/262144, the real name 196608/65536.
            #
            # This routing stays IMMEDIATE (removing it during loading would change
            # nothing for a client: the upstream does not answer yet, it would get the
            # same error). What is deferred is the ANNOUNCEMENT to users and the alert
            # on failure — see _suivre_lancement.
            _register_litellm_model(model_name, vllm_args, engine or 'vllm')
            _point_auto_model(model_name, vllm_args, engine or 'vllm')
            _suivre_lancement(model_name)
            return True, '', False
        # The runner REFUSES with a precise reason (flag outside the allow-list,
        # missing engine, GGUF not found). Throwing it away and announcing only a
        # generic failure sends the administrator looking in the wrong place: seen
        # on 04/09, « flag not allowed: --llama-next » shown as « Runner inaccessible ».
        try:
            motif = (r.json() or {}).get('error') or r.text[:200]
        except Exception:                                    # noqa: BLE001
            motif = r.text[:200]
        return False, f"{motif} (HTTP {r.status_code})", False
    try:
        return _once()
    except requests.exceptions.ConnectionError:
        # Runner briefly unreachable: we retry ONCE. A ConnectionError
        # means that no launch could start (so no risk of double-spawn). A
        # timeout, on the other hand, may correspond to a startup already
        # under way → we do not insist (golden rule: never relaunch a model
        # that is already running).
        time.sleep(1)
        try:
            return _once()
        except requests.exceptions.Timeout:
            return False, _MOTIF_DELAI_LANCEMENT, True
        except Exception:
            return False, "runner injoignable", False
    except requests.exceptions.Timeout:
        return False, _MOTIF_DELAI_LANCEMENT, True
    except Exception as e:                                   # noqa: BLE001
        return False, f"runner injoignable ({type(e).__name__})", False

# Tracking window for an accepted launch. A ~100 Go loading can be long
# (weights + FlashInfer kernel compilation on first startup), and the
# runner's watchdog retries up to 3 times: the attempts need time before
# concluding failure.
_FENETRE_DEMARRAGE_S = 900
_INTERVALLE_SUIVI_S = 5

def _suivre_lancement(model_name, fenetre_s=None):
    """Checks AFTER THE FACT that an accepted launch really succeeded, and says so.

    The runner answers « starting » right at the Popen: accepting is not
    serving. Without this tracking, the portal announced the new model to
    users at spawn time — so a model that might never start — and a load
    failure (OOM, incomplete weights, unsupported arch) left the platform
    with NO served model without anyone being warned: the monitor does not
    probe vLLM (on-demand sidecar), so « runner up, no model » is invisible
    to it.

    The announcement to users now goes out when the model SERVES, and a
    failure produces an infra email + an audit line. Deliberately in a
    background thread: the admin request must answer immediately (it is
    held by act() on the UI side, and the runner's log stream already shows
    progress to the client).
    """
    fenetre = _FENETRE_DEMARRAGE_S if fenetre_s is None else fenetre_s
    annonce, audit = _announce_launch, log_audit
    try:
        ctx = current_app._get_current_object()
    except Exception:                                        # noqa: BLE001
        # Outside a request context (support tool, script): we give up tracking
        # rather than fail loudly — the launch itself did happen.
        return

    def _boucle():
        fin = time.time() + fenetre
        while time.time() < fin:
            time.sleep(_INTERVALLE_SUIVI_S)
            etat = runner_status()
            nom, statut = etat.get('model'), etat.get('status')
            if nom and nom != model_name and statut in ('running', 'starting'):
                # ANOTHER model was launched in the meantime: this is no longer our
                # business (and a deliberate stop must not trigger an alert).
                return
            if nom == model_name and statut == 'running':
                with ctx.app_context():
                    try:
                        annonce(model_name)
                        audit('systeme', 'model.servi',
                              f"{model_name} chargé et en service")
                        # The runner exposes no time of entry into service: we thus
                        # record WHAT THE PORTAL OBSERVED (the model answers at
                        # that instant). Read by the « état de la plateforme »
                        # banner; absent if the model was already serving before
                        # this version, and the UI then shows « — ».
                        set_setting('model_servi', json.dumps(
                            {'nom': model_name, 'depuis': int(time.time())}))
                    except Exception:                        # noqa: BLE001
                        _log.warning("suivi de lancement : annonce impossible", exc_info=True)
                return
            if nom == model_name and statut == 'error':
                break
        with ctx.app_context():
            try:
                audit('systeme', 'model.echec_apres_lancement',
                      f"{model_name} accepté par le runner mais jamais en service "
                      f"dans les {int(fenetre)} s")
                from notify import notify_infra_alert_email
                notify_infra_alert_email(
                    "Chat model never came up",
                    f"{model_name}: le runner a accepté le lancement mais le modèle "
                    f"n'est pas en service après {int(fenetre)} s. La plateforme n'a "
                    f"probablement AUCUN modèle servi — vérifie les logs du runner.")
            except Exception:                                # noqa: BLE001
                _log.warning("suivi de lancement : alerte impossible", exc_info=True)

    threading.Thread(target=_boucle, name='suivi-lancement', daemon=True).start()

def runner_stop():
    """Stops the served model. Returns (ok, motif, incertain).

    Timeout raised to 45 s: /stop SIGTERMs the process, waits up to 30 s,
    then SIGKILL (5 s) and erases last_model.json. At 5 s, a perfectly
    normal stop was announced « Runner vLLM inaccessible » while still going on.
    """
    try:
        r = requests.post(f"{RUNNER_URL}/stop", headers=_runner_headers(), timeout=45)
        if r.ok:
            return True, '', False
        return False, f"le runner a refusé l'arrêt (HTTP {r.status_code})", False
    except requests.exceptions.Timeout:
        return False, _MOTIF_DELAI_ARRET, True
    except Exception as e:                                   # noqa: BLE001
        return False, f"runner injoignable ({type(e).__name__})", False

def runner_delete_files(hf_model_id):
    """Erases a model's weights (via the runner). Returns (ok, octets, motif).

    `ok` is true also when there was NOTHING to erase (folder already
    absent): the wanted state — no more files — is reached. Wide timeout:
    an rm -rf of 100 Go on this disk takes a few seconds, but nothing hurries.
    """
    try:
        r = requests.post(f"{RUNNER_URL}/models/delete-files", headers=_runner_headers(),
                          json={'hf_model_id': hf_model_id}, timeout=620)
        corps = r.json() if r.content else {}
        if r.ok:
            return True, int(corps.get('bytes') or 0), ''
        if r.status_code == 404 and corps.get('absent'):
            return True, 0, 'aucun fichier sur le disque'
        return False, 0, corps.get('error') or f"HTTP {r.status_code}"
    except Exception as e:                                   # noqa: BLE001
        return False, 0, f"runner injoignable ({type(e).__name__})"


_sidecar_proc_cache = {}

def _sidecar_proc_status(kind):
    """kind ∈ {'ocr', 'video', 'voice', 'asr'} — raw PROCESS/CONTAINER state (docker inspect /
    systemctl is-active), via vllm-runner (scoped sudo privileges on the host,
    see /etc/sudoers.d/vllmrunner-services): dgx-portal itself has no
    docker/systemd access, neither here nor elsewhere. Does NOT say whether the service already
    answers requests — cf. _sidecar_status().

    Result cached 5 s: each call triggers on the runner side a `sudo`
    then a `docker inspect`/`systemctl is-active`, and the `systemctl` alone
    cost 1.5 s on this machine. The admin probes all four sidecars and
    refreshes every 8 s, so without the cache the page spent most of
    its time in there.
    """
    now = time.time()
    hit = _sidecar_proc_cache.get(kind)
    if hit and now - hit[0] < 5:
        return hit[1]
    v = 'unreachable'
    try:
        r = requests.get(f"{RUNNER_URL}/{kind}/status", headers=_runner_headers(), timeout=5)
        if r.ok:
            v = r.json().get('status', 'unknown')
    except Exception:
        pass
    _sidecar_proc_cache[kind] = (now, v)
    return v

def _sidecar_status(kind):
    """Status shown to the admin. A container/service that just started
    stays several tens of seconds (even minutes, large checkpoint) loading
    the model before it answers — during that time, docker/systemd already
    see it as "running", but any generation would fail. Before this
    fix, the admin card showed "Online" as soon as the process
    launched, not when the backend is really usable (reported: the
    status said video was running while it wasn't answering
    yet). So we additionally verify, live, that the service answers:
    get_ocr_model()/comfyui_is_up() hit respectively /v1/models and
    /system_stats, which only answer once loading is finished.

    We test the CONTAINER state first (fast, cached 5 s). If it isn't
    running, no point probing the HTTP service: the check would go into the
    void and wait its timeout (~3 s), which dragged down the whole admin page
    when a sidecar was stopped. The HTTP "does it answer yet?" probe only makes
    sense if the container is up, to tell "starting" from "running".
    """
    proc = _sidecar_proc_status(kind)
    if proc != 'running':
        return proc
    ready = (get_ocr_model() is not None if kind == 'ocr'
             else comfyui_is_up() if kind == 'video'
             else get_voice_model() is not None if kind == 'voice'
             else asr_is_up() if kind == 'asr'
             else image_ready() if kind == 'image'
             else music_ready() if kind == 'music'
             else False)
    if ready:
        return 'running'
    # A FAILED load is not a load in progress. The dictation sidecar says WHY
    # it loaded nothing (see asr_load_error): showing it beats waiting forever
    # for a « Démarrage… » that will never come. The other sidecars publish no
    # cause, hence no « failed » state for
    # them — they keep `starting`.
    return 'failed' if kind == 'asr' and asr_load_error() else 'starting'

def _mem_available_gb():
    """Actually allocatable memory (MemAvailable from /proc/meminfo), in GB.
    On the GB10 memory is unified: this is also the headroom available to
    load a model on the GPU.
    """
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return None

# Approximate memory (GB, margin included) a sidecar must be able to allocate
# to load its model. On unified memory, a sidecar that overflows doesn't
# merely fail: the OOM killer kills the largest process — the chat model —
# and the whole platform goes down. Hence this guard BEFORE starting.
# OCR/voice/dictation load a model then stay stable → threshold = weight + small
# margin. Video (ComfyUI) additionally has memory SPIKES during generation →
# higher threshold to keep a real cushion. The chat model's memory is,
# itself, frozen at launch (KV pre-allocated), so once a sidecar is loaded
# the whole is stable — that's what makes these thresholds reliable.
# 'music' covers the sidecar's default mode: 8-bit quantization of the two
# big LLMs (~24 Go in bf16 → ~13 Go). Overridable by env if the sidecar goes
# back to full precision (MUSIC_QUANT=none) or 4-bit.
_SIDECAR_MEM_NEED_GB = {'ocr': 20, 'video': 28, 'voice': 15, 'asr': 5, 'image': 40,
                        'music': int(os.environ.get('MUSIC_MEM_NEED_GB', 15))}

def _mem_guard(kind):
    """Return an error message if starting `kind` risks an OOM, otherwise None."""
    need = _SIDECAR_MEM_NEED_GB.get(kind)
    if not need:
        return None
    avail = _mem_available_gb()
    if avail is not None and avail < need:
        return (f"Mémoire insuffisante pour démarrer {kind} : {avail:.0f} Go libres, "
                f"~{need} Go requis. Arrête un autre backend, ou réduis le contexte du "
                f"modèle de chat, puis réessaie.")
    return None

def _sidecar_start_json(kind):
    """Start a sidecar with a memory guard, JSON response for the frontend."""
    err = _mem_guard(kind)
    if err:
        return jsonify({'ok': False, 'error': err}), 507
    ok, detail = _sidecar_action(kind, 'start')
    return jsonify({'ok': bool(ok),
                    'error': None if ok else f"Échec du démarrage {kind}.{detail}"}), (200 if ok else 502)

def _sidecar_stop_json(kind):
    """Stopping a sidecar: same JSON contract as the start.

    The stop answered with a flash + a redirect, which the Next UI renders
    nowhere: a stop failure thus displayed as a success.
    """
    ok, detail = _sidecar_action(kind, 'stop')
    return jsonify({'ok': bool(ok),
                    'error': None if ok else f"Échec de l'arrêt {kind}.{detail}"}), (200 if ok else 502)

def _sidecar_action(kind, action):
    """(ok, detail) — `detail` carries the exact reason returned by the runner.

    Throwing it away left the administrator with only a « Échec du démarrage
    ocr. » identical for an unreachable runner, a missing container after a
    failed recreation and a transient error — so nothing actionable without
    opening a shell on the host.
    """
    ok, detail = False, ''
    try:
        r = requests.post(f"{RUNNER_URL}/{kind}/{action}", headers=_runner_headers(), timeout=30)
        ok = r.ok
        if not ok:
            motif = ''
            try:
                corps = r.json() or {}
                motif = corps.get('detail') or corps.get('error') or ''
            except Exception:                                # noqa: BLE001
                motif = (r.text or '')[:200]
            motif = str(motif).strip()
            detail = f" ({motif})" if motif else f" (HTTP {r.status_code})"
    except Exception as e:                                   # noqa: BLE001
        detail = f" ({type(e).__name__})"
    log_audit(session.get('username'), f'sidecar.{action}',
              f"{kind} : {'OK' if ok else 'échec' + detail}")
    return ok, detail

def _ocr_launch(hf_id, args):
    """Recreate the OCR container with another model (runner.py validates the flags
    against the OCR allowlist before any sudo call, see _OCR_*_FLAGS).
    """
    try:
        r = requests.post(f"{RUNNER_URL}/ocr/launch", headers=_runner_headers(),
                          json={'hf_model_id': hf_id, 'vllm_args': args or ''}, timeout=90)
        return r.ok, motif_refus(r)
    except Exception as e:
        return False, str(e)

def _voice_launch(repo_id):
    """Recreate the voice container with another Chatterbox variant (runner.py
    revalidates repo_id against its own allowlist before any sudo call,
    see _VOICE_REPO_IDS).
    """
    try:
        r = requests.post(f"{RUNNER_URL}/voice/launch", headers=_runner_headers(),
                          json={'repo_id': repo_id}, timeout=90)
        return r.ok, motif_refus(r)
    except Exception as e:
        return False, str(e)

# Image generation models the admin may launch (mirrors _VOICE_REPO_IDS): a
# closed allowlist, revalidated by the runner (_IMAGE_MODEL_IDS) before any sudo
# call. Each id maps host-side to a pre-downloaded diffusers dir (image-recreate.sh).
IMAGE_MODEL_IDS = {'black-forest-labs/FLUX.2-klein-4B'}

def _image_launch(model_id):
    """Recreate the image container with another diffusers model (runner.py
    revalidates model_id against its own allowlist before any sudo call).
    """
    try:
        r = requests.post(f"{RUNNER_URL}/image/launch", headers=_runner_headers(),
                          json={'model_id': model_id}, timeout=180)
        return r.ok, motif_refus(r)
    except Exception as e:
        return False, str(e)

# Music models: free HuggingFace id (like OCR), the shape is validated
# here AND on the runner side before any sudo call.
_HF_ID_RE = re.compile(r'^[A-Za-z0-9][\w.-]{0,60}/[A-Za-z0-9][\w.-]{0,80}$')

def _music_launch(model_id):
    """Recreates the music container with another HF model."""
    try:
        r = requests.post(f"{RUNNER_URL}/music/launch", headers=_runner_headers(),
                          json={'model_id': model_id}, timeout=180)
        return r.ok, motif_refus(r)
    except Exception as e:
        return False, str(e)

# Routine access lines (health/status polls) → noise that drowns the useful logs.
_LOG_NOISE_RE = re.compile(r'"GET /(?:v1/models|metrics|health\S*|version|ping)\b')

def _drop_log_noise(lines):
    return [l for l in lines if not _LOG_NOISE_RE.search(l)]

# The runner's full buffer. We ALWAYS ask for the maximum, never a window
# proportional to n: the buffer is dominated by routine access lines (/metrics
# and /v1/models probes), in a proportion that varies with the polling pace
# and the model's activity. Measurement of 28/08, model idle: of the last 1
# 000 raw lines, ZERO useful line — the n*5 = 750 window held only noise,
# `runner_logs` returned an empty list and the admin panel displayed « ce
# modele n'est pas demarre » while it was running. The first useful lines
# only appeared beyond 1 500.
_RUNNER_LOGS_TAMPON = 2000

def runner_logs(n=150):
    try:
        r = requests.get(f"{RUNNER_URL}/logs", headers=_runner_headers(),
                         params={'n': _RUNNER_LOGS_TAMPON}, timeout=5)
        if r.ok:
            return _drop_log_noise(r.json().get('logs', []))[-n:]
        _log.warning("runner_logs : le runner a repondu %s", r.status_code)
    except Exception as e:                                   # noqa: BLE001
        # Without this trace, a failure here is indistinguishable from a stopped
        # model: this is exactly what masked the problem above.
        _log.warning("runner_logs : %s", type(e).__name__)
    return []

_runner_metrics_cache = {'t': 0.0, 'v': None}

def runner_metrics():
    """Host CPU/RAM/GPU metrics from the runner. Expensive on the runner side:
    _cpu_pct() samples /proc/stat and _gpu() spawns nvidia-smi. /api/home calls
    this on every poll, so cache briefly — the "Server state" panel is polled
    every ~5 s and doesn't need sub-3 s freshness. (The 229 ms cost itself is a
    runner-side time.sleep(0.2); see vllm-runner/runner.py _cpu_pct.)
    """
    now = time.time()
    if now - _runner_metrics_cache['t'] < 3:
        return _runner_metrics_cache['v']
    out = None
    try:
        r = requests.get(f"{RUNNER_URL}/metrics", headers=_runner_headers(), timeout=5)
        if r.ok:
            out = r.json()
    except Exception:
        pass
    _runner_metrics_cache.update(t=now, v=out)
    return out
