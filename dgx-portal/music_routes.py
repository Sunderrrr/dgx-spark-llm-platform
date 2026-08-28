"""Musique (sidecar diffusers) — routes extraites de app.py le 28/08.

Blueprint sans url_prefix : les chemins restent identiques au caractere pres,
donc le frontend n'a rien a changer. Cf. memory_routes.py pour le raisonnement
complet sur les endpoints.
"""
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime

import requests
from flask import Blueprint, abort, jsonify, request, send_file, session

from auth import login_required
from config import MUSIC_URL
from db import DB_PATH, get_db
from guards import maintenance_block_json, media_rate_block

bp = Blueprint('music', __name__)

MUSIC_FILES_DIR = '/app/data/music_files'
MUSIC_HISTORY_LIMIT = 20
MUSIC_MAX_SECONDS = 300
# Plafond volontairement bas : ~4x la durée demandée par version, donc 3 versions
# d'un morceau de 3 min monopolisent déjà le GPU ~35 min.
MUSIC_MAX_BATCH = 3

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

def _music_set_done(job_id, username, done):
    """Incrémente le compteur produit : la page affiche les versions au fur et à
    mesure plutôt qu'à la toute fin du lot."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("UPDATE music_jobs SET done_count=? WHERE job_id=? AND username=?",
                  (done, job_id, username))
        c.commit(); c.close()
    except Exception:
        pass

def _music_worker(job_id, username, prompt, lyrics, duration, count):
    """Thread : appelle le sidecar `count` fois, écrit un WAV par version.

    Séquentiel : le sidecar sérialise déjà les générations derrière son verrou
    GPU, et enchaîner en parallèle ne ferait qu'ajouter de l'attente. Chaque
    appel repart d'une graine différente → autant de variantes du même morceau.
    Génération longue (~4x la durée demandée) → timeout large, et le portail ne
    bloque pas la requête de l'utilisateur : la page interroge /status.
    """
    started = datetime.now()
    done = 0
    os.makedirs(MUSIC_FILES_DIR, exist_ok=True)
    for idx in range(count):
        try:
            r = requests.post(f"{MUSIC_URL}/generate",
                              data={'prompt': prompt, 'lyrics': lyrics, 'duration': duration},
                              timeout=1800)
            if r.ok and r.headers.get('Content-Type', '').startswith('audio/'):
                with open(os.path.join(MUSIC_FILES_DIR, f"{job_id}_{idx}.wav"), 'wb') as f:
                    f.write(r.content)
                done += 1
                _music_set_done(job_id, username, done)
        except Exception:
            pass
    status = 'done' if done else 'error'
    dur = int((datetime.now() - started).total_seconds() * 1000) if done else None
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("UPDATE music_jobs SET status=?, duration_ms=?, done_count=? WHERE job_id=? AND username=?",
                  (status, dur, done, job_id, username))
        c.commit(); c.close()
    except Exception:
        pass


@bp.route('/api/music/generate', methods=['POST'])
@login_required
def api_music_generate():
    blocked = maintenance_block_json()
    if blocked:
        return blocked
    limited = media_rate_block()
    if limited:
        return limited
    prompt = request.form.get('prompt', '').strip()
    if not prompt:
        return jsonify({'error': "Une description musicale est requise."}), 400
    if not music_ready():
        return jsonify({'error': "Aucun modèle musique configuré."}), 503
    lyrics = request.form.get('lyrics', '')[:10000]
    try:
        duration = int(float(request.form.get('duration', 60)))
    except (TypeError, ValueError):
        duration = 60
    duration = max(5, min(MUSIC_MAX_SECONDS, duration))
    try:
        count = int(request.form.get('count', 1))
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(MUSIC_MAX_BATCH, count))
    job_id = secrets.token_hex(12)
    db = get_db()
    db.execute("INSERT INTO music_jobs (username, job_id, prompt, lyrics, duration_s, status, count, done_count, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (session['username'], job_id, prompt, lyrics, duration, 'running', count, 0, datetime.now().isoformat()))
    db.execute("""DELETE FROM music_jobs WHERE username=? AND id NOT IN (
                     SELECT id FROM music_jobs WHERE username=? ORDER BY id DESC LIMIT ?)""",
               (session['username'], session['username'], MUSIC_HISTORY_LIMIT))
    db.commit()
    threading.Thread(target=_music_worker,
                     args=(job_id, session['username'], prompt, lyrics, duration, count),
                     daemon=True).start()
    return jsonify({'job_id': job_id, 'duration': duration, 'count': count})


@bp.route('/api/music/history')
@login_required
def api_music_history():
    rows = get_db().execute(
        "SELECT job_id, prompt, lyrics, duration_s, status, count, done_count, created_at FROM music_jobs "
        "WHERE username=? ORDER BY id DESC", (session['username'],)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/music/status/<job_id>')
@login_required
def api_music_status(job_id):
    row = get_db().execute("SELECT status, count, done_count FROM music_jobs WHERE job_id=? AND username=?",
                           (job_id, session['username'])).fetchone()
    if not row:
        abort(404)
    return jsonify({'status': row['status'], 'count': row['count'], 'done_count': row['done_count']})


@bp.route('/music/file/<job_id>')
@bp.route('/music/file/<job_id>/<int:idx>')
@login_required
def music_file(job_id, idx=0):
    # Scope (id, username) en une requête — même garde IDOR que /voice/audio.
    owned = get_db().execute("SELECT 1 FROM music_jobs WHERE job_id=? AND username=?",
                             (job_id, session['username'])).fetchone()
    if not owned:
        abort(404)
    safe = re.sub(r'[^a-f0-9]', '', str(job_id))
    if not safe:
        abort(404)
    idx = max(0, min(MUSIC_MAX_BATCH - 1, int(idx)))
    path = os.path.join(MUSIC_FILES_DIR, f"{safe}_{idx}.wav")
    # Compat : les morceaux d'avant le multi-version sont en <job_id>.wav.
    if not os.path.isfile(path) and idx == 0:
        legacy = os.path.join(MUSIC_FILES_DIR, safe + '.wav')
        if os.path.isfile(legacy):
            path = legacy
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype='audio/wav')
