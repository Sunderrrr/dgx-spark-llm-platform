"""OCR : sonde du modele servi, flux d'extraction et routes.

Extrait de app.py le 28/08. Frontiere redessinee, la aussi : la banniere
« OCR » du monolithe couvrait en realite la sante vLLM, les notifications mail
et la recherche HuggingFace — seul ocr_extract_stream y etait de l'OCR. On n'a
donc pris que ce qui l'est vraiment, plutot que de deplacer la banniere.

get_ocr_model est reimporte par app.py : le tableau de bord des sidecars s'en
sert pour savoir si le conteneur OCR repond.
"""
import base64
import json
import os
import secrets
import time
from datetime import datetime

import requests
from flask import (Blueprint, Response, abort, jsonify, request, send_file,
                   session, stream_with_context)

from auth import login_required
from config import OCR_URL
from db import get_db
from guards import (_chat_rate_limited, _read_uploaded_image, _sse_msg,
                    maintenance_block_sse)

bp = Blueprint('ocr', __name__)


def _sidecar_proc_status_differe(kind):
    """Import DIFFERE de app._sidecar_proc_status, volontairement.

    Cette aide est generique (elle interroge les quatre sidecars via le runner)
    et reste donc dans app.py. L'importer en tete d'ocr_routes.py creerait le
    cycle qu'on evite depuis db.py ; l'importer A L'APPEL ne le cree pas, car
    app.py est entierement charge avant qu'une requete n'arrive. C'est une
    couture assumee, a retirer le jour ou la gestion des sidecars sortira elle
    aussi du monolithe.
    """
    from app import _sidecar_proc_status
    return _sidecar_proc_status(kind)

OCR_HISTORY_LIMIT = 20

_CHANDRA_OCR_LAYOUT_PROMPT = """
OCR this image to HTML, arranged as layout blocks.  Each layout block should be a div with the data-bbox attribute representing the bounding box of the block in x0 y0 x1 y1 format.  Bboxes are normalized 0-1000. The data-label attribute is the label for the block.

Use the following labels:
- Caption
- Footnote
- Equation-Block
- List-Group
- Page-Header
- Page-Footer
- Image
- Section-Header
- Table
- Text
- Complex-Block
- Code-Block
- Form
- Table-Of-Contents
- Figure
- Chemical-Block
- Diagram
- Bibliography
- Blank-Page

Only use these tags ['math', 'br', 'i', 'b', 'u', 'del', 'sup', 'sub', 'table', 'tr', 'td', 'p', 'th', 'div', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'ul', 'ol', 'li', 'input', 'a', 'span', 'img', 'hr', 'tbody', 'small', 'caption', 'strong', 'thead', 'big', 'code', 'chem'], and these attributes ['class', 'colspan', 'rowspan', 'display', 'checked', 'type', 'border', 'value', 'style', 'href', 'alt', 'align', 'data-bbox', 'data-label'].

Guidelines:
* Inline math: Surround math with <math>...</math> tags. Math expressions should be rendered in KaTeX-compatible LaTeX. Use display for block math.
* Tables: Use colspan and rowspan attributes to match table structure.
* Formatting: Maintain consistent formatting with the image, including spacing, indentation, subscripts/superscripts, and special characters.
* Images: Include a description of any images in the alt attribute of an <img> tag. Do not fill out the src property. Describe in detail inside the div tag. Also convert charts to high fidelity data, and convert diagrams to mermaid.
* Forms: Mark checkboxes and radio buttons properly.
* Text: join lines together properly into paragraphs using <p>...</p> tags.  Use <br> tags for line breaks within paragraphs, but only when absolutely necessary to maintain meaning.
* Chemistry: Use <chem>...</chem> tags for chemical formulas with reactive SMILES.
* Lists: Preserve indents and proper list markers.
* Use the simplest possible HTML structure that accurately represents the content of the block.
* Make sure the text is accurate and easy for a human to read and interpret.  Reading order should be correct and natural.
""".strip()

