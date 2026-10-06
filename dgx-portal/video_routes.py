"""Video (MiniMax H3 via ComfyUI) — routes extracted from app.py on 28/08.

Blueprint without url_prefix: the paths stay identical to the character, so
the frontend has nothing to change. See memory_routes.py for the full
reasoning about the endpoints.
"""
import os
import sqlite3
from datetime import datetime

from flask import Blueprint, Response, abort, jsonify, request, send_file, session

from auth import login_required
from comfyui_client import (
    _cache_video_local, _comfyui_output_file, _local_video_path,
    comfyui_cancel, comfyui_fetch_video, comfyui_generate, comfyui_status,
)
from db import DB_PATH, get_db
from guards import (
    _read_uploaded_image,
    media_block_json,
)
from sidecars import mention_memoire_partagee

bp = Blueprint('video', __name__)

VIDEO_HISTORY_LIMIT = 10

@bp.route('/api/video/generate', methods=['POST'])
@login_required
def api_video_generate():
    refus = media_block_json()
    if refus:
        return refus
    # Optional image: absent → text-only generation (T2V). Provided but
    # invalid (wrong format/too heavy) → always a 400 error, as
    # before — only the total ABSENCE of the field switches to T2V.
    data = None
    if request.files.get('image') and request.files['image'].filename:
        data, err_or_mime = _read_uploaded_image()
        if data is None:
            return jsonify({'error': err_or_mime}), 400
    prompt_text = request.form.get('prompt', '').strip()
    if not prompt_text:
        return jsonify({'error': "Un prompt texte est requis."}), 400
    try:
        duration = float(request.form.get('duration', 5))
    except ValueError:
        duration = 5
    prompt_id = comfyui_generate(data, prompt_text, duration)
    if not prompt_id:
        # 503 and NOT 502: measured 2026-10-02, Cloudflare's edge REPLACES the
        # body of a 502 with its own error page (« error code: 502 ») — the
        # honest message never reaches the user. The same JSON as 503 passes
        # through intact (verified on this exact route, both ways). Every
        # user-facing "upstream unavailable" answer in the portal uses 503 for
        # that reason.
        return jsonify({'error': "ComfyUI inaccessible ou requête refusée." + mention_memoire_partagee()}), 503
    db = get_db()
    db.execute("INSERT INTO video_jobs (username, prompt_id, prompt, created_at, req_duration_s) VALUES (?,?,?,?,?)",
               (session['username'], prompt_id, prompt_text, datetime.now().isoformat(), int(duration)))
    # Keeps only the VIDEO_HISTORY_LIMIT most recent per user.
    # The FILES go with the rows (audit of 2026-10-02): the table was purged, the
    # disk never, and `video_files` was swept by NO backup script (ORPHAN_DIRS
    # named only image and music). A job leaves 10 to 100 Mo, never reclaimed, in
    # the volume that also holds the portal database — the only bound was
    # therefore disk size. We collect the identifiers BEFORE the DELETE, then
    # erase the local cache. The orphan sweep (with its 7-day grace) remains the
    # safety net.
    evinces = [r['prompt_id'] for r in db.execute(
        """SELECT prompt_id FROM video_jobs WHERE username=? AND id NOT IN (
                     SELECT id FROM video_jobs WHERE username=?
                     ORDER BY id DESC LIMIT ?)""",
        (session['username'], session['username'], VIDEO_HISTORY_LIMIT)).fetchall()]
    db.execute("""DELETE FROM video_jobs WHERE username=? AND id NOT IN (
                     SELECT id FROM video_jobs WHERE username=?
                     ORDER BY id DESC LIMIT ?)""",
               (session['username'], session['username'], VIDEO_HISTORY_LIMIT))
    db.commit()
    for ancien in evinces:
        chemin = _local_video_path(ancien)
        try:
            if chemin and os.path.isfile(chemin):
                os.remove(chemin)
        except OSError:
            # A file we fail to delete must not fail the ongoing generation: the orphan
            # sweep will pick it up again.
            pass
    return jsonify({'prompt_id': prompt_id})

@bp.route('/api/video/history')
@login_required
def api_video_history():
    rows = get_db().execute(
        "SELECT prompt_id, prompt, status, created_at FROM video_jobs WHERE username=? ORDER BY id DESC",
        (session['username'],)).fetchall()
    return jsonify([dict(r) for r in rows])

