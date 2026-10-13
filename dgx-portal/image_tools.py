"""The playground's « generer_image » tool — modelled on websearch_tools.

The principle is the same as for search: an explicit user REQUEST
(« génère une image de… », « dessine-moi… ») arms the tool; a mere mention of
an image (« décris cette image », « l'image générée hier ») triggers nothing.
Execution reuses the Image page worker (image_routes._image_worker) — a
single generation path to maintain.

Why go THROUGH THE MODEL: « génère une image d'un chat astronaute » must
become a rich diffusion prompt (subject, style, lighting, framing), not the
raw sentence. The model rewrites it; the portal only executes and returns
the addresses of the produced files (/image/file/<prompt_id>/<idx>), which
the model embeds in markdown — the Astryx renderer displays them as <img>.

Execution is a GENERATOR: a generation commonly takes 10–60 s (up to several
minutes) and the frontend proxy cuts on inactivity — so we emit SSE
heartbeats while waiting (same reason as the websearch phase, see
_phase_outils).
"""
import os
import re
import secrets
import threading
from datetime import datetime

from request_turns import previous_answer, request_texts
from db import get_db
from sidecars import wait_media_job
from guards import media_job_done, media_job_slot
from sidecars import image_ready

# Wait between two polls of the job status (image_jobs row): the worker runs
# in its own daemon thread, we only read its progress.
_POLL_INTERVAL_S = 2
# Past this, we hand back to the model with a failure message (the sidecar
# usually answers in 10–60 s; 600 s is its own internal timeout).
_IMAGE_TIMEOUT_S = 420
# An SSE heartbeat every 15 s: well below the proxy's idle cutoff.
_HEARTBEAT_EVERY_S = 15

# Same strict rule as web search: a VERB of creation followed, in the same
# sentence, by a visual object. Participles DID not match (« généré », « créée »)
# and numerals did not either (« 4 images » is not « une image ») — measured
# 2026-10-06: the operator asked « généré en 4K 4 images pour halloween » and the
# model answered « je ne peux pas générer » because the tool was never armed.
# Both forms are accepted now. « redessine » still does not match (\b mid-word
# does not exist).
_IMAGE_REQUEST = re.compile(
    # g[ée]n[éeè]r…: génère, génére (sic), générer, générées, genere…
    r"\b(?:g[ée]n[éeè]r\w*|"
    r"cree|crée|creer|créer|créé|créée|créés|créées|veux|voudrais|"
    # « fait » (sic) is how « fais-moi » is very often typed (2026-10-13).
    r"aimerais|fais|fait|faire|faites|refais|refait|generate|create|make|design)\b"
    r"[^.!?\n]{0,40}?"
    r"(?:\b(?:une?|des|les|ces|mes|la|le|an?|\d+|deux|trois|quatre|cinq)\s+|\bl')"
    r"(?:image|dessin|illustration|logo|ic[ôo]ne|avatar|visuel|affiche|"
    r"flyer|poster|photo|picture|drawing|icon)s?\b"
    r"|\bdessine\b[^.!?\n]{0,40}"
    r"|\bdraw\b[^.!?\n]{0,40}",
    re.I)

# Accepted false positives from system vocabulary: « image docker », « image
# disque », « image système » are not images to paint. Tested on the sentence
# fragment that matched, not on the whole message.
_NOT_AN_IMAGE = re.compile(r"\b(?:docker|conteneur|container|oci|disque|disk|iso|"
                            r"syst[èe]me|systeme|kernel)\b", re.I)


def _image_requested(history):
    """Does the user EXPLICITLY ask for an image to be generated?"""
    from websearch_tools import _request_text   # import tardif : cycle sinon
    # The request may sit before a clarifying questionnaire (request_turns).
    for raw in request_texts(history):
        text = _request_text(raw)
        # Every matched occurrence is checked: « crée une image docker » falls to the
        # guard, « crée une image de chat » passes.
        if any(not _NOT_AN_IMAGE.search(text[m.start():m.end() + 40])
               for m in _IMAGE_REQUEST.finditer(text)):
            return True
    # A follow-up on images just generated (« ajuste les photos », « refais en
    # plus sombre »): the tool stays offered. Without it the model « redid »
    # them by inventing addresses (/image/file/<id>/3…8, 2026-10-13).
    return '/image/file/' in previous_answer(history)


# A generated image cited in an answer: ![…](/image/file/<prompt_id>/<idx>).
_IMAGE_REF = re.compile(r'/image/file/([0-9a-f]+)(?:/(\d+))?')


def image_exists(username, prompt_id, idx=0):
    """Does /image/file/<prompt_id>/<idx> serve a file to this user? Same
    lookup as the route (image_routes.image_file)."""
    from image_routes import IMAGE_EXT, IMAGE_FILES_DIR
    if not get_db().execute("SELECT 1 FROM image_jobs WHERE prompt_id=? AND username=?",
                            (prompt_id, username)).fetchone():
        return False
    if any(os.path.isfile(os.path.join(IMAGE_FILES_DIR, f"{prompt_id}_{idx}.{ext}"))
           for ext in IMAGE_EXT.values()):
        return True
    return idx == 0 and os.path.isfile(os.path.join(IMAGE_FILES_DIR, prompt_id + '.png'))


def invented_images(text, username):
    """The generated-image addresses an answer cites that serve nothing — the
    model « showing » images it never produced."""
    seen, out = set(), []
    for m in _IMAGE_REF.finditer(text or ''):
        key = (m.group(1), int(m.group(2) or 0))
        if key in seen:
            continue
        seen.add(key)
        if not image_exists(username, *key):
            out.append(m.group(0))
    return out


