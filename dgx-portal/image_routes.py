"""Generation d'image (sidecar diffusers) — routes extraites de app.py le 28/08.

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
from db import DB_PATH, add_notification, get_db
from config import IMAGE_URL
from sidecars import get_image_model, image_ready
from guards import (maintenance_block_json, media_job_done, media_job_slot,
                    media_rate_block)

bp = Blueprint('image', __name__)

# A dedicated containerised sidecar (image-gen/) runs the diffusers
# pipeline; the portal drives it asynchronously (a background thread calls the
# sidecar, saves the PNG, updates the job row) so the UI keeps its polling flow.
IMAGE_FILES_DIR = '/app/data/image_files'
IMAGE_HISTORY_LIMIT = 20
IMAGE_MAX_BATCH = 4  # max variations generated per prompt (sequential on unified memory)


def _image_set_done(prompt_id, username, done):
    """Bump the produced-so-far counter so the page can show images as they land."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("UPDATE image_jobs SET done_count=? WHERE prompt_id=? AND username=?",
                  (done, prompt_id, username))
        c.commit(); c.close()
    except Exception:
        pass

def _image_cancelled(prompt_id, username):
    """True si l'utilisateur a demandé l'arrêt de ce job (bouton « Arrêter »)."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        row = c.execute("SELECT status FROM image_jobs WHERE prompt_id=? AND username=?",
                        (prompt_id, username)).fetchone()
        c.close()
        return bool(row and row['status'] == 'cancelled')
    except Exception:
        return False


def _image_worker(prompt_id, username, prompt_text, count):
    """Background thread: call the sidecar `count` times (sequentially — one image
    at a time keeps the GPU memory spike at single-image level on unified memory),
    saving each as <prompt_id>_<idx>.png. Each call reseeds implicitly, so the N
    images are variations of the same prompt. L'annulation est coopérative : on
    vérifie le drapeau « cancelled » avant chaque image — l'image en cours se
    termine, la suite du lot est interrompue et le slot GPU est libéré par le
    finally côté generate."""
    started = datetime.now()
    done = 0
    os.makedirs(IMAGE_FILES_DIR, exist_ok=True)
    for idx in range(count):
        if _image_cancelled(prompt_id, username):
            break
        try:
            r = requests.post(f"{IMAGE_URL}/generate", data={'prompt': prompt_text[:10000]}, timeout=600)
            if r.ok and r.headers.get('Content-Type', '').startswith('image/'):
                with open(os.path.join(IMAGE_FILES_DIR, f"{prompt_id}_{idx}.png"), 'wb') as f:
                    f.write(r.content)
                done += 1
                _image_set_done(prompt_id, username, done)
        except Exception:
            pass
    cancelled = _image_cancelled(prompt_id, username)
    status = 'cancelled' if cancelled else ('done' if done else 'error')
    dur = int((datetime.now() - started).total_seconds() * 1000) if done else None
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("UPDATE image_jobs SET status=?, duration_ms=?, done_count=? WHERE prompt_id=? AND username=?",
                  (status, dur, done, prompt_id, username))
        c.commit(); c.close()
    except Exception:
        pass
    if not cancelled:
        add_notification(username, 'image',
                         'Génération image terminée ({}/{}).'.format(done or 0, count)
                         if done else 'Échec de la génération image.')


@bp.route('/api/image/generate', methods=['POST'])
@login_required
def api_image_generate():
    blocked = maintenance_block_json()
    if blocked:
        return blocked
    limited = media_rate_block()
    if limited:
        return limited
    prompt_text = request.form.get('prompt', '').strip()
    if not prompt_text:
        return jsonify({'error': "Un prompt texte est requis."}), 400
    if not image_ready():
        return jsonify({'error': "Aucun modèle image configuré."}), 503
    # Batch size: 1–4 variations per prompt (generated sequentially).
    try:
        count = int(request.form.get('count', 1))
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(IMAGE_MAX_BATCH, count))
    prompt_id = secrets.token_hex(12)
    db = get_db()
    db.execute("INSERT INTO image_jobs (username, prompt_id, prompt, status, count, done_count, created_at) VALUES (?,?,?,?,?,?,?)",
               (session['username'], prompt_id, prompt_text, 'running', count, 0, datetime.now().isoformat()))
    db.execute("""DELETE FROM image_jobs WHERE username=? AND id NOT IN (
                     SELECT id FROM image_jobs WHERE username=? ORDER BY id DESC LIMIT ?)""",
               (session['username'], session['username'], IMAGE_HISTORY_LIMIT))
    db.commit()
    username = session['username']
    # Bound the number of in-flight async jobs per account: each spawns a
    # thread that blocks up to 600 s against the shared GPU sidecar.
    if not media_job_slot(username):
        return jsonify({'error': "Trop de générations d'images en cours. Attends la fin des précédentes."}), 429
    def _run(u=username, pid=prompt_id, pt=prompt_text, c=count):
        try:
            _image_worker(pid, u, pt, c)
        finally:
            media_job_done(u)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'prompt_id': prompt_id, 'count': count})


@bp.route('/api/image/history')
@login_required
def api_image_history():
    rows = get_db().execute(
        "SELECT prompt_id, prompt, status, count, done_count, created_at FROM image_jobs WHERE username=? ORDER BY id DESC",
        (session['username'],)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/image/status/<prompt_id>')
@login_required
def api_image_status(prompt_id):
    row = get_db().execute(
        "SELECT status, count, done_count FROM image_jobs WHERE prompt_id=? AND username=?",
        (prompt_id, session['username'])).fetchone()
    if not row:
        abort(404)
    return jsonify({'status': row['status'], 'count': row['count'], 'done_count': row['done_count']})


@bp.route('/api/image/cancel/<prompt_id>', methods=['POST'])
@login_required
def api_image_cancel(prompt_id):
    """Demande l'arrêt d'une génération image (coopératif : l'image en cours se
    termine, la suite du lot est interrompue). Ne concerne que ses propres jobs."""
    db = get_db()
    row = db.execute(
        "SELECT status FROM image_jobs WHERE prompt_id=? AND username=?",
        (prompt_id, session['username'])).fetchone()
    if not row:
        abort(404)
    if row['status'] in ('done', 'cancelled'):
        return jsonify({'ok': True})  # déjà terminé : no-op
    if row['status'] != 'running':
        return jsonify({'error': "Ce job n'est plus actif."}), 400
    db.execute("UPDATE image_jobs SET status='cancelled' WHERE prompt_id=? AND username=?",
               (prompt_id, session['username']))
    db.commit()
    return jsonify({'ok': True})


@bp.route('/api/image/delete/<prompt_id>/<int:idx>', methods=['POST'])
@login_required
def api_image_delete(prompt_id, idx):
    """Supprime UNE image d'un lot (galerie). Le job (et l'historique) reste :
    seule la vignette est retirée du disque."""
    owned = get_db().execute(
        "SELECT 1 FROM image_jobs WHERE prompt_id=? AND username=?",
        (prompt_id, session['username'])).fetchone()
    if not owned:
        abort(404)
    path = os.path.join(IMAGE_FILES_DIR, f"{prompt_id}_{idx}.png")
    try:
        if os.path.exists(path):
            os.remove(path)
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@bp.route('/image/file/<prompt_id>')
@bp.route('/image/file/<prompt_id>/<int:idx>')
@login_required
def image_file(prompt_id, idx=0):
    owned = get_db().execute(
        "SELECT 1 FROM image_jobs WHERE prompt_id=? AND username=?",
        (prompt_id, session['username'])).fetchone()
    if not owned:
        abort(404)
    safe = re.sub(r'[^a-f0-9]', '', str(prompt_id))
    if not safe:
        abort(404)
    idx = max(0, min(IMAGE_MAX_BATCH - 1, int(idx)))
    path = os.path.join(IMAGE_FILES_DIR, f"{safe}_{idx}.png")
    # Backward-compat: jobs made before batching saved a single <prompt_id>.png.
    if not os.path.isfile(path) and idx == 0:
        legacy = os.path.join(IMAGE_FILES_DIR, safe + '.png')
        if os.path.isfile(legacy):
            path = legacy
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype='image/png')
