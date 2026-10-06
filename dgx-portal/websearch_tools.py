"""Web search tools exposed to the model from the playground.

Extracted from app.py on 28/08. Follows on from db.py: its only remaining
dependency was LITELLM_URL, which comes from the environment, and get_db,
which now lives in the shared core — so no app.py import, no possible cycle.

The low-level mechanics (SearXNG, crawl4ai, SSRF guardrails) live in
websearch.py; this module is the layer that presents it to the model as
tools and orchestrates the call round.
"""
import json
import logging
import os
import re
import threading
import time

import requests

import websearch
import document_tools
import image_tools
import video_tools
from db import get_db

LITELLM_URL = os.environ.get('LITELLM_URL', 'http://litellm:4000')

# The playground had no tool loop: it relayed the model's stream
# as-is. We add one, in two steps:
#   1. a tool phase without streaming — the model may search, read, search
#      again, up to MAX_TOURS_OUTILS;
#   2. the final answer, streamed, exactly as before.
# Splitting this way avoids reassembling fragmented tool calls from
# deltas, and the visible answer always arrives token by token.
MAX_TOURS_OUTILS = 3
# Time cap of the tool phase. Legitimate or not, a search must never
# monopolize the wait: past this, we answer with what was found.
# A single page read can take a minute and a half.
DELAI_MAX_OUTILS = 60
# What the tool phase re-reads to decide whether to search. Deliberately
# SHORT: deciding « faut-il une recherche ? » does not call for re-reading a
# 65 Ko file. Passing it the whole conversation added a full preloading
# BEFORE the answer — measured: 30 s on 100 Ko of context, over 60 s beyond,
# and the client gave up (« Network error ») on slightly old conversations
# while a fresh conversation worked.
OUTILS_MSG_MAX = 1500      # per message
OUTILS_TOTAL_MAX = 8000    # in total
OUTILS_DERNIERS = 6        # number of messages re-read
# What is then added to `court` (tool results) was bounded by NOTHING:
# `OUTILS_TOTAL_MAX` only applies to the starting conversation, and each
# round stacked up to 4 results of 20 000 characters, reposted IN FULL on
# the next round. The 2nd round prompt reached ~88 000 characters, the 3rd
# even more — a preload of several tens of seconds, without a single byte
# sent to the client meanwhile. We bound per result AND in total.
OUTILS_RESULTAT_PROMPT = 6000   # per tool result (the digest is enough to decide)
OUTILS_PROMPT_MAX = 40000       # in total in `court`
# A search tool is SYNCHRONOUS and can block for a very long time:
# `lire_pages` waits `websearch.TIMEOUT_LECTURE` per page (90 s), while the
# frontend proxy cuts after 60 s WITHOUT A SINGLE BYTE (IDLE_TIMEOUT_MS). The
# result was thus lost and the user saw « Erreur réseau » although the read
# had succeeded. We run the tool in a thread and emit an SSE comment while
# waiting — same remedy as image generation.
BATTEMENT_OUTIL_S = 8

_log = logging.getLogger('app')


def _contexte_outils(msgs):
    """Lightweight version of the conversation, for the tool decision alone."""
    systeme = [m for m in msgs[:1] if m.get('role') == 'system']
    reste = msgs[len(systeme):]
    court, total = [], 0
    for m in reversed(reste[-OUTILS_DERNIERS:]):
        c = str(m.get('content', ''))
        if len(c) > OUTILS_MSG_MAX:
            # We keep the START and the END: the request is often at the top, the last
            # instruction at the tail; the belly of a big file does not help.
            moitie = OUTILS_MSG_MAX // 2
            c = c[:moitie] + "\n…\n" + c[-moitie:]
        if total + len(c) > OUTILS_TOTAL_MAX and court:
            break
        total += len(c)
        court.append({'role': m.get('role'), 'content': c})
    return [{'role': m['role'], 'content': str(m.get('content', ''))[:OUTILS_MSG_MAX]}
            for m in systeme] + list(reversed(court))