def image_available():
    """Is the generation sidecar configured and ready?"""
    return image_ready()


def _image_tools():
    return [{'type': 'function', 'function': {
        'name': 'generate_image',
        'description': (
            "Génère une image à partir d'une description. Réécris la demande en "
            "prompt visuel détaillé (sujet, style, lumière, cadrage) — idéalement "
            "en anglais, les modèles de diffusion y répondent mieux. Ne l'appelle "
            "que sur une demande explicite de l'utilisateur."),
        'parameters': {'type': 'object', 'properties': {
            'prompt': {'type': 'string',
                       'description': "Description visuelle détaillée de l'image à produire."},
            'format': {'type': 'string', 'enum': ['png', 'jpeg', 'webp'],
                       'description': "Format du fichier (défaut png)."},
            'width': {'type': 'integer',
                        'description': "Largeur en pixels (256–1536, défaut 1024)."},
            'height': {'type': 'integer',
                        'description': "Hauteur en pixels (256–1536, défaut 1024)."},
            'count': {'type': 'integer',
                       'description': "Nombre d'images à produire (1-4, défaut 1)."},
            'output_width': {'type': 'integer',
                               'description': "Largeur FINALE après agrandissement "
                                              "(jusqu'à 3840 — la 4K — défaut : la taille "
                                              "de génération)."},
            'output_height': {'type': 'integer',
                               'description': "Hauteur FINALE après agrandissement "
                                              "(jusqu'à 3840)."},
        }, 'required': ['prompt']}}}]



def _announce_image(args):
    """What we are about to generate, told to the client before doing it."""
    # `nombre` lets the client show ONE placeholder per image to come — the
    # Image page does this, a single grey square while four are cooking read as
    # « nothing is happening ».
    try:
        count_value = max(1, min(4, int(args.get('count') or 1)))
    except (TypeError, ValueError):
        count_value = 1
    return {'step': 'generation', 'tool': 'diffusers', 'count': count_value,
            'question': str(args.get('prompt', ''))[:200]}


def _exec_image_tool(args, username, step_log):
    """Generate the image and return the text to hand back to the model.

    GENERATOR: yields SSE heartbeats while waiting; the final text is the
    return value, retrieved by `yield from` in _phase_outils.

    The worker runs in a daemon thread (as on the Image page): if the client
    leaves mid-way, the generation still completes — the job is properly
    closed and the image stays in the Image page history.
    """
    import image_routes

    prompt = str(args.get('prompt', '')).strip()[:10000]
    if not prompt:
        return "Aucun prompt fourni : impossible de générer une image."
    fmt = str(args.get('format', 'png') or 'png').strip().lower()
    fmt = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'webp': 'webp'}.get(fmt, 'png')

    def _dim(key, default):
        try:
            v = int(args.get(key) or default)
        except (TypeError, ValueError):
            v = default
        return max(256, min(1536, v))

    width, height = _dim('width', 1024), _dim('height', 1024)

    def _output_dim(key):
        try:
            v = int(args.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        return max(0, min(3840, v))

    # 4K is done by UPSCALING, like on the image page: the generation stays
    # within 256–1536 and it is the output that goes up to 3840.
    output_width, output_height = _output_dim('output_width'), _output_dim('output_height')
    try:
        count_value = int(args.get('count') or 1)
    except (TypeError, ValueError):
        count_value = 1
    count_value = max(1, min(4, count_value))

    if not media_job_slot(username):
        return ("Trop de générations d'images en cours pour ce compte — "
                "attends la fin des précédentes.")
    prompt_id = secrets.token_hex(12)
    db = get_db()
    db.execute("INSERT INTO image_jobs (username, prompt_id, prompt, status, count, "
               "done_count, format, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (username, prompt_id, prompt[:2000], 'running', count_value, 0, fmt,
                datetime.now().isoformat()))
    db.commit()

    def _run():
        try:
            image_routes._image_worker(prompt_id, username, prompt, count_value, fmt,
                                       width, height, output_width, output_height)
        finally:
            media_job_done(username)

    threading.Thread(target=_run, daemon=True).start()

    # The wait loop is shared with the video tool (sidecars.attendre_job_media):
    # same poll, same heartbeat, one place to change.
    status_value, fact = 'running', 0
    # `yield from` FORWARDS the heartbeats to the chat stream: a plain
    # next() loop would swallow them and the client would see silence.
    line = yield from wait_media_job('image_jobs', prompt_id, username, _IMAGE_TIMEOUT_S)
    if line:
        status_value, fact = line.get('status'), line.get('done_count') or 0

    urls = ([f"/image/file/{prompt_id}/{i}" for i in range(fact)]
            if status_value == 'done' else [])
    step_log.append({'step': 'generation_done', 'tool': 'diffusers',
                    'question': prompt[:200], 'prompt_id': prompt_id,
                    'images': urls,
                    'error': None if urls else "La génération a échoué."})
    if not urls:
        return ("La génération d'image a échoué (sidecar indisponible ou "
                "surchargé). Dis-le tel quel à l'utilisateur.")
    return ("Image générée. Intègre-la TELLE QUELLE dans ta réponse en markdown "
            "(ne modifie jamais l'adresse) :\n" +
            "\n".join(f"![image générée]({u})" for u in urls))
