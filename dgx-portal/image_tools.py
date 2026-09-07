"""Outil « generer_image » du playground — sur le modèle de websearch_tools.

Le principe est le même que pour la recherche : une DEMANDE explicite de
l'utilisateur (« génère une image de… », « dessine-moi… ») arme l'outil ; une
simple mention d'image (« décris cette image », « l'image générée hier ») ne
déclenche rien. L'exécution réutilise le worker de la page Image
(image_routes._image_worker) — un seul chemin de génération à maintenir.

Pourquoi passer PAR LE MODÈLE : « génère une image d'un chat astronaute » doit
devenir un prompt de diffusion riche (sujet, style, lumière, cadrage), pas la
phrase brute. Le modèle réécrit ; le portal ne fait qu'exécuter et rendre les
adresses des fichiers produits (/image/file/<prompt_id>/<idx>), que le modèle
intègre en markdown — le renderer Astryx les affiche comme des <img>.

L'exécution est un GÉNÉRATEUR : une génération prend 10–60 s couramment
(jusqu'à plusieurs minutes) et le proxy du frontend coupe sur inactivité — on
renvoie donc des battements SSE pendant l'attente (même raison que la phase
websearch, cf. _phase_outils).
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

# Attente entre deux sondages du statut du job (ligne image_jobs) : le worker
# tourne dans son propre thread daemon, on ne fait que lire son avancement.
_POLL_INTERVAL_S = 2
# Au-delà, on rend la main au modèle avec un message d'échec (le sidecar
# répond en général en 10–60 s ; 600 s est son propre timeout interne).
_IMAGE_TIMEOUT_S = 420
# Un battement SSE toutes les 15 s : très en dessous du coupe-flux du proxy.
_HEARTBEAT_EVERY_S = 15

# Même règle stricte que la recherche web : un VERBE de création suivi, dans la
# même phrase, d'un objet visuel. Les participes passés (« générée », « dessiné »)
# ne matchent pas (l'accent ou la lettre finale casse le mot), « redessine » non
# plus (\b au milieu d'un mot n'existe pas).
_DEMANDE_IMAGE = re.compile(
    r"\b(?:genere|génère|generer|générer|cree|crée|creer|créer|veux|voudrais|"
    r"aimerais|fais|faites|generate|create|make|design)\b"
    r"[^.!?\n]{0,40}?"
    r"\b(?:une?|des|an?)\s+(?:image|dessin|illustration|logo|ic[ôo]ne|avatar|picture|drawing|icon)s?\b"
    r"|\bdessine\b[^.!?\n]{0,40}"
    r"|\bdraw\b[^.!?\n]{0,40}",
    re.I)

# Faux positifs assumés du vocabulaire système : « image docker », « image
# disque », « image système » ne sont pas des images à peindre. Testé sur le
# fragment de phrase qui a matché, pas sur tout le message.
_PAS_UNE_IMAGE = re.compile(r"\b(?:docker|conteneur|container|oci|disque|disk|iso|"
                            r"syst[èe]me|systeme|kernel)\b", re.I)


def _image_demandee(history):
    """L'utilisateur demande-t-il EXPLICITEMENT la génération d'une image ?"""
    from websearch_tools import _texte_de_la_demande   # import tardif : cycle sinon
    dernier = next((m for m in reversed(history) if m.get('role') == 'user'), None)
    texte = _texte_de_la_demande(str((dernier or {}).get('content', '')))
    # Chaque occurrence matchée est vérifiée : « crée une image docker » tombe
    # sur le garde-fou, « crée une image de chat » passe.
    return any(not _PAS_UNE_IMAGE.search(texte[m.start():m.end() + 40])
               for m in _DEMANDE_IMAGE.finditer(texte))


def image_disponible():
    """Le sidecar de génération est-il configuré et prêt ?"""
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
        }, 'required': ['prompt']}}}]



def _annonce_image(args):
    """Ce qu'on s'apprête à générer, dit au client avant de le faire."""
    return {'etape': 'generation', 'outil': 'diffusers',
            'question': str(args.get('prompt', ''))[:200]}


def _exec_image_tool(args, username, journal):
    """Génère l'image et rend le texte à remettre au modèle.

    GÉNÉRATEUR : yield de battements SSE pendant l'attente ; le texte final est
    la valeur de retour, récupérée par `yield from` dans _phase_outils.

    Le worker tourne dans un thread daemon (comme côté page Image) : si le
    client part en plein milieu, la génération se termine quand même — le job
    est proprement clôturé et l'image reste dans l'historique de la page Image.
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

    if not media_job_slot(username):
        return ("Trop de générations d'images en cours pour ce compte — "
                "attends la fin des précédentes.")
    prompt_id = secrets.token_hex(12)
    db = get_db()
    db.execute("INSERT INTO image_jobs (username, prompt_id, prompt, status, count, "
               "done_count, format, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (username, prompt_id, prompt[:2000], 'running', 1, 0, fmt,
                datetime.now().isoformat()))
    db.commit()

    def _run():
        try:
            image_routes._image_worker(prompt_id, username, prompt, 1, fmt,
                                       largeur, hauteur)
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