# Search only goes out on an EXPLICIT DIRECTIVE.
#
# Five versions of this rule failed in production, always the same way:
# guessing the intent from isolated words. « google » came from a font tag,
# « source » from a createBufferSource, and « en ligne » from a
# « jeu d'échecs en ligne » — the latter blocked one user six times in a
# row. An isolated word does not say what someone wants.
#
# So we require a phrasing that can only mean one thing: « cherche sur
# internet », « fais une recherche ». A news question no longer triggers
# anything by itself — a deliberate step back, but predictable: nobody
# suffers a search they did not ask for, and writing it is enough to get
# one.
#
# 2026-09: « en ligne » left the targets for good — « je cherche à mettre
# mon site en ligne » (19 characters under the window) opened a search,
# after the historic « jeu d'échecs en ligne ». The rule fits in one
# sentence: a REQUEST (« cherche sur internet », « fais une recherche »,
# « google-le ») triggers; a DESCRIPTION of what the user is looking for
# (« je cherche à… », « est en ligne ») does not.
_DEMANDE_RECHERCHE = re.compile(
    r"(?:cherche|recherche|regarde|va voir|renseigne-toi|informe-toi)"
    r"[^.!?\n]{0,30}?\b(?:sur (?:le )?(?:web|internet|net)|sur google)"
    r"|(?:fais|lance|effectue)[^.!?\n]{0,20}?\brecherche\b"
    r"|recherche\s+web|web\s*search|search\s+(?:the\s+)?web|search\s+online"
    r"|google[-\s]?(?:le|la|it)\b",
    re.I)


def _texte_de_la_demande(contenu):
    """What the user WROTE, without the file they pasted.

    Searching for the intent in the whole message amounted to searching it
    in the code. Observed: a `<link href="https://fonts.googleapis.com/...">`
    — hence the word « google » — was enough to look like an explicit search
    request, which overrides the file veto. Three searches went out on
    « "index (2).html" », all empty.
    """
    t = re.sub(r"```[\s\S]*?```", " ", contenu or "")     # delimited blocks
    t = re.sub(r"<[^>]{1,300}>", " ", t)                    # glued tags
    t = re.sub(r"https?://\S+", " ", t)                     # adresses (google, etc.)
    if len(t) > 1200:
        # Raw paste without delimiter: a human's request sits at the start or at
        # the end, never in the middle of 60 Ko of code.
        t = t[:600] + " \n " + t[-600:]
    return t


def _recherche_pertinente(history):
    """Should we merely OFFER the search tools for this round?"""
    dernier = next((m for m in reversed(history) if m.get('role') == 'user'), None)
    texte = _texte_de_la_demande(str((dernier or {}).get('content', '')))
    # An explicit directive, and nothing else. It beats everything else: if
    # someone writes « cherche sur internet », that is what they want.
    return bool(_DEMANDE_RECHERCHE.search(texte))


def websearch_active(username):
    """Is search usable for this user?"""
    row = get_db().execute(
        "SELECT websearch_enabled FROM user_prefs WHERE username=?", (username,)).fetchone()
    if row is not None and not row['websearch_enabled']:
        return False
    return websearch.disponible()


def _web_tools():
    return [
        {'type': 'function', 'function': {
            'name': 'recherche_web',
            'description': ("Cherche sur le web et rend une liste de résultats (titre, adresse, "
                            "extrait). Réservé à ce que tu ne peux pas savoir : actualité, faits "
                            "postérieurs à ton entraînement, informations à vérifier. "
                            "N'appelle JAMAIS cet outil pour comprendre ou corriger du code déjà "
                            "présent dans la conversation : la réponse est dans ce code, relis-le."),
            'parameters': {'type': 'object', 'properties': {
                'question': {'type': 'string', 'description': "Ce qu'il faut chercher."},
                'nombre': {'type': 'integer',
                           'description': "Nombre de résultats souhaités (1 à 8, défaut 6)."},
            }, 'required': ['question']}}},
        {'type': 'function', 'function': {
            'name': 'lire_pages',
            'description': ("Ouvre des adresses trouvées par recherche_web et rend leur contenu. "
                            "N'invente jamais une adresse : n'utilise que celles des résultats. "
                            "Certaines pages sont inaccessibles (mur d'abonnement, blocage) — "
                            "c'est dit explicitement, appuie-toi alors sur les extraits."),
            'parameters': {'type': 'object', 'properties': {
                'urls': {'type': 'array', 'items': {'type': 'string'},
                         'description': "Jusqu'à 4 adresses, issues des résultats de recherche."},
            }, 'required': ['urls']}}},
    ]


