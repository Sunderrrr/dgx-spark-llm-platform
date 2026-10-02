"""Dictation (ASR): audio transcription.

Extracted from app.py on 28/08, at the same time as voice: these routes lived
under the « Voix » banner although it is a distinct sidecar, on its own
network. That misplaced boundary is what made the section non-extractable.

asr_is_up() is re-imported by app.py: the sidecar dashboard uses it.
"""
import requests
from flask import Blueprint, jsonify, request

from auth import login_required
from config import ASR_URL
from sidecars import asr_is_up, motif_refus
from guards import (_MAX_VOICE_UPLOAD_BYTES, dictation_rate_block,
                    maintenance_block_json)

bp = Blueprint('asr', __name__)


@bp.route('/api/transcribe', methods=['POST'])
@login_required
def api_transcribe():
    """Dictation: mic audio → text. Deliberately self-hosted — the browser's
    SpeechRecognition API would send the voice to Google, which
    would defeat the whole point of the platform.
    """
    blocked = maintenance_block_json()
    if blocked:
        return blocked
    # Dictation is an expensive GPU endpoint (up to 180 s per call against the
    # shared ASR model) that has no LiteLLM key/budget, so it needs its own
    # bound — but NOT the media one: the client re-transcribes every second
    # while the user speaks (guards.dictation_rate_block explains the 429 that
    # cost the users 20 s of speech).
    limited = dictation_rate_block()
    if limited:
        return limited
    f = request.files.get('audio')
    if not f or not f.filename:
        return jsonify({'error': "Aucun audio fourni."}), 400
    data = f.read(_MAX_VOICE_UPLOAD_BYTES + 1)
    if len(data) > _MAX_VOICE_UPLOAD_BYTES:
        return jsonify({'error': "Enregistrement trop volumineux (15 Mo max)."}), 400
    language = request.form.get('language', '').strip()[:10]
    try:
        r = requests.post(f"{ASR_URL}/transcribe",
                          files={'audio': ('rec.wav', data, 'audio/wav')},
                          data={'language': language}, timeout=180)
        if not r.ok:
            detail = motif_refus(r)
            # The SIDECAR's error code is passed through, no longer overwritten as 502. A
            # recording too short, an unreadable format or a file too large are INPUT
            # ERRORS (400/413): presenting them as a service failure made it look like an
            # outage, and a client that retries automatically replayed a request that
            # could not work anyway. 503 (model not loaded) and 5xx remain genuine
            # failures.
            statut = r.status_code
            if 400 <= statut < 500:
                return jsonify({'error': detail or "Enregistrement refusé."}), statut
            if statut == 503:
                return jsonify({'error': detail or "Modèle de transcription non chargé."}), 503
            return jsonify({'error': detail or "Échec de la transcription."}), 502
        return jsonify({'text': r.json().get('text', '')})
    except requests.exceptions.Timeout:
        return jsonify({'error': "La transcription a mis trop de temps."}), 504
    except Exception:
        return jsonify({'error': "Service de transcription injoignable."}), 502

@bp.route('/api/transcribe/available')
@login_required
def api_transcribe_available():
    return jsonify({'available': asr_is_up()})

