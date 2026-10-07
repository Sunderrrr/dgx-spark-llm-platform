"""The playground's « generer_video » tool — modelled on image_tools.

The principle is the same as for search and images: an explicit user REQUEST
(« génère une vidéo de… », « fais un petit film de… ») arms the tool; a mere
mention of a video (« la vidéo que j'ai envoyée », « pourquoi ma vidéo
saccade ») triggers nothing. Execution reuses the Video page entry point
(comfyui_client.comfyui_generate → ComfyUI, MiniMax H3): a single generation
protocol to maintain, no new sidecar dialogue.

Why go THROUGH THE MODEL: « génère une vidéo d'un chat qui marche sur la lune »
must become a rich scene description (subject, movement, framing, camera,
lighting), not the raw sentence. The model rewrites it; the portal only
submits it and returns the address of the produced file
(/video/file/<prompt_id>), which the model links in markdown.

Execution is a GENERATOR: an H3 generation takes several minutes and the
frontend proxy cuts on inactivity — so we emit SSE heartbeats while waiting
(same reason as the websearch phase, see _phase_outils). As on the Image
page, the job runs in its own daemon thread and we only read its row: if the
client leaves mid-way, the generation still completes — the job is properly
closed and the video stays in the Video page history.

Service on demand: ComfyUI is a heavy sidecar (28 GiB launch guard) that sits
absent from memory when idle. The tool STARTS it at first use
(sidecars.sidecar_demarrage_auto, memory guard included) and is only declared
to the model while the service is up or startable (video_disponible).
"""
import re
import sqlite3
import threading
import time
from datetime import datetime

from comfyui_client import comfyui_generate, comfyui_is_up, comfyui_status
from db import get_db
from sidecars import attendre_job_media
from guards import media_job_done, media_job_slot
from sidecars import sidecar_demarrable, sidecar_demarrage_auto
from video_routes import _persister_video

# Wait between two polls of the job row (video_jobs): the worker follows the
# ComfyUI job in its own daemon thread, we only read its state.
_POLL_INTERVAL_S = 2
# Past this, we hand back to the model saying the generation is still running
# (the worker keeps following it and the video lands in the Video page
# history) — an H3 clip of a few seconds routinely takes several minutes.
_VIDEO_TIMEOUT_S = 900
# The worker's own bound: ComfyUI has no internal timeout, and a job that
# never ends must not hold the account's job slot for ever.
_VIDEO_JOB_MAX_S = 3600
# An SSE heartbeat every 15 s: well below the proxy's idle cutoff.
_HEARTBEAT_EVERY_S = 15

# Same strict rule as web search and images: a VERB of creation followed, in
# the same sentence, by something to watch. Past participles (« générée »,
# « filmé ») do not match, nor does a plain mention of a video. Up to two words
# may sit between the article and the object (« une COURTE vidéo », « a SHORT
# movie ») — the image tool's rule leaves them out and misses those phrasings.
_DEMANDE_VIDEO = re.compile(
    r"\b(?:genere|génère|generer|générer|cree|crée|creer|créer|veux|voudrais|"
    r"aimerais|fais|faites|realise|réalise|realiser|réaliser|generate|create|make|"
    r"animate|design)\b"
    r"[^.!?\n]{0,40}?"
    r"\b(?:une?|des|an?|the|a)\s+(?:\S+\s+){0,2}?"
    r"(?:vid[ée]os?|films?|animations?|clips?|s[ée]quences?|movies?)\b",
    re.I)

# Accepted false positives: « animation CSS », « film de protection »,
# « clip du presse-papiers » are not videos to render. Tested on the sentence
# fragment that matched, not on the whole message.
_PAS_UNE_VIDEO = re.compile(
    r"\b(?:css|html|web|react|vue|svelte|composant|interface|bouton|transition|"
    r"page|hover|survol|chargement|loading|curseur|plastique|protecteur|protection|"
    r"alimentaire|étirable|etirable|adh[ée]sif|isolant|windows|bios|presse-papiers)\b",
    re.I)


def _video_demandee(history):
    """Does the user EXPLICITLY ask for a video to be generated?"""
    from websearch_tools import _texte_de_la_demande   # import tardif : cycle sinon
    dernier = next((m for m in reversed(history) if m.get('role') == 'user'), None)
    texte = _texte_de_la_demande(str((dernier or {}).get('content', '')))
    # Every matched occurrence is checked: « crée une animation CSS » falls to
    # the guard, « crée une animation de chat » passes.
    return any(not _PAS_UNE_VIDEO.search(texte[m.start():m.end() + 40])
               for m in _DEMANDE_VIDEO.finditer(texte))


def video_disponible():
    """Is the video service ready, or at least startable? (tool declaration)

    The same rule as image_disponible, extended to « startable »: the sidecar
    is on-demand, and a tool that could not start it must not even be
    declared — the model would deny a capability the platform HAS.
    """
    return comfyui_is_up() or sidecar_demarrable('video')


def _outils_video():
    return [{'type': 'function', 'function': {
        'name': 'generer_video',
        'description': (
            "Génère une courte vidéo à partir d'une description. Réécris la "
            "demande en description de SCÈNE détaillée (sujet, mouvement, "
            "cadrage, caméra, lumière) — idéalement en anglais. Ne l'appelle "
            "que sur une demande explicite de l'utilisateur."),
        'parameters': {'type': 'object', 'properties': {
            'prompt': {'type': 'string',
                       'description': "Description détaillée de la scène à filmer."},
            'duree': {'type': 'integer',
                      'description': "Durée en secondes (2 à 15, défaut 5)."},
        }, 'required': ['prompt']}}}]



