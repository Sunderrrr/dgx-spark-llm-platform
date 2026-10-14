"""ComfyUI client: MiniMax H3 video generation and file retrieval.

Extracted from app.py on 28/08. Section taken first because it was the most
independent of the monolith: its only dependency was COMFYUI_URL, which comes
from the environment — so this module re-imports NOTHING from app.py, and no
import cycle is possible.

app.py keeps the /api/video/* routes; only the ComfyUI mechanics live here.
"""
import json
import os
import re
import secrets
import threading
import time

import requests

COMFYUI_URL = os.environ.get('COMFYUI_URL', 'http://host.docker.internal:8188')

# Never exposed (ComfyUI listens on 127.0.0.1 on the host): only this backend
# talks to it, going through host.docker.internal like the vLLM runner.
_H3_R2V_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'workflows', 'h3_r2v_template.json')
# T2V (text only, no reference image): same CLIP/VAE as R2V, only the
# UNET checkpoint differs (minimax_h3_fl2va_* instead of *_ref2va_*) — derived
# from the official Comfy-Org template (MiniMaxH3ImageToVideo, first_frame/last_frame
# left unconnected), manually validated on 05/08.
_H3_T2V_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'workflows', 'h3_t2v_template.json')

def _comfyui_upload_image(image_bytes, filename):
    try:
        r = requests.post(f"{COMFYUI_URL}/upload/image",
                          files={'image': (filename, image_bytes)},
                          data={'type': 'input'}, timeout=30)
        if r.ok:
            return r.json().get('name')
    except Exception:
        pass
    return None

def comfyui_generate(image_bytes, prompt_text, duration_seconds=5):
    """Submit an H3 video generation to ComfyUI. Returns prompt_id or None.

    image_bytes is optional: None → text only (T2V, workflows/h3_t2v_template.json),
    provided → reference image (R2V, workflows/h3_r2v_template.json). Both
    graphs derive from the official Comfy-Org workflow (manually validated);
    only a few fields are substituted (image, prompt, duration, seed).
    """
    is_t2v = image_bytes is None
    template_path = _H3_T2V_TEMPLATE_PATH if is_t2v else _H3_R2V_TEMPLATE_PATH
    if not is_t2v:
        uploaded_name = _comfyui_upload_image(image_bytes, 'ref.png')
        if not uploaded_name:
            return None
    try:
        with open(template_path) as f:
            graph = json.load(f)
        if not is_t2v:
            graph['137']['inputs']['image'] = uploaded_name
        graph['138']['inputs']['value'] = prompt_text[:10000]
        graph['132']['inputs']['value'] = max(2, min(15, float(duration_seconds)))
        graph['129']['inputs']['noise_seed'] = secrets.randbelow(2**32)
        r = requests.post(f"{COMFYUI_URL}/prompt", json={'prompt': graph}, timeout=15)
        if r.ok:
            return r.json().get('prompt_id')
    except Exception:
        pass
    return None

def comfyui_status(prompt_id):
    """Returns {'status': 'pending'|'running'|'done'|'error', 'video_path': str|None}.

    Real shape of a /history/<id> entry (verified on a full generation):
      {"status": {"status_str": "success"|"error", "completed": bool, "messages": [...]},
       "outputs": {"92": {"images": [{"filename", "subfolder", "type"}], "animated": [true]}}}
    The SaveVideo node stores its file under the historical key "images".
    """
    try:
        r = requests.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=5)
        if r.ok:
            hist = r.json()
            if prompt_id in hist:
                entry = hist[prompt_id]
                if entry.get('status', {}).get('status_str') == 'error':
                    return {'status': 'error', 'video_path': None}
                videos = entry.get('outputs', {}).get('92', {}).get('images') or []
                if videos:
                    v = videos[0]
                    return {'status': 'done',
                            'video_path': v.get('filename'),
                            'video_subfolder': v.get('subfolder', ''),
                            'video_type': v.get('type', 'output')}
                return {'status': 'error', 'video_path': None}
        # not yet in the history → in progress or waiting in the queue
        rq = requests.get(f"{COMFYUI_URL}/queue", timeout=5)
        if rq.ok:
            q = rq.json()
            running_ids = [item[1] for item in q.get('queue_running', [])]
            pending_ids = [item[1] for item in q.get('queue_pending', [])]
            if prompt_id in running_ids:
                return {'status': 'running', 'video_path': None}
            if prompt_id in pending_ids:
                return {'status': 'pending', 'video_path': None}
    except Exception:
        pass
    return {'status': 'error', 'video_path': None}

def comfyui_cancel(prompt_id):
    """Cancels a video generation. Removes the request from the queue if it is
    waiting, otherwise interrupts the ongoing generation. ComfyUI serializes
    renders: if this prompt is the only 'running' one, /interrupt indeed
    cancels it; if queued, we delete it by id (scoped, no side effect)."""
    try:
        rq = requests.get(f"{COMFYUI_URL}/queue", timeout=5)
        if rq.ok:
            q = rq.json()
            running_ids = [i[1] for i in q.get('queue_running', [])]
            pending_ids = [i[1] for i in q.get('queue_pending', [])]
            if prompt_id in pending_ids:
                requests.post(f"{COMFYUI_URL}/queue", json={'delete': [prompt_id]}, timeout=10)
                return True
            if prompt_id in running_ids:
                requests.post(f"{COMFYUI_URL}/interrupt", timeout=10)
                return True
    except Exception:
        pass
    return False