def _persister_video(prompt_id, username, st, cree_le=None, duree_ms=None):
    """Writes a KNOWN ComfyUI result into video_jobs and caches the MP4 locally.

    ComfyUI's in-memory history is volatile (cleared on each service restart),
    whereas /view reads the file directly from disk — by keeping the path here,
    the history stays viewable even after a ComfyUI restart.

    Shared by this page's polling route and by the `generer_video` playground
    tool's worker (video_tools._video_worker): one job ending, handled one way.
    `cree_le`/`duree_ms` are those of the row ALREADY read by the caller (they
    are not touched by the UPDATE): the generation duration is written ONCE, on
    the first « done » — the `duration_ms IS NULL` guard keeps a second writer
    from doubling it.

    A bare `sqlite3.connect` and not `get_db()`: the worker runs in a daemon
    THREAD, with no Flask application context — `get_db()` would raise there
    without leaving a trace (the same trap as image_routes._image_worker).
    """
    if st['status'] not in ('done', 'error'):
        return
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.execute(
        "UPDATE video_jobs SET status=?, video_path=?, video_subfolder=?, video_type=? "
        "WHERE prompt_id=? AND username=?",
        (st['status'], st.get('video_path'), st.get('video_subfolder'), st.get('video_type'),
         prompt_id, username))
    # Generation duration = time elapsed since creation. Approx. to the polling
    # period (~5 s), which is negligible on a several-minute generation.
    if st['status'] == 'done' and duree_ms is None and cree_le:
        try:
            dur = int((datetime.now() - datetime.fromisoformat(cree_le)).total_seconds() * 1000)
            if 0 < dur < 3600000:  # safety bound (< 1 h)
                db.execute(
                    "UPDATE video_jobs SET duration_ms=? WHERE prompt_id=? AND username=? AND duration_ms IS NULL",
                    (dur, prompt_id, username))
        except Exception:
            pass
    db.commit()
    db.close()
    # Cache the MP4 to the portal volume while ComfyUI is still up, so it
    # stays viewable after the video sidecar is stopped.
    if st['status'] == 'done':
        _cache_video_local(prompt_id, st)


@bp.route('/api/video/status/<prompt_id>')
@login_required
def api_video_status(prompt_id):
    # IDOR guard: prompt_id is an opaque but non-secret ComfyUI identifier
    # (visible in the DOM/URL) — without this check, any
    # logged-in user could query another's status/video
    # just by knowing their prompt_id.
    owned = get_db().execute(
        "SELECT status, created_at, duration_ms FROM video_jobs "
        "WHERE prompt_id=? AND username=?",
        (prompt_id, session['username'])).fetchone()
    if not owned:
        abort(404)
    # Cancelled → stop polling ComfyUI (the interrupt may mark it 'error').
    if owned['status'] == 'cancelled':
        return jsonify({'status': 'cancelled', 'video_path': None})
    st = comfyui_status(prompt_id)
    # Persists the result as soon as it's known (see _persister_video above).
    _persister_video(prompt_id, session['username'], st,
                     owned['created_at'], owned['duration_ms'])
    return jsonify(st)


@bp.route('/api/video/cancel/<prompt_id>', methods=['POST'])
@login_required
def api_video_cancel(prompt_id):
    """Cancels an ongoing video generation (ComfyUI interrupt) or a pending one
    (removed from the queue). Only affects one's own jobs."""
    db = get_db()
    row = db.execute("SELECT status FROM video_jobs WHERE prompt_id=? AND username=?",
                     (prompt_id, session['username'])).fetchone()
    if not row:
        abort(404)
    if row['status'] in ('done', 'cancelled'):
        return jsonify({'ok': True})
    if row['status'] != 'running':
        return jsonify({'error': "Ce job n'est plus actif."}), 400
    comfyui_cancel(prompt_id)
    db.execute("UPDATE video_jobs SET status='cancelled' WHERE prompt_id=? AND username=?",
               (prompt_id, session['username']))
    db.commit()
    return jsonify({'ok': True})

@bp.route('/video/file/<prompt_id>')
@login_required
def video_file(prompt_id):
    # Same IDOR guard as api_video_status: we first need a row
    # belonging to THIS account for this prompt_id, even when video_path is
    # not yet filled in (job not yet marked "done" in the DB) — before, the
    # fallback on comfyui_status(prompt_id) below wasn't scoped by
    # user and served the video of any job known to ComfyUI.
    owned = get_db().execute(
        "SELECT video_path, video_subfolder, video_type FROM video_jobs "
        "WHERE prompt_id=? AND username=?",
        (prompt_id, session['username'])).fetchone()
    if not owned:
        abort(404)
    # 1) Serve the locally cached copy first — works even when ComfyUI is stopped.
    local = _local_video_path(prompt_id)
    if local and os.path.isfile(local) and os.path.getsize(local) > 0:
        return send_file(local, mimetype='video/mp4')
    # 2) Serve straight from ComfyUI's output dir on disk (read-only mount) — also
    #    works with the ComfyUI process stopped, and covers videos made before the
    #    portal-side cache existed.
    if owned['video_path']:
        disk = _comfyui_output_file(owned['video_path'], owned['video_subfolder'] or '')
        if disk:
            return send_file(disk, mimetype='video/mp4')
    # 3) Otherwise pull it from ComfyUI over HTTP (and cache it for next time).
    if owned['video_path']:
        st = {'video_path': owned['video_path'], 'video_subfolder': owned['video_subfolder'],
              'video_type': owned['video_type']}
    else:
        st = comfyui_status(prompt_id)
        if st['status'] != 'done' or not st['video_path']:
            abort(404)
    cached = _cache_video_local(prompt_id, st)
    if cached:
        return send_file(cached, mimetype='video/mp4')
    upstream = comfyui_fetch_video(st['video_path'], st.get('video_subfolder', ''),
                                   st.get('video_type', 'output'))
    if upstream is None:
        abort(503)
    return Response(upstream.iter_content(chunk_size=65536), mimetype='video/mp4',
                    # The name comes from ComfyUI: it does NOT go in the header (a
                # quote breaks the string, a CRLF causes a 500). The browser names
                # the file from the URL.
                headers={'Content-Disposition': 'inline'})
