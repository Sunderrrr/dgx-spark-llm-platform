"""Video (MiniMax H3 via ComfyUI) — routes extraites de app.py le 28/08.

Blueprint sans url_prefix : les chemins restent identiques au caractere pres,
donc le frontend n'a rien a changer. Cf. memory_routes.py pour le raisonnement
complet sur les endpoints.
"""
import os
from datetime import datetime

from flask import Blueprint, Response, abort, jsonify, request, send_file, session

from auth import login_required
from comfyui_client import (
    _cache_video_local, _comfyui_output_file, _local_video_path,
    comfyui_fetch_video, comfyui_generate, comfyui_status,
)
from db import get_db
from guards import (
    _ALLOWED_IMAGE_TYPES, _MAX_UPLOAD_BYTES, _read_uploaded_image,
    maintenance_block_json, media_rate_block,
)

bp = Blueprint('video', __name__)

VIDEO_HISTORY_LIMIT = 10

@bp.route('/api/video/generate', methods=['POST'])
@login_required
def api_video_generate():
    blocked = maintenance_block_json()
    if blocked:
        return blocked
    limited = media_rate_block()
    if limited:
        return limited
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
        return jsonify({'error': "ComfyUI inaccessible ou requête refusée."}), 502
    db = get_db()
    db.execute("INSERT INTO video_jobs (username, prompt_id, prompt, created_at, req_duration_s) VALUES (?,?,?,?,?)",
               (session['username'], prompt_id, prompt_text, datetime.now().isoformat(), int(duration)))
    # Keeps only the VIDEO_HISTORY_LIMIT most recent per user.
    db.execute("""DELETE FROM video_jobs WHERE username=? AND id NOT IN (
                     SELECT id FROM video_jobs WHERE username=?
                     ORDER BY id DESC LIMIT ?)""",
               (session['username'], session['username'], VIDEO_HISTORY_LIMIT))
    db.commit()
    return jsonify({'prompt_id': prompt_id})

@bp.route('/api/video/history')
@login_required
def api_video_history():
    rows = get_db().execute(
        "SELECT prompt_id, prompt, status, created_at FROM video_jobs WHERE username=? ORDER BY id DESC",
        (session['username'],)).fetchall()
    return jsonify([dict(r) for r in rows])

@bp.route('/api/video/status/<prompt_id>')
@login_required
def api_video_status(prompt_id):
    # IDOR guard: prompt_id is an opaque but non-secret ComfyUI identifier
    # (visible in the DOM/URL) — without this check, any
    # logged-in user could query another's status/video
    # just by knowing their prompt_id.
    owned = get_db().execute(
        "SELECT 1 FROM video_jobs WHERE prompt_id=? AND username=?",
        (prompt_id, session['username'])).fetchone()
    if not owned:
        abort(404)
    st = comfyui_status(prompt_id)
    # Persists the result as soon as it's known: ComfyUI's in-memory
    # history is volatile (cleared on each service restart), whereas
    # /view reads the file directly from disk — by keeping the path here,
    # the history stays viewable even after a ComfyUI restart.
    if st['status'] in ('done', 'error'):
        get_db().execute(
            "UPDATE video_jobs SET status=?, video_path=?, video_subfolder=?, video_type=? "
            "WHERE prompt_id=? AND username=?",
            (st['status'], st.get('video_path'), st.get('video_subfolder'), st.get('video_type'),
             prompt_id, session['username']))
        # Generation duration = time elapsed since creation, set ONCE
        # (on the first "done"). Approx. to the polling period (~5 s), which
        # is negligible on a several-minute generation.
        if st['status'] == 'done':
            row = get_db().execute(
                "SELECT created_at, duration_ms FROM video_jobs WHERE prompt_id=? AND username=?",
                (prompt_id, session['username'])).fetchone()
            if row and row['duration_ms'] is None and row['created_at']:
                try:
                    dur = int((datetime.now() - datetime.fromisoformat(row['created_at'])).total_seconds() * 1000)
                    if 0 < dur < 3600000:  # safety bound (< 1 h)
                        get_db().execute(
                            "UPDATE video_jobs SET duration_ms=? WHERE prompt_id=? AND username=? AND duration_ms IS NULL",
                            (dur, prompt_id, session['username']))
                except Exception:
                    pass
        get_db().commit()
        # Cache the MP4 to the portal volume while ComfyUI is still up, so it
        # stays viewable after the video sidecar is stopped.
        if st['status'] == 'done':
            _cache_video_local(prompt_id, st)
    return jsonify(st)

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
        abort(502)
    return Response(upstream.iter_content(chunk_size=65536), mimetype='video/mp4',
                    headers={'Content-Disposition': f'inline; filename="{st["video_path"]}"'})
