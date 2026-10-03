"""Image generation (diffusers sidecar) — routes extracted from app.py on 28/08.

Blueprint without url_prefix: the paths stay identical to the character, so
the frontend has nothing to change. See memory_routes.py for the full
reasoning about the endpoints.
"""
import logging
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
from sidecars import mention_memoire_partagee, image_ready

_log = logging.getLogger('app')
from guards import media_block_json, media_job_done, media_job_slot

bp = Blueprint('image', __name__)

# A dedicated containerised sidecar (image-gen/) runs the diffusers
# pipeline; the portal drives it asynchronously (a background thread calls the
# sidecar, saves the PNG, updates the job row) so the UI keeps its polling flow.
IMAGE_FILES_DIR = '/app/data/image_files'
IMAGE_HISTORY_LIMIT = 20
IMAGE_MAX_BATCH = 4  # max variations generated per prompt (sequential on unified memory)

# Accepted output formats. « jpg » is an alias normalized to « jpeg »; the
# sidecar receives the canonical key, we store/serve by the real extension.
IMAGE_FORMATS = {'png': 'png', 'jpg': 'jpeg', 'jpeg': 'jpeg', 'webp': 'webp'}
IMAGE_EXT = {'png': 'png', 'jpeg': 'jpg', 'webp': 'webp'}
IMAGE_MIME = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp'}


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
    """True if the user asked to stop this job (« Arrêter » button)."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        row = c.execute("SELECT status FROM image_jobs WHERE prompt_id=? AND username=?",
                        (prompt_id, username)).fetchone()
        c.close()
        return bool(row and row['status'] == 'cancelled')
    except Exception:
        return False


def _image_worker(prompt_id, username, prompt_text, count, fmt='png',
                  width=1024, height=1024, out_width=0, out_height=0):
    """Background thread: call the sidecar `count` times (sequentially — one image
    at a time keeps the GPU memory spike at single-image level on unified memory),
    saving each as <prompt_id>_<idx>.<ext>. Each call reseeds implicitly, so the N
    images are variations of the same prompt. Cancellation is cooperative: the
    « cancelled » flag is checked before each image — the current image
    finishes, the rest of the batch is interrupted and the GPU slot is freed
    by the finally on the generate side."""
    started = datetime.now()
    done = 0
    ext = IMAGE_EXT.get(fmt, 'png')
    os.makedirs(IMAGE_FILES_DIR, exist_ok=True)
    for idx in range(count):
        if _image_cancelled(prompt_id, username):
            break
        try:
            r = requests.post(f"{IMAGE_URL}/generate",
                              data={'prompt': prompt_text[:10000], 'format': fmt,
                                    'width': width, 'height': height,
                                    'out_width': out_width, 'out_height': out_height},
                              timeout=600)
            if r.ok and r.headers.get('Content-Type', '').startswith('image/'):
                with open(os.path.join(IMAGE_FILES_DIR, f"{prompt_id}_{idx}.{ext}"), 'wb') as f:
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
    refus = media_block_json()
    if refus:
        return refus
    prompt_text = request.form.get('prompt', '').strip()
    if not prompt_text:
        return jsonify({'error': "Un prompt texte est requis."}), 400
    if not image_ready():
        return jsonify({'error': "Aucun modèle image configuré." + mention_memoire_partagee()}), 503
    # Batch size: 1–4 variations per prompt (generated sequentially).
    try:
        count = int(request.form.get('count', 1))
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(IMAGE_MAX_BATCH, count))
    # Output format: normalized (jpg alias -> jpeg), default png. The sidecar
    # encodes this format and the portal stores/serves the matching extension.
    fmt = IMAGE_FORMATS.get((request.form.get('format', 'png') or 'png').strip().lower(), 'png')
    # Resolution: native generation (multiple of 8, capped like the sidecar) and
    # optional output size (Lanczos upscale on the sidecar side if larger).
    def _dim(key, default, lo, hi):
        try:
            v = int(request.form.get(key) or default)
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))
    width = _dim('width', 1024, 256, 1536)
    height = _dim('height', 1024, 256, 1536)
    out_width = _dim('out_width', 0, 0, 3840)
    out_height = _dim('out_height', 0, 0, 3840)
    prompt_id = secrets.token_hex(12)
    db = get_db()
    db.execute("INSERT INTO image_jobs (username, prompt_id, prompt, status, count, done_count, format, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (session['username'], prompt_id, prompt_text, 'running', count, 0, fmt, datetime.now().isoformat()))
    db.execute("""DELETE FROM image_jobs WHERE username=? AND id NOT IN (
                     SELECT id FROM image_jobs WHERE username=? ORDER BY id DESC LIMIT ?)""",
               (session['username'], session['username'], IMAGE_HISTORY_LIMIT))
    db.commit()
    username = session['username']
    # Bound the number of in-flight async jobs per account: each spawns a
    # thread that blocks up to 600 s against the shared GPU sidecar.
    if not media_job_slot(username):
        return jsonify({'error': "Trop de générations d'images en cours. Attends la fin des précédentes."}), 429
    def _run(u=username, pid=prompt_id, pt=prompt_text, c=count, f=fmt,
             w=width, h=height, ow=out_width, oh=out_height):
        try:
            _image_worker(pid, u, pt, c, f, w, h, ow, oh)
        finally:
            media_job_done(u)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'prompt_id': prompt_id, 'count': count})


@bp.route('/api/image/history')
@login_required
def api_image_history():
    rows = get_db().execute(
        "SELECT prompt_id, prompt, status, count, done_count, format, created_at FROM image_jobs WHERE username=? ORDER BY id DESC",
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
    """Ask to stop an image generation (cooperative: the current image finishes,
    the rest of the batch is interrupted). Only affects one's own jobs."""
    db = get_db()
    row = db.execute(
        "SELECT status FROM image_jobs WHERE prompt_id=? AND username=?",
        (prompt_id, session['username'])).fetchone()
    if not row:
        abort(404)
    if row['status'] in ('done', 'cancelled'):
        return jsonify({'ok': True})  # already finished: no-op
    if row['status'] != 'running':
        return jsonify({'error': "Ce job n'est plus actif."}), 400
    db.execute("UPDATE image_jobs SET status='cancelled' WHERE prompt_id=? AND username=?",
               (prompt_id, session['username']))
    db.commit()
    return jsonify({'ok': True})


