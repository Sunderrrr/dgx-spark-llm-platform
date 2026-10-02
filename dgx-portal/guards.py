"""Guards shared by the costly routes (maintenance, rate).

Extracted from app.py on 28/08, with the media blueprints: video, image and
music all call the same two guards, which lived in the monolith. Leaving
them there would have forced each blueprint to re-import app.py — the cycle
we have avoided since db.py.

Depends only on flask, the core (db) and time.
"""
import struct
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import json

from flask import Response, jsonify, request, session

from config import KEY_DURATION
from db import get_db, get_setting, maintenance_active


# ── Quota guard (reliable SpendLogs accounting) ────────────────────────────
# LiteLLM's native 429 is not enough: its internal counter was wiped for
# weeks by the daily resets and its DB sync is capricious (one account: 242 M
# real tokens over 7 days against 1,3 M counted, 2026-09-09 — it "exceeded"
# its quota without ever being blocked). The portal therefore judges BEFORE
# the model call on SpendLogs — the same source as the usage card: what the
# user sees exceeded is really enforced.
_QUOTA_CACHE = {}          # username -> (horodatage, verdict)
_QUOTA_CACHE_TTL = 30      # s: one Postgres aggregate pass per account is enough


def quota_depasse_reset(username):
    """Date of the next reset if the account has exhausted its envelope, else None.

    We return the DATA (date), not a sentence: the wording lives on the
    frontend side, translated into the interface language (i18n contract: French msgid)."""
    now = time.time()
    hit = _QUOTA_CACHE.get(username)
    if hit and now - hit[0] < _QUOTA_CACHE_TTL:
        return hit[1]
    reset = None
    try:
        from litellm_client import _litellm_user_info
        from stats import _real_tokens_by_user
        ui = _litellm_user_info(username)
        effective = ui.get('max_budget')
        if effective:
            duree = get_setting('default_key_duration', KEY_DURATION)
            jours = int(''.join(c for c in str(duree) if c.isdigit()) or 7)
            since = datetime.utcnow() - timedelta(days=jours)
            used = int(_real_tokens_by_user(since).get(username, 0) or 0)
            if used >= int(effective):
                reset = (ui.get('budget_reset_at') or '')[:16].replace('T', ' ')
    except Exception:
        reset = None                     # NEVER block on an internal failure
    _QUOTA_CACHE[username] = (now, reset)
    return reset


def maintenance_block_json():
    if not maintenance_active() or session.get('is_admin'):
        return None
    return jsonify({'error': "Mode maintenance en cours — réessaie plus tard."}), 503


def media_block_json():
    """The TWO refusals that open any media generation: maintenance, then rate.

    Returns the response to send back, or None when the request can proceed.
    The four media routes (image, music, video, voice) repeated these four
    lines identically; the ORDER — maintenance first — is a decision, which
    now has a single place to be reviewed.
    """
    return maintenance_block_json() or media_rate_block()


CHAT_RATE_MAX    = 20    # requests allowed…
CHAT_RATE_WINDOW = 60    # …per 60 s window and per user

def _chat_rate_limited(username, bucket, max_requetes=None, fenetre=None):
    """Returns the number of seconds to wait, or 0 if the request can pass.

    The cap and the window are PARAMETERS since 2026-09-22: dictation does
    not call ASR at a clicking user's pace, but every second (see
    `dictation_rate_block`), and a single cap cannot describe both
    usages."""
    max_requetes = CHAT_RATE_MAX if max_requetes is None else max_requetes
    fenetre = CHAT_RATE_WINDOW if fenetre is None else fenetre
    now = time.time()
    key = f"{bucket}|{username}"
    db = get_db()
    # The ACQUISITION is a single conditional UPSERT since the 2026-10-02 audit.
    # A SELECT followed by an UPDATE let N simultaneous requests through: all
    # read `fails` before the first write. 64 gunicorn threads could thus cross a
    # cap of 20 at once, saturate the pool and the GPU — and the counter must
    # live in SQL anyway, since the 4 workers cannot coordinate otherwise.
    # `rowcount` is 1 when the request was counted (window open and cap not
    # reached), 0 otherwise: this is the decision, made by the database, not by
    # application code.
    cur = db.execute(
        "INSERT INTO login_attempts (key, fails, first_at, locked_until) VALUES (?,1,?,0) "
        "ON CONFLICT(key) DO UPDATE SET fails=fails+1 "
        "WHERE login_attempts.first_at > ? AND login_attempts.fails < ?",
        (key, now, now - fenetre, max_requetes))
    compte = cur.rowcount
    db.commit()
    if compte:
        return 0
    row = db.execute("SELECT first_at FROM login_attempts WHERE key=?", (key,)).fetchone()
    if row and now - row['first_at'] <= fenetre:
        return max(1, int(fenetre - (now - row['first_at'])))
    # Expired window: reopen it. Two simultaneous requests can both reopen it
    # and the second overwrites the first — the only effect is one uncounted
    # request, once per window.
    db.execute("UPDATE login_attempts SET fails=1, first_at=? WHERE key=?", (now, key))
    db.commit()
    return 0