def comfyui_fetch_video(filename, subfolder='', ftype='output'):
    try:
        r = requests.get(f"{COMFYUI_URL}/view",
                         params={'filename': filename, 'subfolder': subfolder, 'type': ftype},
                         timeout=30, stream=True)
        if r.ok:
            return r
    except Exception:
        pass
    return None

# Generated MP4s are cached into the portal's own volume so past videos stay
# viewable even when the ComfyUI video sidecar is stopped (on unified memory the
# video backend is often stopped to free the GPU). ComfyUI's /view only answers
# while its process is up, so relying on it alone made the history unusable at rest.
VIDEO_FILES_DIR = '/app/data/video_files'
# ComfyUI's own output directory, bind-mounted read-only (docker-compose): lets us
# serve past videos straight from disk when the ComfyUI process is stopped.
COMFYUI_OUTPUT_DIR = os.environ.get('COMFYUI_OUTPUT_DIR', '/comfyui-output')

def _comfyui_output_file(video_path, subfolder=''):
    """Resolve a video file inside the mounted ComfyUI output dir, guarding against
    path traversal. Returns the path if it exists, else None."""
    if not video_path:
        return None
    root = os.path.realpath(COMFYUI_OUTPUT_DIR)
    cand = os.path.realpath(os.path.join(root, subfolder or '', video_path))
    if (cand == root or cand.startswith(root + os.sep)) and os.path.isfile(cand):
        return cand
    return None

def _video_name(prompt_id):
    """The stored file name of a job's video (media_store, kind video_files)."""
    safe = re.sub(r'[^A-Za-z0-9_-]', '', str(prompt_id))
    return safe + '.mp4' if safe else None

def _local_video_path(prompt_id):
    name = _video_name(prompt_id)
    return os.path.join(VIDEO_FILES_DIR, name) if name else None

def _cache_video_local(prompt_id, st):
    """Download the finished MP4 from ComfyUI once and store it (media_store:
    the portal volume or the bucket), so it can be served later without the
    sidecar. Best-effort; returns the stored file NAME if available."""
    import media_store
    name = _video_name(prompt_id)
    if not name:
        return None
    dest = _local_video_path(prompt_id)
    if media_store.exists('video_files', name):
        return name
    if not st or not st.get('video_path'):
        return None
    tmp = dest + '.part'
    try:
        os.makedirs(VIDEO_FILES_DIR, exist_ok=True)
        upstream = comfyui_fetch_video(st['video_path'], st.get('video_subfolder', ''),
                                       st.get('video_type', 'output'))
        if upstream is None:
            return None
        with open(tmp, 'wb') as f:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        if os.path.getsize(tmp) > 0:
            os.replace(tmp, dest)
            # Downloaded to disk first (a stream of unknown length), then
            # moved to the bucket when that is where files go.
            media_store.offload('video_files', name, 'video/mp4')
            return name
    except Exception:
        pass
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
    return None


# Availability probe, brought over from app.py on 28/08: it sat in « Helpers »
# although it queries ComfyUI and nothing else.
_comfyui_up_cache = {'t': 0.0, 'v': False, 'refreshing': False}
_comfyui_up_lock = threading.Lock()


def _probe_comfyui():
    try:
        # 0.3 s, not 3: a ComfyUI that is up but busy — or a relay answering
        # while the engine behind it does not — used to make the whole home
        # page wait 3 s (measured 2026-10-07). Availability matters little
        # enough to accept a false negative; the tool retries at the next call.
        return requests.get(f"{COMFYUI_URL}/system_stats", timeout=0.3).ok
    except Exception:
        return False


def _refresh_comfyui():
    try:
        _comfyui_up_cache.update(t=time.time(), v=_probe_comfyui())
    finally:
        _comfyui_up_cache['refreshing'] = False


def comfyui_is_up():
    """ComfyUI (MiniMax-H3, video generation) serves a single fixed workflow and
    has no /v1/models — we just probe its availability.

    Stale-while-revalidate: once a first answer exists, an expired cache returns
    it at once and refreshes in the background. With video off, the probe is a
    full 0.3 s timeout, and every page asking « which services run? » paid it
    every 5 s per gunicorn worker (measured 2026-10-11: 300 ms of a cold
    /api/home). Only the first call of a worker waits.
    """
    now = time.time()
    if now - _comfyui_up_cache['t'] < 5:
        return _comfyui_up_cache['v']
    if _comfyui_up_cache['t']:
        with _comfyui_up_lock:
            launch = not _comfyui_up_cache['refreshing']
            _comfyui_up_cache['refreshing'] = True
        if launch:
            # Short-lived (one probe): unnamed, unlike the long-lived threads
            # test_regression_guards counts.
            threading.Thread(target=_refresh_comfyui, daemon=True).start()
        return _comfyui_up_cache['v']
    v = _probe_comfyui()
    _comfyui_up_cache.update(t=now, v=v)
    return v