@bp.route('/api/image/delete/<prompt_id>/<int:idx>', methods=['POST'])
@login_required
def api_image_delete(prompt_id, idx):
    """Deletes ONE image from a batch (gallery). The job (and history) stays:
    only the thumbnail is removed from disk."""
    owned = get_db().execute(
        "SELECT 1 FROM image_jobs WHERE prompt_id=? AND username=?",
        (prompt_id, session['username'])).fetchone()
    if not owned:
        abort(404)
    safe = re.sub(r'[^a-f0-9]', '', str(prompt_id))
    if not safe:
        abort(404)
    idx = max(0, min(IMAGE_MAX_BATCH - 1, int(idx)))
    # The format varies from job to job: we look for the file under any known
    # extension before removing it.
    for ext in IMAGE_EXT.values():
        path = os.path.join(IMAGE_FILES_DIR, f"{safe}_{idx}.{ext}")
        if os.path.isfile(path):
            try:
                os.remove(path)
            except Exception as exc:
                # `str(exc)` of an `os.remove` carries the ABSOLUTE path of the volume
                # (internal tree) and leaked into the HTTP response. The log
                # keeps the detail, the user gets one sentence.
                _log.warning("suppression image %s/%s échouée : %r", safe, idx, exc)
                return jsonify({'error': "Suppression impossible."}), 500
            return jsonify({'ok': True})
    return jsonify({'ok': True})


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
    # The format (and thus the extension) varies from job to job: we look for the
    # file under each known extension, then serve the right Content-Type.
    path = None
    for ext in IMAGE_EXT.values():
        cand = os.path.join(IMAGE_FILES_DIR, f"{safe}_{idx}.{ext}")
        if os.path.isfile(cand):
            path = cand
            break
    # Backward-compat: jobs made before batching saved a single <prompt_id>.png.
    if path is None and idx == 0:
        legacy = os.path.join(IMAGE_FILES_DIR, safe + '.png')
        if os.path.isfile(legacy):
            path = legacy
    if path is None:
        abort(404)
    mime = IMAGE_MIME.get(os.path.splitext(path)[1].lstrip('.').lower(), 'image/png')
    return send_file(path, mimetype=mime)