def media_rate_block():
    """Rate guard for the expensive GPU endpoints (video/OCR/voice/image/music).
    None goes through a LiteLLM key: the token budget therefore doesn't cap
    them, and each holds a gunicorn thread for up to 180 s while
    saturating the shared GPU. We bound the number of calls per user, as
    for chat, via the same sliding bucket.
    """
    wait = _chat_rate_limited(session['username'], 'rl-media')
    if wait:
        return jsonify({'error': f"Trop de requêtes. Réessaie dans {wait} s."}), 429
    return None


# ── Dictation (ASR): a budget at its own pace, not the media one ────────────
# Dictation is NOT a single media call: `useDictation` (frontend)
# retranscribes all audio captured since the start EVERY SECOND while the
# user speaks (POLL_MS = 1000), plus a final pass. Sharing the media window
# of 20/min thus condemned it to 429 after about 20 s of speech — observed
# on 2026-09-22 (« Trop de requêtes. Réessaie dans 36 s. ») and the
# dictation stopped being written. Its budget follows its real pace
# (~1 req/s) with 25 % headroom: a long dictation never touches it, a
# hammering client meets it at once.
ASR_RATE_MAX    = 75    # transcriptions allowed…
ASR_RATE_WINDOW = 60    # …per 60 s window and per user


def dictation_rate_block():
    """Rate guard for `/api/transcribe` (bucket distinct from `rl-media`)."""
    wait = _chat_rate_limited(session['username'], 'rl-asr', ASR_RATE_MAX, ASR_RATE_WINDOW)
    if wait:
        return jsonify({'error': f"Trop de requêtes. Réessaie dans {wait} s."}), 429
    return None


# ── Async jobs (image/music): per-user concurrency bound ───────────────────
# media_rate_block bounds the RATE (20/min), but each request creates a
# daemon thread that blocks up to 600 s (image) / 1800 s (music) on the
# sidecar. Over several windows one account can thus stack threads that stay
# hooked to the GPU. We bound the number of IN-FLIGHT jobs per account,
# independently of the pace — the only real limit for a background thread.
_MEDIA_SLOTS = defaultdict(int)
_MEDIA_SLOTS_LOCK = threading.Lock()
MEDIA_MAX_CONCURRENT = 3

def media_job_slot(username):
    """Acquires an async job slot for `username`. True if accepted (the caller
    MUST release via media_job_done when the worker finishes), False if this
    account already has MEDIA_MAX_CONCURRENT jobs in flight."""
    with _MEDIA_SLOTS_LOCK:
        if _MEDIA_SLOTS[username] >= MEDIA_MAX_CONCURRENT:
            return False
        _MEDIA_SLOTS[username] += 1
        return True

def media_job_done(username):
    """Releases a slot acquired by media_job_slot (called at the end of the worker)."""
    with _MEDIA_SLOTS_LOCK:
        cur = _MEDIA_SLOTS.get(username, 0)
        if cur <= 1:
            _MEDIA_SLOTS.pop(username, None)
        else:
            _MEDIA_SLOTS[username] = cur - 1


# Audio upload limit, shared by voice (reference clip) and dictation: both
# accept a user file towards third-party model code.
_MAX_VOICE_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB, reference sample