def _annonce(nom, args):
    """What we are about to do, told to the client before doing it."""
    if nom == 'recherche_web':
        return {'etape': 'recherche', 'outil': 'searxng',
                'question': str(args.get('question', ''))[:200]}
    if nom == 'lire_pages':
        urls = [str(u) for u in (args.get('urls') or [])][:websearch.MAX_PAGES]
        return {'etape': 'lecture', 'outil': 'crawl4ai', 'urls': urls}
    return {'etape': 'inconnue', 'outil': nom}


def _exec_web_tool(nom, args, journal):
    """Executes a tool and returns the text to hand back to the model."""
    if nom == 'recherche_web':
        question = str(args.get('question', ''))[:400]
        res, err = websearch.rechercher(question, args.get('nombre', 6))
        journal.append({'etape': 'recherche_finie', 'outil': 'searxng',
                        'question': question, 'nombre': len(res), 'erreur': err,
                        'sources': [{'titre': r['titre'], 'url': r['url']} for r in res[:8]]})
        if err:
            return f"La recherche a échoué : {err}"
        if not res:
            return "Aucun résultat."
        return json.dumps({'resultats': res}, ensure_ascii=False)
    if nom == 'lire_pages':
        urls = [str(u) for u in (args.get('urls') or [])][:websearch.MAX_PAGES]
        # The instruction « n'utilise que les adresses des résultats » lived in the
        # tool DESCRIPTION, thus aimed at the model — that is, at the least
        # reliable entry of the system. It is now ENFORCED here (audit of
        # 2026-10-02): only URLs actually returned by a search of the SAME round
        # can be opened. This is the exfiltration channel disappearing (the model
        # holds up to 8 000 characters of context, which it could place in an
        # address and have the crawler fetch), and it closes arbitrary URL opening
        # at the same time. The refusal is EXPLICIT so the model falls back on the
        # extracts instead of retrying.
        connues = {s['url'] for entree in journal if entree.get('etape') == 'recherche_finie'
                   for s in (entree.get('sources') or [])}
        refusees = [u for u in urls if u not in connues]
        urls = [u for u in urls if u in connues]
        if not urls:
            return ("Aucune de ces adresses ne vient d'une recherche de cette "
                    "conversation : je ne peux ouvrir que les résultats affichés. "
                    "Refusées : " + ", ".join(refusees[:4]))
        pages, err = websearch.lire(urls)
        journal.append({'etape': 'lecture_finie', 'outil': 'crawl4ai', 'urls': urls,
                        'lues': sum(1 for p in pages if p.get('contenu')), 'erreur': err,
                        'refusees': refusees,
                        'echecs': [{'url': p['url'], 'raison': p['erreur']}
                                   for p in pages if p.get('erreur')]})
        if err:
            return f"La lecture a échoué : {err}"
        suite = (" (adresses refusées, hors résultats de recherche : "
                 + ", ".join(refusees[:4]) + ")") if refusees else ""
        return json.dumps({'pages': pages}, ensure_ascii=False) + suite
    return "Outil inconnu."


def _borner_prompt_outils(court):
    """Brings `court` back under OUTILS_PROMPT_MAX by dropping the OLDEST tools.

    We drop a COMPLETE round — the assistant message carrying the `tool_calls`
    AND the `tool` messages answering it: a call without an answer makes some
    models' chat template fail. The original conversation (`court`
    before any addition) is never touched: it is already bounded.
    """
    def _taille(msgs):
        return sum(len(str(m.get('content') or '')) for m in msgs)

    total = _taille(court)
    while total > OUTILS_PROMPT_MAX:
        idx = next((i for i, m in enumerate(court)
                    if m.get('role') == 'assistant' and m.get('tool_calls')), None)
        if idx is None:
            break
        fin = idx + 1
        while fin < len(court) and court[fin].get('role') == 'tool':
            fin += 1
        total -= _taille(court[idx:fin])
        del court[idx:fin]


