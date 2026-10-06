"""The playground's « lire_document » tool — modelled on image_tools.

The user JOINS an image (scan, photo of a page, screenshot) and asks what it
contains: « lis ce document », « transcris cette facture », « extrais le
texte ». The model reads images only when it has a projector, and never as
reliably as an OCR model on a dense page — so the tool runs the SAME extraction
the /ocr page runs (ocr_routes.ocr_extract_stream → the Unlimited-OCR sidecar)
on the SAME attached file the playground already sends as base64, and hands the
TEXT back to the model.

The strict rule is the one of search and images: an explicit REQUEST arms the
tool, a mere mention (« le document que tu as rédigé », « la facture de
janvier ») triggers nothing.

Which attachment: the model may name one (« la 2e pièce jointe ») — the images
travel as data URLs WITHOUT their file name (it stays in the browser), so a
name can only aim at its place in the conversation; anything else falls back
to the NEWEST attachment, which is almost always the one being discussed.

Execution is a GENERATOR: under GPU contention an extraction that is normally
under a second can climb to ~100 s (seen in prod on 04/08) and the frontend
proxy cuts on inactivity — so we emit SSE heartbeats while waiting, exactly
like the image generation.

Service on demand: the OCR sidecar sits absent from memory when idle. The tool
STARTS it at first use (sidecars.sidecar_demarrage_auto, memory guard
included) and is only declared to the model while the service is up or
startable (document_disponible).
"""
import base64
import re
import threading
import time

from sidecars import get_ocr_model, sidecar_demarrable, sidecar_demarrage_auto

# Wait between two polls of the extraction thread. The OCR call is a single
# streamed request (no job row): we only watch the thread that consumes it.
_POLL_INTERVAL_S = 2
# Past this we hand back a failure — the sidecar's own read timeout is 180 s
# (ocr_extract_stream), we leave it the last word before giving up.
_DOC_TIMEOUT_S = 210
# An SSE heartbeat every 15 s: well below the proxy's idle cutoff.
_HEARTBEAT_EVERY_S = 15
# What the model receives back. An extraction is text to quote from, not a file
# to store: the tool result already goes through the 6 000-char digest bound of
# _phase_outils, this one caps what is kept for the final re-injection.
_TEXTE_MAX = 20000
# Same default as the /ocr page: the extraction task token of Unlimited-OCR.
_INSTRUCTION = 'document parsing.'

# Same strict rule as search and images: a verb of READING/EXTRACTION followed,
# in the same sentence, by a document object (or the reverse). « Décris cette
# image » is NOT here on purpose: describing is the model's own job, this tool
# extracts text.
_VERBES_LECTURE = (r"lis|lire|lit|transcris|transcrire|transcription|extrais|"
                  r"extraire|extraction|reconnais|reconna[îi]tre|saisis|saisir|"
                  r"d[ée]chiffre|d[ée]chiffrer|d[ée]crypte|d[ée]crypter|"
                  r"lecture|ocr|que contient|que dit|read|transcribe|extract")
_OBJETS_DOCUMENT = (r"documents?|pi[èe]ces? jointes?|fichiers? joints?|images?|"
                    r"pictures?|photos?|factures?|invoices?|re[çc]us?|receipts?|"
                    r"tickets?|bons? de commande|formulaire|contrats?|notes? de "
                    r"frais|manuscrits?|lettres?|scans?|num[ée]ris[ée]s?|textes?|"
                    r"captures? d.?[eé]cran|attestations?|bulletins?|pages?")

_DEMANDE_DOCUMENT = re.compile(
    rf"\b(?:{_VERBES_LECTURE})\b[^.!?\n]{{0,40}}\b(?:{_OBJETS_DOCUMENT})\b"
    rf"|\b(?:{_OBJETS_DOCUMENT})\b[^.!?\n]{{0,30}}\b(?:{_VERBES_LECTURE})\b"
    rf"|\bocr\b",
    re.I)

# Accepted false positives: a page to READ on the web is search's job, a file
# of code is already in the conversation as text, and an « image disque » is
# not a page to OCR. Tested on the sentence fragment that matched, not on the
# whole message.
_PAS_UN_DOCUMENT = re.compile(
    r"\b(?:web|internet|en ligne|site|url|adresse|html|code|source|répertoire|"
    r"depot|dépôt|github|documentation|manuel|docker|conteneur|container|disque|"
    r"disk|iso|syst[èe]me|systeme)\b",
    re.I)


def _document_demandee(history):
    """Does the user EXPLICITLY ask for a document to be read?"""
    from websearch_tools import _texte_de_la_demande   # import tardif : cycle sinon
    dernier = next((m for m in reversed(history) if m.get('role') == 'user'), None)
    texte = _texte_de_la_demande(str((dernier or {}).get('content', '')))
    # Every matched occurrence is checked: « lis la page web » falls to the
    # guard, « lis la facture jointe » passes.
    return any(not _PAS_UN_DOCUMENT.search(texte[m.start():m.end() + 40])
               for m in _DEMANDE_DOCUMENT.finditer(texte))


def document_disponible():
    """Is the OCR service ready, or at least startable? (tool declaration)

    The same rule as image_disponible, extended to « startable »: the sidecar
    is on-demand, and a tool that could not start it must not even be
    declared — the model would deny a capability the platform HAS.
    """
    return get_ocr_model() is not None or sidecar_demarrable('ocr')


