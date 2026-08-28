"""Voix (Chatterbox / Qwen3-TTS) : clonage, synthese, historique.

Extrait de app.py le 28/08. La banniere « Voix » du monolithe couvrait en
realite TROIS sujets — la voix, la dictee (ASR) et l'amorcage de l'application
en fin de fichier — ce qui expliquait des dependances incoherentes
(init_db, AVATAR_IDS, LOGIN_WINDOW). La frontiere a donc ete redessinee avant
d'extraire : la dictee part dans asr_routes.py, l'amorcage reste dans app.py.

get_voice_engine / get_voice_languages viennent de la section « Helpers » : ce
sont des sondes purement voix, leur place est ici. VOICE_REPO_IDS et
get_voice_model restent dans app.py — ils servent au lancement de modeles
depuis l'administration, pas aux routes voix.
"""
import io as _io
import os
import secrets
import time
import wave as _wave
from datetime import datetime

import requests
from flask import Blueprint, abort, jsonify, request, send_file, session

from auth import login_required
from config import VOICE_URL
from db import get_db
from guards import _MAX_VOICE_UPLOAD_BYTES, maintenance_block_json, media_rate_block

bp = Blueprint('voice', __name__)

_voice_langs_cache = {'t': 0.0, 'v': {}}
_voice_engine_cache = {'t': 0.0, 'v': 'chatterbox'}

def get_voice_engine():
    """Voice engine currently served: 'chatterbox' or 'qwen3-tts'. Both
    share the container name and port; only this field, announced by
    /api/model-info, says which one answers — and thus which protocol to speak.
    """
    now = time.time()
    if now - _voice_engine_cache['t'] < 30:
        return _voice_engine_cache['v']
    v = 'chatterbox'
    try:
        r = requests.get(f"{VOICE_URL}/api/model-info", timeout=3)
        if r.ok:
            v = r.json().get('engine') or 'chatterbox'
    except Exception:
        pass
    _voice_engine_cache.update(t=now, v=v)
    return v

def get_voice_languages():
    """Languages actually accepted by the loaded Chatterbox variant.
    Turbo and Original speak ONLY English; only the multilingual
    variant handles 23. The list therefore comes from the live model rather
    than a constant — otherwise the page would offer languages the
    backend would refuse (or, worse, silently generate in English).
    """
    now = time.time()
    if now - _voice_langs_cache['t'] < 30:
        return _voice_langs_cache['v']
    v = {}
    try:
        r = requests.get(f"{VOICE_URL}/api/model-info", timeout=3)
        if r.ok:
            v = r.json().get('supported_languages') or {}
    except Exception:
        pass
    _voice_langs_cache.update(t=now, v=v)
    return v

# Internal container (dedicated voice_net network, cf. README "Security"), never
# a published port. Unlike OCR/video, generation is SYNCHRONOUS on the
# Chatterbox side (no queue to poll): /api/voice/generate
# returns the created job directly, ready to play.
VOICE_AUDIO_DIR = '/app/data/voice_audio'
VOICE_HISTORY_LIMIT = 20
_ALLOWED_AUDIO_TYPES = {'audio/wav', 'audio/x-wav', 'audio/mpeg', 'audio/mp3'}
_VOICE_AUDIO_EXT = {'audio/wav': 'wav', 'audio/x-wav': 'wav',
                    'audio/mpeg': 'mp3', 'audio/mp3': 'mp3'}

def _wav_duration_ms(audio_bytes):
    """Duration (ms) of a WAV audio buffer — the voice engine returns WAV. Serves the
    real-time factor (audio produced / generation time). None if unreadable
    (engine returning another format), in which case the factor is simply omitted.
    """
    import io as _io
    import wave as _wave
    try:
        with _wave.open(_io.BytesIO(audio_bytes), 'rb') as w:
            frames, rate = w.getnframes(), w.getframerate()
            if rate:
                return int(frames * 1000 / rate)
    except Exception:
        pass
    return None