# ── Envoi d'image, partage ───────────────────────────────────────────────────
# Read by the video AND OCR routes: both accept a user image. This helper
# lived in the monolith's video section, which made it invisible to OCR at
# extraction time — the /api/ocr/extract route raised a NameError AT CALL
# TIME, which neither the tests nor the route-table comparison could
# see.
_MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB, reference image
_ALLOWED_IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp'}

# ── Image DECODING bound (audit of 2026-10-02) ─────────────────────────────
# The byte cap only bounds the COMPRESSED file: a PNG of a few hundred Ko can
# describe 170 Mpx (a flat area compresses ~1000:1) and make the decoding
# process allocate ~0,7 Go — ComfyUI, a HOST service for video, or the OCR
# container. On unified memory, the OOM-killer targets the biggest RSS, i.e.
# the served model. The dimensions are read in the HEADER, without decoding:
# the portal has no Pillow, and decoding to measure would already be the
# bomb. An unreadable header is REFUSED (fail-closed): letting through what
# we cannot measure would not merely cancel the guard, corrupting the header
# would be enough to bypass it.
MAX_IMAGE_PIXELS = 40_000_000  # ~40 Mpx


def _dimensions_image(data, mime):
    """(width, height) read from the PNG/JPEG/WebP header, or None."""
    try:
        if mime == 'image/png':
            # Signature puis chunk IHDR : largeur/hauteur en u32 big-endian.
            if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
                return None
            return struct.unpack('>II', data[16:24])
        if mime == 'image/jpeg':
            # Walk the segments up to the SOF marker (0xC0-0xCF, except DHT, JPG and
            # DAC which share the prefix).
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marqueur = data[i + 1]
                if marqueur == 0x01 or 0xD0 <= marqueur <= 0xD8:
                    i += 2
                    continue
                taille = struct.unpack('>H', data[i + 2:i + 4])[0]
                if marqueur in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    hauteur, largeur = struct.unpack('>HH', data[i + 5:i + 9])
                    return largeur, hauteur
                if marqueur == 0xDA:  # start of data: no SOF left to come
                    return None
                i += 2 + taille
            return None
        if mime == 'image/webp':
            if data[:4] != b'RIFF' or data[8:12] != b'WEBP':
                return None
            format_webp = data[12:16]
            if format_webp == b'VP8X':      # 24-bit dimensions, minus one
                return (int.from_bytes(data[24:27], 'little') + 1,
                        int.from_bytes(data[27:30], 'little') + 1)
            if format_webp == b'VP8 ':      # lossy : 14 bits chacune
                return (struct.unpack('<H', data[26:28])[0] & 0x3FFF,
                        struct.unpack('<H', data[28:30])[0] & 0x3FFF)
            if format_webp == b'VP8L':      # lossless: 14 bits packed
                bits = int.from_bytes(data[21:25], 'little')
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            return None
    except (struct.error, IndexError):
        return None
    return None


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
    dimensions = _dimensions_image(data, f.mimetype)
    if dimensions is None:
        return None, "Image illisible (en-tête PNG/JPEG/WebP non reconnu)."
    if dimensions[0] * dimensions[1] > MAX_IMAGE_PIXELS:
        return None, (f"Image trop grande : {dimensions[0]}×{dimensions[1]} pixels "
                      f"({MAX_IMAGE_PIXELS // 1_000_000} Mpx max).")
    return data, f.mimetype



# ── Aides SSE, partagees ─────────────────────────────────────────────────────
# Used by the playground, the support and the OCR: they must live outside
# the monolith so a blueprint can import them.

def _sse_msg(text):
    """A single SSE 'content' message + end of stream (safe JSON escaping)."""
    payload = json.dumps({'choices': [{'delta': {'content': text}}]})
    return f"data: {payload}\n\ndata: [DONE]\n\n"


def _sse_notice(nid, **args):
    """STRUCTURED system notice (e.g. quota exceeded): the frontend translates it
    into the interface language — the server never writes a sentence."""
    payload = json.dumps({'cronos_notice': {'id': nid, **args}})
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
