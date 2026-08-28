"""Gardes partages par les routes couteuses (maintenance, debit).

Extrait de app.py le 28/08, avec les blueprints media : video, image et musique
appellent tous les deux memes gardes, qui vivaient dans le monolithe. Les
laisser la aurait force chaque blueprint a reimporter app.py — le cycle qu'on
evite depuis db.py.

Ne depend que de flask, du noyau (db) et du temps.
"""
import time

from flask import jsonify, session

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
