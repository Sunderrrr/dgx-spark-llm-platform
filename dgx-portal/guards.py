"""Gardes partages par les routes couteuses (maintenance, debit).

Extrait de app.py le 28/08, avec les blueprints media : video, image et musique
appellent tous les deux memes gardes, qui vivaient dans le monolithe. Les
laisser la aurait force chaque blueprint a reimporter app.py — le cycle qu'on
evite depuis db.py.

Ne depend que de flask, du noyau (db) et du temps.
"""
import time

import json

from flask import Response, jsonify, request, session

from db import get_db, maintenance_active


def maintenance_block_json():
    if not maintenance_active() or session.get('is_admin'):
        return None
    return jsonify({'error': "Mode maintenance en cours — réessaie plus tard."}), 503


CHAT_RATE_MAX    = 20    # requests allowed…
CHAT_RATE_WINDOW = 60    # …per 60 s window and per user

def _chat_rate_limited(username, bucket):
    """Returns the number of seconds to wait, or 0 if the request can pass."""
    now = time.time()
    key = f"{bucket}|{username}"
    db = get_db()
    row = db.execute("SELECT fails, first_at FROM login_attempts WHERE key=?", (key,)).fetchone()
    if not row or now - row['first_at'] > CHAT_RATE_WINDOW:
        db.execute("INSERT INTO login_attempts (key, fails, first_at, locked_until) VALUES (?,1,?,0) "
                   "ON CONFLICT(key) DO UPDATE SET fails=1, first_at=excluded.first_at",
                   (key, now))
        db.commit()
        return 0
    if row['fails'] >= CHAT_RATE_MAX:
        return max(1, int(CHAT_RATE_WINDOW - (now - row['first_at'])))
    db.execute("UPDATE login_attempts SET fails=fails+1 WHERE key=?", (key,))
    db.commit()
    return 0


def media_rate_block():
    """Rate guard for the expensive GPU endpoints (video/OCR/voice/dictation).
    None goes through a LiteLLM key: the token budget therefore doesn't cap
    them, and each holds a gunicorn thread for up to 180 s while
    saturating the shared GPU. We bound the number of calls per user, as
    for chat, via the same sliding bucket.
    """
    wait = _chat_rate_limited(session['username'], 'rl-media')
    if wait:
        return jsonify({'error': f"Trop de requêtes. Réessaie dans {wait} s."}), 429
    return None


# Limite d'envoi audio, partagee par la voix (clip de reference) et la dictee :
# les deux acceptent un fichier de l'utilisateur vers du code de modele tiers.
_MAX_VOICE_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB, reference sample


# ── Envoi d'image, partage ───────────────────────────────────────────────────
# Lu par les routes video ET OCR : les deux acceptent une image de
# l'utilisateur. Cette aide vivait dans la section video du monolithe, ce qui
# l'a rendue invisible pour l'OCR au moment de l'extraction — la route
# /api/ocr/extract levait un NameError A L'APPEL, que ni les tests ni la
# comparaison de table de routes ne pouvaient voir.
_MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB, reference image
_ALLOWED_IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp'}


def _read_uploaded_image(field='image'):
    """Reads and validates an image file from the form. Returns (bytes, mime) or
    (None, error_message).
    """
    f = request.files.get(field)
    if not f or not f.filename:
        return None, "Aucune image fournie."
    if f.mimetype not in _ALLOWED_IMAGE_TYPES:
        return None, "Format d'image non supporté (PNG/JPEG/WebP uniquement)."
    data = f.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        return None, "Image trop volumineuse (15 Mo max)."
    return data, f.mimetype



# ── Aides SSE, partagees ─────────────────────────────────────────────────────
# Utilisees par le playground, le support et l'OCR : elles doivent vivre hors
# du monolithe pour qu'un blueprint puisse les importer.

def _sse_msg(text):
    """A single SSE 'content' message + end of stream (safe JSON escaping)."""
    payload = json.dumps({'choices': [{'delta': {'content': text}}]})
    return f"data: {payload}\n\ndata: [DONE]\n\n"


def maintenance_block_sse():
    """For use in the chat routes (SSE): same mechanism as the error
    messages already shown client-side ("No active model", etc.).
    """
    if not maintenance_active() or session.get('is_admin'):
        return None
    return Response(_sse_msg("Maintenance in progress — model access is temporarily "
                             "suspended, please try again later."),
                    mimetype='text/event-stream')