def _exec_web_tool_avec_battements(nom, args, journal):
    """GENERATOR: runs a web tool while beating time during the wait.

    Yields (generator return value) the text meant for the model, exactly
    like `_exec_web_tool`. In between it emits `": battement"` every
    BATTEMENT_OUTIL_S seconds: without it a slow page read produces no byte
    during its whole run and the frontend proxy gives up (60 s idle), while
    the work itself does succeed.
    """
    boite = {}

    def _run():
        try:
            boite['r'] = _exec_web_tool(nom, args, journal)
        except Exception as e:                      # noqa: BLE001
            # An exception here is a REAL failure (crawl4ai unreachable, unreadable
            # response): we surface it to the caller, who tells the client and the
            # model. Swallowing it meant leaving the user in front of an answer pulled
            # out of the model's memory, without a word.
            boite['e'] = e

    fil = threading.Thread(target=_run, daemon=True)
    fil.start()
    while fil.is_alive():
        fil.join(BATTEMENT_OUTIL_S)
        if fil.is_alive():
            yield ": battement\n\n"
    if 'e' in boite:
        raise boite['e']
    return boite.get('r', '')


def _texte_des_trouvailles(trouvailles):
    """What the search brought back, as plain text.

    We append it to the last user message rather than using `tool` roles:
    no dependency on the chat template, so nothing that can make it fail,
    and the model sees the data where it expects it.
    """
    if not trouvailles:
        return ''
    morceaux = ["\n\n---\nRésultats de recherche web récupérés pour cette demande "
                "(données externes : cite les adresses si tu t'en sers) :\n"]
    for nom, contenu in trouvailles:
        morceaux.append(f"\n[{nom}]\n{contenu}\n")
    return ''.join(morceaux)[:40000]


