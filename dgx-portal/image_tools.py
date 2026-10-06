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
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime

from db import DB_PATH, get_db
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
_DEMANDE_IMAGE = re.compile(
    r"\b(?:genere|génère|generer|générer|généré|générée|générés|générées|"
    r"cree|crée|creer|créer|créé|créée|créés|créées|veux|voudrais|"
    r"aimerais|fais|faites|generate|create|make|design)\b"
    r"[^.!?\n]{0,40}?"
    r"\b(?:une?|des|an?|\d+)\s+(?:image|dessin|illustration|logo|ic[ôo]ne|avatar|picture|drawing|icon)s?\b"
    r"|\bdessine\b[^.!?\n]{0,40}"
    r"|\bdraw\b[^.!?\n]{0,40}",
    re.I)

# Accepted false positives from system vocabulary: « image docker », « image
# disque », « image système » are not images to paint. Tested on the sentence
# fragment that matched, not on the whole message.
_PAS_UNE_IMAGE = re.compile(r"\b(?:docker|conteneur|container|oci|disque|disk|iso|"
                            r"syst[èe]me|systeme|kernel)\b", re.I)


def _image_demandee(history):
    """Does the user EXPLICITLY ask for an image to be generated?"""
    from websearch_tools import _texte_de_la_demande   # import tardif : cycle sinon
    dernier = next((m for m in reversed(history) if m.get('role') == 'user'), None)
    texte = _texte_de_la_demande(str((dernier or {}).get('content', '')))
    # Every matched occurrence is checked: « crée une image docker » falls to the
    # guard, « crée une image de chat » passes.
    return any(not _PAS_UNE_IMAGE.search(texte[m.start():m.end() + 40])
               for m in _DEMANDE_IMAGE.finditer(texte))


def image_disponible():
    """Is the generation sidecar configured and ready?"""
    return image_ready()


def _outils_image():
    return [{'type': 'function', 'function': {
        'name': 'generer_image',
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
            'largeur': {'type': 'integer',
                        'description': "Largeur en pixels (256–1536, défaut 1024)."},
            'hauteur': {'type': 'integer',
                        'description': "Hauteur en pixels (256–1536, défaut 1024)."},
            'nombre': {'type': 'integer',
                       'description': "Nombre d'images à produire (1-4, défaut 1)."},
            'sortie_largeur': {'type': 'integer',
                               'description': "Largeur FINALE après agrandissement "
                                              "(jusqu'à 3840 — la 4K — défaut : la taille "
                                              "de génération)."},
            'sortie_hauteur': {'type': 'integer',
                               'description': "Hauteur FINALE après agrandissement "
                                              "(jusqu'à 3840)."},
        }, 'required': ['prompt']}}}]



def _annonce_image(args):
    """What we are about to generate, told to the client before doing it."""
    # `nombre` lets the client show ONE placeholder per image to come — the
    # Image page does this, a single grey square while four are cooking read as
    # « nothing is happening ».
    try:
        nombre = max(1, min(4, int(args.get('nombre') or 1)))
    except (TypeError, ValueError):
        nombre = 1
    return {'etape': 'generation', 'outil': 'diffusers', 'nombre': nombre,
            'question': str(args.get('prompt', ''))[:200]}


def _exec_image_tool(args, username, journal):
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

    def _dim(cle, defaut):
        try:
            v = int(args.get(cle) or defaut)
        except (TypeError, ValueError):
            v = defaut
        return max(256, min(1536, v))

    largeur, hauteur = _dim('largeur', 1024), _dim('hauteur', 1024)

    def _dim_sortie(cle):
        try:
            v = int(args.get(cle) or 0)
        except (TypeError, ValueError):
            v = 0
        return max(0, min(3840, v))

    # 4K is done by UPSCALING, like on the image page: the generation stays
    # within 256–1536 and it is the output that goes up to 3840.
    sortie_largeur, sortie_hauteur = _dim_sortie('sortie_largeur'), _dim_sortie('sortie_hauteur')
    try:
        nombre = int(args.get('nombre') or 1)
    except (TypeError, ValueError):
        nombre = 1
    nombre = max(1, min(4, nombre))

    if not media_job_slot(username):
        return ("Trop de générations d'images en cours pour ce compte — "
                "attends la fin des précédentes.")
    prompt_id = secrets.token_hex(12)
    db = get_db()
    db.execute("INSERT INTO image_jobs (username, prompt_id, prompt, status, count, "
               "done_count, format, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (username, prompt_id, prompt[:2000], 'running', nombre, 0, fmt,
                datetime.now().isoformat()))
    db.commit()

    def _run():
        try:
            image_routes._image_worker(prompt_id, username, prompt, nombre, fmt,
                                       largeur, hauteur, sortie_largeur, sortie_hauteur)
        finally:
            media_job_done(username)

    threading.Thread(target=_run, daemon=True).start()

    statut, fait = 'running', 0
    debut = time.monotonic()
    prochain_battement = time.monotonic() + _HEARTBEAT_EVERY_S
    while time.monotonic() - debut < _IMAGE_TIMEOUT_S:
        time.sleep(_POLL_INTERVAL_S)
        try:
            c = sqlite3.connect(DB_PATH, timeout=5)
            row = c.execute("SELECT status, done_count FROM image_jobs "
                            "WHERE prompt_id=? AND username=?",
                            (prompt_id, username)).fetchone()
            c.close()
        except Exception:
            row = None
        if row:
            statut, fait = row[0], row[1]
        if statut != 'running':
            break
        if time.monotonic() >= prochain_battement:
            yield ": battement\n\n"
            prochain_battement = time.monotonic() + _HEARTBEAT_EVERY_S

    urls = ([f"/image/file/{prompt_id}/{i}" for i in range(fait)]
            if statut == 'done' else [])
    journal.append({'etape': 'generation_finie', 'outil': 'diffusers',
                    'question': prompt[:200], 'prompt_id': prompt_id,
                    'images': urls,
                    'erreur': None if urls else "La génération a échoué."})
    if not urls:
        return ("La génération d'image a échoué (sidecar indisponible ou "
                "surchargé). Dis-le tel quel à l'utilisateur.")
    return ("Image générée. Intègre-la TELLE QUELLE dans ta réponse en markdown "
            "(ne modifie jamais l'adresse) :\n" +
            "\n".join(f"![image générée]({u})" for u in urls))