_ocr_model_cache = {'t': 0.0, 'v': None}

def get_ocr_model():
    """Model served by the OCR container (baidu/Unlimited-OCR), a separate vLLM
    with its own /v1/models — never mixed with get_running_models() on which
    other routes (stop/relaunch from admin) depend to target only
    the main chat model.
    """
    now = time.time()
    if now - _ocr_model_cache['t'] < 5:
        return _ocr_model_cache['v']
    v = None
    # Do NOT attempt the HTTP call if the container isn't running: the sidecar
    # network silently DROPs packets to an absent service, so
    # requests would wait the full timeout (~3 s) — that's what dragged down the
    # admin page when OCR was stopped. Process state is cached for 5 s.
    if _sidecar_proc_status_differe('ocr') == 'running':
        try:
            r = requests.get(f"{OCR_URL}/models", timeout=3)
            if r.ok:
                data = r.json().get('data', [])
                if data:
                    v = data[0]['id']
        except Exception:
            pass
    _ocr_model_cache.update(t=now, v=v)
    return v

def ocr_extract_stream(image_bytes, mime, instruction, on_done):
    """SSE generator: relays the OCR container's response as it comes
    (same format as playground_chat). The model queried is the one
    ACTUALLY served (get_ocr_model(), probed live) rather than a frozen
    name — indispensable since the admin can recreate this container with
    another model (OCR catalog, cf. _ocr_launch / /admin/ocr/catalog/*).
    on_done(full_text) is called once the stream ends (empty text on
    error), to let the caller persist the history.
    """
    # Commentaire SSE emis AVANT tout travail : il force l'ecriture immediate
    # des en-tetes de reponse. Sinon le premier octet ne part qu'au retour du
    # POST vers le conteneur OCR, or celui-ci peut monter a ~100 s sous
    # contention GPU (cf. plus bas) — bien au-dela des 15 s de delai de
    # connexion du proxy Next.js (lib/sseProxy.ts), qui coupait donc la requete
    # avant meme la premiere reponse. Meme piege que /support/chat et
    # /playground/chat. get_ocr_model() sonde le reseau : le yield passe avant.
    yield ": ouverture\n\n"
    model = get_ocr_model() or 'baidu/Unlimited-OCR'
    is_chandra = 'chandra' in model.lower()
    prompt_text = _CHANDRA_OCR_LAYOUT_PROMPT if is_chandra else f'<image>{instruction}'
    b64 = base64.b64encode(image_bytes).decode()
    full = []
    body = {
        'model': model,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt_text},
                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}},
            ],
        }],
        'max_tokens': 8192 if is_chandra else 4096,
        'temperature': 0.0,
        'stream': True,
    }
    if not is_chandra:
        # vllm_xargs: parameters of Unlimited-OCR's custom logits processor
        # (--logits_processors, cf. _OCR_VALUE_FLAGS on the runner side) — exists
        # only for this model, absent from the body sent to the others.
        body['extra_body'] = {
            'skip_special_tokens': False,
            'vllm_xargs': {'ngram_size': 35, 'window_size': 128},
        }
    try:
        # Wide margin: under GPU contention (H3 video running at the same time),
        # an OCR request that is normally <1s can climb to ~100s — seen in prod on
        # 04/08. Stays under the gunicorn worker timeout (200s) to never
        # kill the process.
        with requests.post(f"{OCR_URL}/chat/completions",
                           json=body, stream=True, timeout=(10, 180)) as r:
            if not r.ok:
                yield _sse_msg("OCR service unreachable.")
                return
            for line in r.iter_lines():
                if not line:
                    continue
                decoded = line.decode('utf-8', 'replace')
                yield decoded + "\n\n"
                if decoded.startswith('data: '):
                    payload = decoded[len('data: '):].strip()
                    if payload and payload != '[DONE]':
                        try:
                            piece = json.loads(payload)['choices'][0]['delta'].get('content')
                            if piece:
                                full.append(piece)
                        except Exception:
                            pass
    except Exception:
        yield _sse_msg("⚠ OCR stream interrupted.")
    finally:
        on_done(''.join(full))

