"""Gardes partages par les routes couteuses (maintenance, debit).

Extrait de app.py le 28/08, avec les blueprints media : video, image et musique
appellent tous les deux memes gardes, qui vivaient dans le monolithe. Les
laisser la aurait force chaque blueprint a reimporter app.py — le cycle qu'on
evite depuis db.py.

Ne depend que de flask, du noyau (db) et du temps.
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


# ── Garde de quota (comptabilité fiable SpendLogs) ───────────────────────────
# Le 429 natif de LiteLLM ne suffit pas : son compteur interne a été écrasé
# pendant des semaines par les resets quotidiens et sa sync DB est capricieuse
# (un compte : 242 M de tokens réels sur 7 j contre 1,3 M comptés, 2026-09-09 —
# il « dépassait » son quota sans jamais être bloqué). Le portail juge donc
# AVANT l'appel modèle sur SpendLogs — la même source que la carte
# d'utilisation : ce que l'utilisateur voit dépassé est réellement appliqué.
_QUOTA_CACHE = {}          # username -> (horodatage, verdict)
_QUOTA_CACHE_TTL = 30      # s : un tour d'agrégat Postgres par compte suffit


def quota_depasse_reset(username):
    """Date du prochain reset si le compte a épuisé son enveloppe, sinon None.

    On renvoie la DONNÉE (date), pas un texte : la phrase vit côté frontend,
    traduite dans la langue de l'interface (contrat i18n : msgid français)."""
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
        reset = None                     # ne JAMAIS bloquer sur une panne interne
    _QUOTA_CACHE[username] = (now, reset)
    return reset


def maintenance_block_json():
    if not maintenance_active() or session.get('is_admin'):
        return None
    return jsonify({'error': "Mode maintenance en cours — réessaie plus tard."}), 503


def media_block_json():
    """Les DEUX refus qui ouvrent toute génération média : maintenance, puis débit.

    Renvoie la réponse à retourner, ou None quand la requête peut continuer.
    Les quatre routes média (image, musique, vidéo, voix) répétaient ces quatre
    lignes à l'identique ; l'ORDRE — la maintenance d'abord — est une décision,
    qui n'a donc plus qu'un seul endroit où être relue.
    """
    return maintenance_block_json() or media_rate_block()


CHAT_RATE_MAX    = 20    # requests allowed…
CHAT_RATE_WINDOW = 60    # …per 60 s window and per user

def _chat_rate_limited(username, bucket, max_requetes=None, fenetre=None):
    """Returns the number of seconds to wait, or 0 if the request can pass.

    Le plafond et la fenêtre sont des PARAMÈTRES depuis le 2026-09-22 : la
    dictée n'appelle pas l'ASR au rythme d'un utilisateur qui clique, mais
    toutes les secondes (cf. `dictation_rate_block`), et un plafond unique ne
    peut pas décrire les deux usages."""
    max_requetes = CHAT_RATE_MAX if max_requetes is None else max_requetes
    fenetre = CHAT_RATE_WINDOW if fenetre is None else fenetre
    now = time.time()
    key = f"{bucket}|{username}"
    db = get_db()
    # L'ACQUISITION est un seul UPSERT conditionnel depuis l'audit du 2026-10-02.
    # Un SELECT suivi d'un UPDATE laissait passer N requêtes simultanées : toutes
    # lisaient `fails` avant la première écriture. 64 threads gunicorn pouvaient
    # donc franchir un plafond de 20 d'un coup, saturer le pool et le GPU — et le
    # compteur doit de toute façon vivre en SQL, puisque les 4 workers ne peuvent
    # pas se coordonner autrement. `rowcount` vaut 1 quand la requête a été
    # comptée (fenêtre ouverte et plafond non atteint), 0 sinon : c'est la
    # décision, prise par la base, pas par du code applicatif.
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
    # Fenêtre expirée : la rouvrir. Deux requêtes simultanées peuvent la rouvrir
    # toutes les deux et la seconde écrase la première — le seul effet est une
    # requête non comptée, une fois par fenêtre.
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


# ── Dictée (ASR) : un budget à son rythme, pas celui des médias ──────────────
# La dictée n'est PAS un appel média isolé : `useDictation` (frontend)
# retranscrit tout l'audio capté depuis le début TOUTES LES SECONDES pendant que
# l'utilisateur parle (POLL_MS = 1000), plus une passe finale. Partager la
# fenêtre de 20/min des médias la condamnait donc au 429 au bout d'environ 20 s
# de parole — constaté le 2026-09-22 (« Trop de requêtes. Réessaie dans 36 s. »)
# et la dictée cessait de s'écrire. Son budget suit son rythme réel (~1 req/s)
# avec 25 % de marge : une longue dictée ne le touche jamais, un client qui
# martèle le trouve tout de suite.
ASR_RATE_MAX    = 75    # transcriptions allowed…
ASR_RATE_WINDOW = 60    # …per 60 s window and per user