def _annonce_video(args):
    """What we are about to generate, told to the client before doing it."""
    return {'etape': 'generation_video', 'outil': 'comfyui',
            'question': str(args.get('prompt', ''))[:200]}


def _video_worker(prompt_id, username, cree_le):
    """Follows the ComfyUI job to its end and closes the row (daemon thread).

    The Video page does this persistence through its own polling route
    (video_routes._persister_video, shared here); the tool has no client
    polling its status, so somebody has to close the job — even when the chat
    client has left. Same contract as the Image page's worker: the generation
    completes and stays in the history.
    """
    try:
        fini, sortie = False, None
        debut = time.monotonic()
        while time.monotonic() - debut < _VIDEO_JOB_MAX_S:
            time.sleep(_POLL_INTERVAL_S)
            st = comfyui_status(prompt_id)
            if st['status'] in ('done', 'error'):
                fini, sortie = True, st
                break
        if not fini:
            # A job ComfyUI never finished: closing the row « error » beats
            # leaving it « running » for ever in the Video page history.
            sortie = {'status': 'error'}
        _persister_video(prompt_id, username, sortie, cree_le, None)
    finally:
        media_job_done(username)


def _exec_video_tool(args, username, journal):
    """Generate the video and return the text to hand back to the model.

    GENERATOR: yields SSE heartbeats while waiting; the final text is the
    return value, retrieved by `yield from` in _phase_outils.

    The job runs in a daemon thread (as on the Image page): if the client
    leaves mid-way, the generation still completes and the video stays in
    the Video page history.
    """
    prompt = str(args.get('prompt', '')).strip()[:10000]
    if not prompt:
        return "Aucune description fournie : impossible de générer une vidéo."
    try:
        duree = float(args.get('duree') or 5)
    except (TypeError, ValueError):
        duree = 5
    duree = max(2, min(15, duree))       # bounds of the H3 workflow

    # Service on demand: ComfyUI starts at first use, under the memory guard —
    # a refusal is said plainly, the user is not left guessing why nothing runs.
    ok, motif = sidecar_demarrage_auto('video', comfyui_is_up)
    if not ok:
        journal.append({'etape': 'generation_video_finie', 'outil': 'comfyui',
                        'question': prompt[:200], 'videos': [], 'erreur': motif})
        return (f"Le service vidéo est indisponible : {motif}. "
                "Dis-le tel quel à l'utilisateur.")
    if not media_job_slot(username):
        return ("Trop de générations vidéo en cours pour ce compte — "
                "attends la fin des précédentes.")
    # T2V (text only): the tool carries no reference image.
    prompt_id = comfyui_generate(None, prompt, duree)
    if not prompt_id:
        media_job_done(username)
        journal.append({'etape': 'generation_video_finie', 'outil': 'comfyui',
                        'question': prompt[:200], 'videos': [],
                        'erreur': "ComfyUI inaccessible ou requête refusée."})
        return ("La génération vidéo a échoué (sidecar indisponible ou surchargé). "
                "Dis-le tel quel à l'utilisateur.")
    cree_le = datetime.now().isoformat()
    db = get_db()
    # `status` is spelled out: the column defaults to 'pending', and the poll
    # below watches for the worker to leave 'running'.
    db.execute("INSERT INTO video_jobs (username, prompt_id, prompt, status, created_at, req_duration_s) VALUES (?,?,?,?,?,?)",
               (username, prompt_id, prompt[:2000], 'running', cree_le, int(duree)))
    db.commit()
    threading.Thread(target=_video_worker,
                     args=(prompt_id, username, cree_le), daemon=True).start()

    # The wait loop is shared with the image tool (sidecars.attendre_job_media):
    # the same 18 lines used to live in BOTH tools (audit 2026-10-07).
    statut = 'running'
    # `yield from` FORWARDS the heartbeats to the chat stream (see image_tools).
    ligne = yield from attendre_job_media('video_jobs', prompt_id, username, _VIDEO_TIMEOUT_S)
    if ligne:
        statut = ligne.get('status')

    urls = [f"/video/file/{prompt_id}"] if statut == 'done' else []
    if statut == 'running':
        # Not a failure: the worker keeps following the job and the video will
        # land in the Video page history. We say so instead of inventing an
        # error the user would retry — a second generation would cost as much
        # as the first one.
        journal.append({'etape': 'generation_video_finie', 'outil': 'comfyui',
                        'question': prompt[:200], 'videos': [],
                        'erreur': "Génération toujours en cours."})
        return ("La vidéo est encore en cours de génération (elle apparaîtra dans "
                "l'historique de la page Vidéo). Ne donne PAS d'adresse pour "
                "l'instant ; dis simplement que la génération continue.")
    journal.append({'etape': 'generation_video_finie', 'outil': 'comfyui',
                    'question': prompt[:200], 'videos': urls,
                    'erreur': None if urls else "La génération a échoué."})
    if not urls:
        return ("La génération vidéo a échoué (sidecar indisponible ou surchargé). "
                "Dis-le tel quel à l'utilisateur.")
    return ("Vidéo générée. Intègre-la TELLE QUELLE dans ta réponse en markdown "
            "(ne modifie jamais l'adresse) :\n" +
            "\n".join(f"[vidéo générée]({u})" for u in urls))