def _read_uploaded_audio(field='reference'):
    """Reads and validates the reference voice sample. Returns (bytes, mime)
    or (None, error_message).
    """
    f = request.files.get(field)
    if not f or not f.filename:
        return None, "Aucun échantillon audio fourni."
    if f.mimetype not in _ALLOWED_AUDIO_TYPES:
        return None, "Format audio non supporté (WAV/MP3 uniquement)."
    data = f.read(_MAX_VOICE_UPLOAD_BYTES + 1)
    if len(data) > _MAX_VOICE_UPLOAD_BYTES:
        return None, "Échantillon audio trop volumineux (15 Mo max)."
    return data, f.mimetype

def voice_clone(reference_bytes, reference_mime, text, language='en', ref_text=''):
    """Sends the reference sample to the voice container then generates the
    clone. Returns (audio_bytes, None) or (None, error_message).

    Two protocols depending on the loaded engine (cf. get_voice_engine()):
    Qwen3-TTS exposes a single multipart POST, Chatterbox requires first an
    upload then a generation referenced by filename.

    The reference filename is always random (never derived from the
    name sent by the client): Chatterbox silently reuses an
    existing file on a name collision (behavior of its
    /upload_reference), which could otherwise make a user clone
    the voice left by another under a guessed/common filename.
    """
    if get_voice_engine() == 'qwen3-tts':
        try:
            r = requests.post(
                f"{VOICE_URL}/clone",
                files={'reference': (f"ref.{_VOICE_AUDIO_EXT.get(reference_mime, 'wav')}",
                                     reference_bytes, reference_mime)},
                data={'text': text, 'language': language, 'ref_text': ref_text or ''},
                timeout=180)
            if not r.ok:
                detail = ''
                try:
                    detail = r.json().get('detail', '')
                except Exception:
                    pass
                return None, detail or "Échec de la génération vocale."
            return r.content, None
        except requests.exceptions.Timeout:
            return None, "Le service voix a mis trop de temps à répondre."
        except Exception:
            return None, "Service voix injoignable."

    ref_ext = _VOICE_AUDIO_EXT.get(reference_mime, 'wav')
    ref_filename = f"{secrets.token_hex(16)}.{ref_ext}"
    try:
        r = requests.post(f"{VOICE_URL}/upload_reference",
                          files={'files': (ref_filename, reference_bytes, reference_mime)},
                          timeout=30)
        # The reason for a refusal (duration out of bounds, unreadable audio…) is NEVER
        # in the HTTP code, always in the body: /upload_reference replies
        # 400 if the only file sent is rejected, but 200 as soon as one file
        # passes — with the failures listed in `errors`. So we read the body
        # in both cases, otherwise the user gets a generic message instead
        # of the real reason (seen in prod: 47 s sample refused by
        # the duration cap, shown as "service unreachable").
        try:
            upload_errors = (r.json() or {}).get('errors') or []
        except ValueError:
            upload_errors = []
        if upload_errors:
            reason = (upload_errors[0] or {}).get('error') or ''
            return None, (f"Échantillon audio refusé : {reason}" if reason
                          else "Échantillon audio refusé par le service voix.")
        if not r.ok:
            return None, "Échec de l'envoi de l'échantillon audio."
        r = requests.post(f"{VOICE_URL}/tts", json={
            'text': text,
            'voice_mode': 'clone',
            'reference_audio_filename': ref_filename,
            'output_format': 'mp3',
            'language': language,
        }, timeout=120)
        if not r.ok:
            detail = ''
            try:
                detail = r.json().get('detail', '')
            except Exception:
                pass
            # Chatterbox refuses any sample of 5 s or less with a plain
            # internal assertion, surfaced here as "failed to synthesize" without
            # any usable hint. It's by far the most frequent cause
            # of failure at this step (the UI bounds mic recordings, but
            # not imported files): so we add the useful hint.
            if 'failed to synthesize' in detail.lower():
                return None, ("Échec de la génération — l'échantillon doit contenir "
                              "plus de 5 secondes de voix.")
            return None, detail or "Échec de la génération vocale."
        return r.content, None
    except requests.exceptions.Timeout:
        return None, "Le service voix a mis trop de temps à répondre."
    except Exception:
        return None, "Service voix injoignable."