OCR_IMAGES_DIR = '/app/data/ocr_images'
_OCR_IMAGE_EXT = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}

@bp.route('/api/ocr/extract', methods=['POST'])
@login_required
def api_ocr_extract():
    blocked = maintenance_block_sse()
    if blocked:
        return blocked
    wait = _chat_rate_limited(session['username'], 'rl-media')
    if wait:
        return Response(_sse_msg(f"Trop de requêtes. Réessaie dans {wait} s."),
                        mimetype='text/event-stream'), 429
    data, err_or_mime = _read_uploaded_image()
    if data is None:
        return Response(_sse_msg(err_or_mime), mimetype='text/event-stream'), 400
    instruction = request.form.get('instruction', 'document parsing.').strip()[:500]
    username = session['username']
    _t0 = time.time()  # start point for the extraction duration (until _persist)

    # Image saved BEFORE streaming (random name, never derived from the
    # filename sent by the client): the history must be able to redisplay
    # the analyzed image with the "detected zones" view, not just the text.
    os.makedirs(OCR_IMAGES_DIR, exist_ok=True)
    image_filename = f"{secrets.token_hex(16)}.{_OCR_IMAGE_EXT.get(err_or_mime, 'png')}"
    with open(os.path.join(OCR_IMAGES_DIR, image_filename), 'wb') as f:
        f.write(data)

    def _persist(text):
        if not text:
            try:
                os.remove(os.path.join(OCR_IMAGES_DIR, image_filename))
            except OSError:
                pass
            return
        db = get_db()
        duration_ms = int((time.time() - _t0) * 1000)  # real extraction time
        db.execute("INSERT INTO ocr_jobs (username, text, image_path, created_at, duration_ms) VALUES (?,?,?,?,?)",
                   (username, text, image_filename, datetime.now().isoformat(), duration_ms))
        # Purges the images of rows that fall out of the history window,
        # otherwise OCR_IMAGES_DIR grows indefinitely (no other reference
        # to these files once the row is deleted).
        stale = db.execute(
            """SELECT image_path FROM ocr_jobs WHERE username=? AND image_path IS NOT NULL
               AND id NOT IN (SELECT id FROM ocr_jobs WHERE username=? ORDER BY id DESC LIMIT ?)""",
            (username, username, OCR_HISTORY_LIMIT)).fetchall()
        for row in stale:
            try:
                os.remove(os.path.join(OCR_IMAGES_DIR, row['image_path']))
            except OSError:
                pass
        db.execute("""DELETE FROM ocr_jobs WHERE username=? AND id NOT IN (
                         SELECT id FROM ocr_jobs WHERE username=?
                         ORDER BY id DESC LIMIT ?)""",
                   (username, username, OCR_HISTORY_LIMIT))
        db.commit()

    return Response(stream_with_context(ocr_extract_stream(data, err_or_mime, instruction, _persist)),
                    mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@bp.route('/api/ocr/history')
@login_required
def api_ocr_history():
    rows = get_db().execute(
        "SELECT id, text, created_at, image_path IS NOT NULL AS has_image "
        "FROM ocr_jobs WHERE username=? ORDER BY id DESC",
        (session['username'],)).fetchall()
    return jsonify([dict(r) for r in rows])

@bp.route('/ocr/image/<int:job_id>')
@login_required
def ocr_image(job_id):
    # Scoped (id, username) in a single query — cf. the IDOR fixed on
    # /video/file/<prompt_id> earlier: never split the lookup from the
    # ownership check into two steps.
    row = get_db().execute(
        "SELECT image_path FROM ocr_jobs WHERE id=? AND username=?",
        (job_id, session['username'])).fetchone()
    if not row or not row['image_path']:
        abort(404)
    path = os.path.join(OCR_IMAGES_DIR, row['image_path'])
    if not os.path.isfile(path):
        abort(404)
    return send_file(path)