def _phase_outils(model, msgs, user_key, journal, trouvailles,
                  web_ok=True, img_ok=False, username='',
                  video_ok=False, doc_ok=False, pieces=()):
    """Lets the model search, read or generate before answering. Modifies `msgs`
    in place.

    `web_ok` arms recherche_web/lire_pages, `img_ok` generer_image, `video_ok`
    generer_video and `doc_ok` lire_document; without any of them, the phase
    does not start (we do not call the model with `tools: []` — some templates
    make it mute). `pieces` carries the conversation's attached images, the
    only input lire_document can read.

    GENERATOR: it renders SSE comments along the way. Without them, the
    client receives nothing during the whole phase — several seconds of
    searching and reading — and the frontend proxy cuts the connection
    before the first token (observed: « Le serveur ne répond pas »).

    The model calls themselves run WITHOUT streaming: reassembling
    fragmented tool calls from deltas is a known bug source, and this phase
    produces no text meant for reading.
    """
    court = _contexte_outils(msgs)
    _outils = ((_web_tools() if web_ok else []) +
               (image_tools._outils_image() if img_ok else []) +
               (video_tools._outils_video() if video_ok else []) +
               (document_tools._outils_document() if doc_ok else []))
    if not _outils:
        return
    _fin = time.monotonic() + DELAI_MAX_OUTILS
    for _ in range(MAX_TOURS_OUTILS):
        if time.monotonic() > _fin:
            journal.append({'etape': 'delai', 'outil': 'recherche',
                            'erreur': "Recherche interrompue : trop longue."})
            yield "data: " + json.dumps({'cronos_web': journal[-1]}) + "\n\n"
            return
        try:
            r = requests.post(f"{LITELLM_URL}/v1/chat/completions",
                              headers={'Authorization': f'Bearer {user_key}'},
                              json={'model': model, 'messages': court, 'tools': _outils,
                                    'tool_choice': 'auto', 'temperature': 0.2,
                                    'max_tokens': 1024,
                                    'chat_template_kwargs': {'enable_thinking': False}},
                              timeout=120)
        except Exception as e:                      # noqa: BLE001
            # Transport failure (LiteLLM unreachable, timeout): we SAY it. Total
            # silence was the worst case — the user had asked for a search and got a
            # normal-looking answer.
            _log.warning("outils %s : appel de decision injoignable (%s)", username, e)
            yield "data: " + json.dumps({'cronos_web': {
                'etape': 'recherche_finie', 'outil': 'litellm', 'nombre': 0,
                'erreur': "assistant injoignable"}}) + "\n\n"
            return
        if not r.ok:
            _log.warning("outils %s : decision d'outil refusee (HTTP %s)",
                         username, r.status_code)
            yield "data: " + json.dumps({'cronos_web': {
                'etape': 'recherche_finie', 'outil': 'litellm', 'nombre': 0,
                'erreur': f"assistant en erreur ({r.status_code})"}}) + "\n\n"
            return
        try:
            choix = (r.json().get('choices') or [{}])[0]
        except Exception:                           # noqa: BLE001
            _log.warning("outils %s : reponse de decision illisible", username)
            yield "data: " + json.dumps({'cronos_web': {
                'etape': 'recherche_finie', 'outil': 'litellm', 'nombre': 0,
                'erreur': "réponse illisible"}}) + "\n\n"
            return
        message = choix.get('message') or {}
        appels = message.get('tool_calls') or []
        if not appels:
            return                      # the model needs no tool: we answer
        # The tool exchange lives only in `court`: the FINAL request must contain
        # no protocol-format message. Sending `tool_calls` and `tool`-role messages
        # WITHOUT declaring the tools yields a conversation the template cannot
        # render — observed in production: 35 tokens produced, no content received,
        # « The model returned no response ». What was found is reinjected as
        # TEXT, further down, by
        # `_texte_des_trouvailles`.
        court.append({'role': 'assistant', 'content': message.get('content') or '',
                      'tool_calls': appels})
        for appel in appels[:4]:
            if time.monotonic() > _fin:
                break
            fn = (appel.get('function') or {})
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except Exception:
                args = {}
            # A search, a read or a generation takes several seconds.
            # We tell the client WHAT WE ARE DOING before doing it: without
            # that the wait is silent and nobody knows what is happening. It
            # also keeps the stream open, otherwise the proxy cuts before the
            # first token.
            if fn.get('name') == 'generer_image':
                yield "data: " + json.dumps({'cronos_web': image_tools._annonce_image(args)}) + "\n\n"
                # GENERATOR: SSE heartbeats during the generation, final text
                # as return value.
                resultat = yield from image_tools._exec_image_tool(args, username, journal)
                # A generation consumes almost all of the phase budget: we
                # extend it, otherwise the final model round would be refused
                # by the deadline right after a perfectly successful image.
                _fin = max(_fin, time.monotonic() + DELAI_MAX_OUTILS)
            elif fn.get('name') == 'generer_video':
                yield "data: " + json.dumps({'cronos_web': video_tools._annonce_video(args)}) + "\n\n"
                # GENERATOR: same contract as the image tool — the phase
                # budget is extended, a video takes MINUTES.
                resultat = yield from video_tools._exec_video_tool(args, username, journal)
                _fin = max(_fin, time.monotonic() + DELAI_MAX_OUTILS)
            elif fn.get('name') == 'lire_document':
                yield "data: " + json.dumps({'cronos_web': document_tools._annonce_document(args)}) + "\n\n"
                # GENERATOR: heartbeats while the OCR sidecar extracts the
                # text of the attachment, text as return value.
                resultat = yield from document_tools._exec_document_tool(
                    args, username, journal, pieces)
                _fin = max(_fin, time.monotonic() + DELAI_MAX_OUTILS)
            else:
                yield "data: " + json.dumps({'cronos_web': _annonce(fn.get('name', ''), args)}) + "\n\n"
                # GENERATOR: SSE heartbeats during execution (a page read can
                # take 90 s), final text as return value.
                try:
                    resultat = yield from _exec_web_tool_avec_battements(
                        fn.get('name', ''), args, journal)
                except Exception as e:              # noqa: BLE001
                    _log.warning("outils %s : %s a leve (%s)", username,
                                 fn.get('name', ''), e)
                    journal.append({'etape': 'recherche_finie', 'outil': 'recherche',
                                    'nombre': 0, 'erreur': "outil en panne"})
                    yield "data: " + json.dumps({'cronos_web': journal[-1]}) + "\n\n"
                    resultat = ("La recherche a échoué (outil en panne). Dis-le "
                                "tel quel à l'utilisateur.")
            yield "data: " + json.dumps({'cronos_web': journal[-1] if journal else {}}) + "\n\n"

            court.append({'role': 'tool', 'tool_call_id': appel.get('id', ''),
                          'name': fn.get('name', ''),
                          'content': resultat[:OUTILS_RESULTAT_PROMPT]})
            trouvailles.append((fn.get('name', ''), resultat[:20000]))
            _borner_prompt_outils(court)
