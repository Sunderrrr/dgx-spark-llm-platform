"""Dictee (ASR) : transcription audio.

Extrait de app.py le 28/08, en meme temps que la voix : ces routes vivaient sous
la banniere « Voix » alors que c'est un sidecar distinct, sur son propre reseau.
C'est cette frontiere mal placee qui rendait la section non extractible.

asr_is_up() est reimporte par app.py : le tableau de bord des sidecars s'en sert.
"""
import time

import requests
from flask import Blueprint, jsonify, request, session

from auth import login_required
from config import ASR_URL
from db import get_db
from sidecars import asr_is_up
from guards import _MAX_VOICE_UPLOAD_BYTES, maintenance_block_json, media_rate_block

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
            detail = ''
            try:
                detail = r.json().get('detail', '')
            except Exception:
                pass
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