@bp.route('/api/voice/generate', methods=['POST'])
@login_required
def api_voice_generate():
    blocked = maintenance_block_json()
    if blocked:
        return blocked
    limited = media_rate_block()
    if limited:
        return limited
    ref_bytes, err_or_mime = _read_uploaded_audio()
    if ref_bytes is None:
        return jsonify({'error': err_or_mime}), 400
    text = request.form.get('text', '').strip()[:2000]
    if not text:
        return jsonify({'error': "Un texte est requis."}), 400
    # Validated against the languages actually loaded: an English variant
    # (turbo/original) receiving 'fr' would generate English without saying so.
    langs = get_voice_languages()
    language = request.form.get('language', '').strip()[:10]
    if language not in langs:
        language = 'en' if 'en' in langs or not langs else next(iter(langs))
    ref_text = request.form.get('ref_text', '').strip()[:2000]
    _t0 = time.time()
    audio_bytes, err = voice_clone(ref_bytes, err_or_mime, text, language, ref_text)
    if audio_bytes is None:
        return jsonify({'error': err}), 502
    duration_ms = int((time.time() - _t0) * 1000)  # real generation time
    audio_ms = _wav_duration_ms(audio_bytes)        # duration of the produced audio (WAV)
    username = session['username']
    os.makedirs(VOICE_AUDIO_DIR, exist_ok=True)
    audio_filename = f"{secrets.token_hex(16)}.mp3"
    with open(os.path.join(VOICE_AUDIO_DIR, audio_filename), 'wb') as f:
        f.write(audio_bytes)
    db = get_db()
    db.execute("INSERT INTO voice_jobs (username, text, audio_path, created_at, duration_ms, audio_ms) VALUES (?,?,?,?,?,?)",
               (username, text, audio_filename, datetime.now().isoformat(), duration_ms, audio_ms))
    # Keeps only the VOICE_HISTORY_LIMIT most recent per user — also purges
    # the corresponding audio files, otherwise VOICE_AUDIO_DIR grows
    # indefinitely (same reasoning as OCR_IMAGES_DIR).
    stale = db.execute(
        "SELECT audio_path FROM voice_jobs WHERE username=? AND id NOT IN ("
        "  SELECT id FROM voice_jobs WHERE username=? ORDER BY id DESC LIMIT ?)",
        (username, username, VOICE_HISTORY_LIMIT)).fetchall()
    for row in stale:
        try:
            os.remove(os.path.join(VOICE_AUDIO_DIR, row['audio_path']))
        except OSError:
            pass
    db.execute("""DELETE FROM voice_jobs WHERE username=? AND id NOT IN (
                     SELECT id FROM voice_jobs WHERE username=?
                     ORDER BY id DESC LIMIT ?)""",
               (username, username, VOICE_HISTORY_LIMIT))
    db.commit()
    job_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
    return jsonify({'id': job_id})

@bp.route('/api/voice/history')
@login_required
def api_voice_history():
    rows = get_db().execute(
        "SELECT id, text, created_at FROM voice_jobs WHERE username=? ORDER BY id DESC",
        (session['username'],)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/voice/info')
@login_required
def api_voice_info():
    """Capabilities of the loaded voice backend. The page adapts to it: a language
    selector only if there are several, a transcription field only
    for Qwen (Chatterbox doesn't use clip transcription).
    """
    engine = get_voice_engine()
    return jsonify({
        'engine': engine,
        'languages': get_voice_languages(),
        'supports_ref_text': engine == 'qwen3-tts',
    })

@bp.route('/voice/audio/<int:job_id>')
@login_required
def voice_audio(job_id):
    # Scoped (id, username) in a single query — same IDOR guard as
    # /ocr/image/<job_id> and /video/file/<prompt_id>.
    row = get_db().execute(
        "SELECT audio_path FROM voice_jobs WHERE id=? AND username=?",
        (job_id, session['username'])).fetchone()
    if not row:
        abort(404)
    path = os.path.join(VOICE_AUDIO_DIR, row['audio_path'])
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype='audio/mpeg')