def _outils_document():
    return [{'type': 'function', 'function': {
        'name': 'lire_document',
        'description': (
            "Lit (OCR) une image jointe à la conversation et en extrait le "
            "TEXTE : document scanné, photo d'une page, capture d'écran. À "
            "appeler quand l'utilisateur demande explicitement le contenu "
            "d'une pièce jointe — pour une simple description d'image, "
            "décris-la toi-même."),
        'parameters': {'type': 'object', 'properties': {
            'fichier': {'type': 'string',
                        'description': "Nom ou numéro de la pièce jointe à lire "
                                       "(la plus récente par défaut)."},
        }, 'required': []}}}]



def _annonce_document(args):
    """What we are about to read, told to the client before doing it."""
    return {'etape': 'ocr', 'outil': 'unlimited-ocr',
            'question': str(args.get('fichier', ''))[:200]}


def _piece_jointe(pieces, fichier):
    """(data-URL of the attachment to read, its display name).

    The images travel WITHOUT their file name (see the module docstring): a
    name therefore only aims at a PLACE — « 2 », « image-2.jpg » and « pièce
    jointe 2 » all mean the second one. Everything else falls back to the
    newest attachment.
    """
    if not pieces:
        return None, ''
    nom = str(fichier or '').strip()
    chiffres = re.findall(r'\d+', nom)
    if chiffres:
        n = int(chiffres[-1])
        if 1 <= n <= len(pieces):
            return pieces[n - 1], nom or f"pièce jointe {n}"
    return pieces[-1], nom


def _decode_data_url(url):
    """(mime, bytes) of an image data URL — the shape attachments already
    travel in (conversation_routes.IMAGE_DATA_URL validates them)."""
    entete, b64 = str(url).split(',', 1)
    mime = entete[len('data:'):].split(';')[0] or 'image/png'
    return mime, base64.b64decode(b64)


def _exec_document_tool(args, username, journal, pieces):
    """Extract the text of the attachment and return it to the model.

    GENERATOR: yields SSE heartbeats while the OCR sidecar works; the extracted
    text is the return value, retrieved by `yield from` in _phase_outils.
    """
    import ocr_routes   # import tardif : image_tools fait de même sur image_routes

    cible, nom = _piece_jointe(pieces, args.get('fichier'))
    if cible is None:
        return ("Aucune image jointe à cette conversation : je ne peux lire "
                "aucun document. Demande à l'utilisateur d'en joindre une.")
    # Service on demand: the OCR sidecar starts at first use, under the memory
    # guard — a refusal is said plainly, the user is not left guessing why.
    ok, motif = sidecar_demarrage_auto('ocr', lambda: get_ocr_model() is not None)
    if not ok:
        journal.append({'etape': 'ocr_finie', 'outil': 'unlimited-ocr',
                        'question': nom, 'caracteres': 0, 'erreur': motif})
        return (f"Le service de lecture est indisponible : {motif}. "
                "Dis-le tel quel à l'utilisateur.")
    mime, octets = _decode_data_url(cible)

    boite = {}
    def _run():
        vu = []
        try:
            # ocr_extract_stream is the /ocr page's entry point: a generator
            # relaying the sidecar's own stream. We drop the frames and keep
            # only the text it gathers, via on_done.
            for _ in ocr_routes.ocr_extract_stream(
                    octets, mime, _INSTRUCTION, vu.append):
                pass
            boite['texte'] = vu[0] if vu else ''
        except Exception as e:                     # noqa: BLE001
            boite['e'] = e

    fil = threading.Thread(target=_run, daemon=True)
    fil.start()
    debut = time.monotonic()
    prochain_battement = time.monotonic() + _HEARTBEAT_EVERY_S
    while fil.is_alive() and time.monotonic() - debut < _DOC_TIMEOUT_S:
        fil.join(_POLL_INTERVAL_S)
        if fil.is_alive() and time.monotonic() >= prochain_battement:
            yield ": battement\n\n"
            prochain_battement = time.monotonic() + _HEARTBEAT_EVERY_S
    if fil.is_alive():
        journal.append({'etape': 'ocr_finie', 'outil': 'unlimited-ocr',
                        'question': nom, 'caracteres': 0,
                        'erreur': "Lecture interrompue : trop longue."})
        return ("La lecture du document a pris trop de temps et a été "
                "interrompue. Dis-le tel quel à l'utilisateur.")

    texte = str(boite.get('texte') or '').strip()
    if not texte:
        motif = (str(boite['e'])[:120] if 'e' in boite
                 else "service en difficulté ou image illisible")
        journal.append({'etape': 'ocr_finie', 'outil': 'unlimited-ocr',
                        'question': nom, 'caracteres': 0, 'erreur': motif})
        return ("Aucun texte n'a pu être extrait de ce document (" + motif +
                "). Dis-le tel quel à l'utilisateur.")
    journal.append({'etape': 'ocr_finie', 'outil': 'unlimited-ocr',
                    'question': nom, 'caracteres': len(texte), 'erreur': None})
    return ("Texte extrait du document par OCR (brut, dactylographié ou "
            "manuscrit — cite-le, ne le réécris pas) :\n\n" + texte[:_TEXTE_MAX])