def dictation_rate_block():
    """Garde de débit de `/api/transcribe` (bucket distinct de `rl-media`)."""
    wait = _chat_rate_limited(session['username'], 'rl-asr', ASR_RATE_MAX, ASR_RATE_WINDOW)
    if wait:
        return jsonify({'error': f"Trop de requêtes. Réessaie dans {wait} s."}), 429
    return None


# ── Jobs asynchrones (image/musique) : borne de concurrence par utilisateur ──
# media_rate_block borne le DEBIT (20/min), mais chaque requete cree un thread
# daemon qui se bloque jusqu'a 600 s (image) / 1800 s (musique) sur le sidecar.
# Sur plusieurs fenetres, un meme compte peut donc empiler des threads qui
# restent accroches au GPU. On borne le nombre de jobs EN COURS par compte,
# independamment du rythme — la seule vraie limite pour un thread de fond.
_MEDIA_SLOTS = defaultdict(int)
_MEDIA_SLOTS_LOCK = threading.Lock()
MEDIA_MAX_CONCURRENT = 3

def media_job_slot(username):
    """Acquiert un slot de job asynchrone pour `username`. True si accepte
    (l'appelant DOIT liberer via media_job_done quand le worker finit),
    False si ce compte a deja MEDIA_MAX_CONCURRENT jobs en cours."""
    with _MEDIA_SLOTS_LOCK:
        if _MEDIA_SLOTS[username] >= MEDIA_MAX_CONCURRENT:
            return False
        _MEDIA_SLOTS[username] += 1
        return True

def media_job_done(username):
    """Libere un slot acquis par media_job_slot (appele en fin de worker)."""
    with _MEDIA_SLOTS_LOCK:
        cur = _MEDIA_SLOTS.get(username, 0)
        if cur <= 1:
            _MEDIA_SLOTS.pop(username, None)
        else:
            _MEDIA_SLOTS[username] = cur - 1


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

# ── Borne de DÉCODAGE de l'image (audit du 2026-10-02) ───────────────────────
# Le plafond d'octets ne borne que le fichier COMPRESSÉ : un PNG de quelques
# centaines de Ko peut décrire 170 Mpx (un aplat se compresse ~1000:1) et faire
# allouer ~0,7 Go au processus qui le décode — ComfyUI, un service HÔTE pour la
# vidéo, ou le conteneur OCR. Sur une mémoire unifiée, l'OOM-killer vise le plus
# gros RSS, c'est-à-dire le modèle servi. Les dimensions se lisent dans l'EN-TÊTE,
# sans décoder : le portail n'a pas Pillow, et décoder pour mesurer serait déjà
# la bombe. Un en-tête illisible est REFUSÉ (fail-closed) : laisser passer ce
# qu'on ne sait pas mesurer n'annulerait pas seulement la garde, il suffirait
# d'abîmer l'en-tête pour la contourner.
MAX_IMAGE_PIXELS = 40_000_000  # ~40 Mpx


def _dimensions_image(data, mime):
    """(largeur, hauteur) lues dans l'en-tête PNG/JPEG/WebP, ou None."""
    try:
        if mime == 'image/png':
            # Signature puis chunk IHDR : largeur/hauteur en u32 big-endian.
            if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
                return None
            return struct.unpack('>II', data[16:24])
        if mime == 'image/jpeg':
            # Parcours des segments jusqu'au marqueur SOF (0xC0-0xCF, sauf DHT,
            # JPG et DAC qui partagent le préfixe).
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
                if marqueur == 0xDA:  # début des données : plus aucun SOF à venir
                    return None
                i += 2 + taille
            return None
        if mime == 'image/webp':
            if data[:4] != b'RIFF' or data[8:12] != b'WEBP':
                return None
            format_webp = data[12:16]
            if format_webp == b'VP8X':      # dimensions 24 bits, moins un
                return (int.from_bytes(data[24:27], 'little') + 1,
                        int.from_bytes(data[27:30], 'little') + 1)
            if format_webp == b'VP8 ':      # lossy : 14 bits chacune
                return (struct.unpack('<H', data[26:28])[0] & 0x3FFF,
                        struct.unpack('<H', data[28:30])[0] & 0x3FFF)
            if format_webp == b'VP8L':      # lossless : 14 bits empaquetés
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
# Utilisees par le playground, le support et l'OCR : elles doivent vivre hors
# du monolithe pour qu'un blueprint puisse les importer.

def _sse_msg(text):
    """A single SSE 'content' message + end of stream (safe JSON escaping)."""
    payload = json.dumps({'choices': [{'delta': {'content': text}}]})
    return f"data: {payload}\n\ndata: [DONE]\n\n"


def _sse_notice(nid, **args):
    """Notice système STRUCTURÉE (ex. quota dépassé) : le frontend la traduit
    dans la langue de l'interface — le serveur n'écrit jamais de phrase."""
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
