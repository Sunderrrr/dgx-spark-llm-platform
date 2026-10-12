"""Playground voice mode — the spoken half (Kyutai TTS).

The conversation itself goes through /playground/chat like any other turn
(same history, same key and budget); dictation through /api/transcribe. What
is new is the VOICE: each sentence of the answer is sent here and comes back
as a stream of raw PCM, played by the browser as it arrives.

Kyutai TTS is the third engine of the « voice » container (see
voice-kyutai/). Only one voice engine runs at a time: when the container was
last created with Chatterbox or Qwen3-TTS, the voice mode says so instead of
speaking with a protocol that engine does not have.
"""
import time

import requests
from flask import Blueprint, Response, jsonify, request, session, stream_with_context

from auth import login_required
from config import VOICE_URL
from guards import _chat_rate_limited, maintenance_block_json
from sidecars import _sidecar_proc_status, sidecar_autostart

bp = Blueprint('voice_chat', __name__)

# A conversation speaks one request per sentence: ~10-30 a minute when the
# model talks a lot. The media bucket (20/min) would cut it mid-answer.
SPEAK_RATE_MAX = 90
SPEAK_RATE_WINDOW = 60
# Bounds of one call: a sentence or two, never a whole document.
SPEAK_MAX_CHARS = 1000
KYUTAI_ENGINE = 'kyutai'

_info_cache = {'t': 0.0, 'v': {}}


def _voice_info(max_age=3):
    """`/api/model-info` of the voice container ({} when it does not answer)."""
    now = time.time()
    if now - _info_cache['t'] < max_age:
        return _info_cache['v']
    v = {}
    if _sidecar_proc_status('voice') == 'running':
        try:
            r = requests.get(f"{VOICE_URL}/api/model-info", timeout=2)
            if r.ok:
                v = r.json() or {}
        except Exception:                            # noqa: BLE001
            pass
    _info_cache.update(t=now, v=v)
    return v


def _kyutai_ready():
    info = _voice_info(max_age=0)
    return info.get('engine') == KYUTAI_ENGINE and bool(info.get('loaded'))


def _status_body():
    info = _voice_info()
    state = _sidecar_proc_status('voice')
    engine = info.get('engine')
    return {
        'ready': engine == KYUTAI_ENGINE and bool(info.get('loaded')),
        'state': state,
        'engine': engine,
        # A running container with another engine cannot speak here; a
        # stopped one may be Kyutai — only starting it tells.
        'wrong_engine': bool(engine) and engine != KYUTAI_ENGINE,
        'voices': info.get('voices') or [],
        'default_voice': info.get('default_voice'),
        'sample_rate': info.get('sample_rate') or 24000,
    }


@bp.route('/api/voice/chat/status')
@login_required
def voice_chat_status():
    return jsonify(_status_body())


@bp.route('/api/voice/chat/start', methods=['POST'])
@login_required
def voice_chat_start():
    """Start the voice container on first use of the voice mode, under the
    same memory guard as every on-demand sidecar."""
    blocked = maintenance_block_json()
    if blocked:
        return blocked
    if not _kyutai_ready():
        ok, reason = sidecar_autostart('voice', _kyutai_ready, timeout_s=120)
        if not ok:
            body = _status_body()
            if body['wrong_engine']:
                return jsonify({**body, 'ok': False, 'code': 'voice_engine',
                                'error': "Le moteur vocal chargé n'est pas Kyutai TTS."}), 409
            return jsonify({**body, 'ok': False, 'code': 'voice_unavailable',
                            'error': reason}), 503
    return jsonify({**_status_body(), 'ok': True})


@bp.route('/api/voice/speak', methods=['POST'])
@login_required
def voice_speak():
    """One sentence → raw PCM (s16le, mono, X-Sample-Rate), streamed.

    The browser closing the request (barge-in) closes the upstream one, and
    the sidecar stops generating at its next frame."""
    blocked = maintenance_block_json()
    if blocked:
        return blocked
    wait = _chat_rate_limited(session['username'], 'rl-speak', SPEAK_RATE_MAX, SPEAK_RATE_WINDOW)
    if wait:
        return jsonify({'error': f"Trop de requêtes. Réessaie dans {wait} s."}), 429
    data = request.get_json(silent=True) or {}
    text = ' '.join(str(data.get('text') or '').split())
    if not text:
        return jsonify({'error': "Texte vide."}), 400
    if len(text) > SPEAK_MAX_CHARS:
        return jsonify({'error': f"Texte trop long ({SPEAK_MAX_CHARS} caractères max)."}), 413
    voice = str(data.get('voice') or '')[:16]
    # No pre-flight probe here: it went through the runner (sudo docker
    # inspect) once per sentence, and ONE slow answer turned a healthy voice
    # into a 503 that silenced the rest of the conversation (2026-10-13). The
    # sidecar's own answer says it all — another engine has no /api/speak.
    try:
        up = requests.post(f"{VOICE_URL}/api/speak", json={'text': text, 'voice': voice},
                           stream=True, timeout=(5, 60))
    except Exception:                                # noqa: BLE001
        return jsonify({'error': "La voix ne répond pas.", 'code': 'voice_unavailable'}), 503
    if not up.ok:
        up.close()
        if up.status_code == 404:
            return jsonify({'error': "Le moteur vocal chargé n'est pas Kyutai TTS.",
                            'code': 'voice_engine'}), 503
        return jsonify({'error': "La voix a refusé la phrase.", 'status': up.status_code}), 503

    def relay():
        try:
            for chunk in up.iter_content(chunk_size=None):
                if chunk:
                    yield chunk
        finally:
            up.close()

    return Response(stream_with_context(relay()), mimetype='application/octet-stream',
                    headers={'X-Sample-Rate': up.headers.get('X-Sample-Rate', '24000'),
                             'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
