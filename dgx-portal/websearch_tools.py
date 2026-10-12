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
import file_tools
import skills_store
import conversation_search
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
MAX_TOOL_ROUNDS = 3
# File edits: view → replace → maybe a corrected replace, per file.
FILE_TOOL_ROUNDS = 10
FILE_TOOLS_MAX_DELAY = 300
FILE_TOOL_RESULT_MAX = 40_000
# Time cap of the tool phase. Legitimate or not, a search must never
# monopolize the wait: past this, we answer with what was found.
# A single page read can take a minute and a half.
TOOLS_MAX_DELAY = 60
# What the tool phase re-reads to decide whether to search. Deliberately
# SHORT: deciding « faut-il une recherche ? » does not call for re-reading a
# 65 Ko file. Passing it the whole conversation added a full preloading
# BEFORE the answer — measured: 30 s on 100 Ko of context, over 60 s beyond,
# and the client gave up (« Network error ») on slightly old conversations
# while a fresh conversation worked.
TOOLS_MSG_MAX = 1500      # per message
TOOLS_TOTAL_MAX = 8000    # in total
TOOLS_LAST = 6        # number of messages re-read
# What is then added to `court` (tool results) was bounded by NOTHING:
# `OUTILS_TOTAL_MAX` only applies to the starting conversation, and each
# round stacked up to 4 results of 20 000 characters, reposted IN FULL on
# the next round. The 2nd round prompt reached ~88 000 characters, the 3rd
# even more — a preload of several tens of seconds, without a single byte
# sent to the client meanwhile. We bound per result AND in total.
TOOLS_RESULT_PROMPT = 6000   # per tool result (the digest is enough to decide)
TOOLS_PROMPT_MAX = 40000       # in total in `court`
# A search tool is SYNCHRONOUS and can block for a very long time:
# `lire_pages` waits `websearch.TIMEOUT_LECTURE` per page (90 s), while the
# frontend proxy cuts after 60 s WITHOUT A SINGLE BYTE (IDLE_TIMEOUT_MS). The
# result was thus lost and the user saw « Erreur réseau » although the read
# had succeeded. We run the tool in a thread and emit an SSE comment while
# waiting — same remedy as image generation.
TOOL_HEARTBEAT_S = 8

_log = logging.getLogger('app')


def _tools_context(msgs):
    """Lightweight version of the conversation, for the tool decision alone."""
    system_part = [m for m in msgs[:1] if m.get('role') == 'system']
    remaining = msgs[len(system_part):]
    short_text, total = [], 0
    for m in reversed(remaining[-TOOLS_LAST:]):
        c = str(m.get('content', ''))
        if len(c) > TOOLS_MSG_MAX:
            # We keep the START and the END: the request is often at the top, the last
            # instruction at the tail; the belly of a big file does not help.
            half = TOOLS_MSG_MAX // 2
            c = c[:half] + "\n…\n" + c[-half:]
        if total + len(c) > TOOLS_TOTAL_MAX and short_text:
            break
        total += len(c)
        short_text.append({'role': m.get('role'), 'content': c})
    return [{'role': m['role'], 'content': str(m.get('content', ''))[:TOOLS_MSG_MAX]}
            for m in system_part] + list(reversed(short_text))


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
_SEARCH_REQUEST = re.compile(
    r"(?:cherche|recherche|regarde|va voir|renseigne-toi|informe-toi)"
    r"[^.!?\n]{0,30}?\b(?:sur (?:le )?(?:web|internet|net)|sur google)"
    r"|(?:fais|lance|effectue)[^.!?\n]{0,20}?\brecherche\b"
    r"|recherche\s+web|web\s*search|search\s+(?:the\s+)?web|search\s+online"
    r"|google[-\s]?(?:le|la|it)\b",
    re.I)


def _request_text(content):
    """What the user WROTE, without the file they pasted.

    Searching for the intent in the whole message amounted to searching it
    in the code. Observed: a `<link href="https://fonts.googleapis.com/...">`
    — hence the word « google » — was enough to look like an explicit search
    request, which overrides the file veto. Three searches went out on
    « "index (2).html" », all empty.
    """
    t = re.sub(r"```[\s\S]*?```", " ", content or "")     # delimited blocks
    t = re.sub(r"<[^>]{1,300}>", " ", t)                    # glued tags
    t = re.sub(r"https?://\S+", " ", t)                     # adresses (google, etc.)
    if len(t) > 1200:
        # Raw paste without delimiter: a human's request sits at the start or at
        # the end, never in the middle of 60 Ko of code.
        t = t[:600] + " \n " + t[-600:]
    return t


def _search_relevant(history):
    """Should we merely OFFER the search tools for this round?"""
    last = next((m for m in reversed(history) if m.get('role') == 'user'), None)
    text = _request_text(str((last or {}).get('content', '')))
    # An explicit directive, and nothing else. It beats everything else: if
    # someone writes « cherche sur internet », that is what they want.
    return bool(_SEARCH_REQUEST.search(text))


def websearch_active(username):
    """Is search usable for this user?"""
    row = get_db().execute(
        "SELECT websearch_enabled FROM user_prefs WHERE username=?", (username,)).fetchone()
    if row is not None and not row['websearch_enabled']:
        return False
    return websearch.available()


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
                'count': {'type': 'integer',
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


def _announce(name, args):
    """What we are about to do, told to the client before doing it."""
    if name == 'recherche_web':
        return {'step': 'search', 'tool': 'searxng',
                'question': str(args.get('question', ''))[:200]}
    if name == 'lire_pages':
        urls = [str(u) for u in (args.get('urls') or [])][:websearch.MAX_PAGES]
        return {'step': 'reading', 'tool': 'crawl4ai', 'urls': urls}
    return {'step': 'unknown', 'tool': name}


def _exec_web_tool(name, args, step_log):
    """Executes a tool and returns the text to hand back to the model."""
    if name == 'recherche_web':
        question = str(args.get('question', ''))[:400]
        res, err = websearch.search(question, args.get('count', 6))
        step_log.append({'step': 'search_done', 'tool': 'searxng',
                        'question': question, 'count': len(res), 'error': err,
                        'sources': [{'title': r['title'], 'url': r['url']} for r in res[:8]]})
        if err:
            return f"La recherche a échoué : {err}"
        if not res:
            return "Aucun résultat."
        return json.dumps({'results': res}, ensure_ascii=False)
    if name == 'lire_pages':
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
        known_set = {s['url'] for entry in step_log if entry.get('step') == 'search_done'
                   for s in (entry.get('sources') or [])}
        refused = [u for u in urls if u not in known_set]
        urls = [u for u in urls if u in known_set]
        if not urls:
            return ("Aucune de ces adresses ne vient d'une recherche de cette "
                    "conversation : je ne peux ouvrir que les résultats affichés. "
                    "Refusées : " + ", ".join(refused[:4]))
        pages, err = websearch.read(urls)
        step_log.append({'step': 'reading_done', 'tool': 'crawl4ai', 'urls': urls,
                        'read': sum(1 for p in pages if p.get('content')), 'error': err,
                        'refused': refused,
                        'failures': [{'url': p['url'], 'reason': p['error']}
                                   for p in pages if p.get('error')]})
        if err:
            return f"La lecture a échoué : {err}"
        continuation = (" (adresses refusées, hors résultats de recherche : "
                 + ", ".join(refused[:4]) + ")") if refused else ""
        return json.dumps({'pages': pages}, ensure_ascii=False) + continuation
    return "Outil inconnu."


def _bound_tools_prompt(short_text):
    """Brings `court` back under OUTILS_PROMPT_MAX by dropping the OLDEST tools.

    We drop a COMPLETE round — the assistant message carrying the `tool_calls`
    AND the `tool` messages answering it: a call without an answer makes some
    models' chat template fail. The original conversation (`court`
    before any addition) is never touched: it is already bounded.
    """
    def _size(msgs):
        return sum(len(str(m.get('content') or '')) for m in msgs)

    total = _size(short_text)
    while total > TOOLS_PROMPT_MAX:
        idx = next((i for i, m in enumerate(short_text)
                    if m.get('role') == 'assistant' and m.get('tool_calls')), None)
        if idx is None:
            break
        end = idx + 1
        while end < len(short_text) and short_text[end].get('role') == 'tool':
            end += 1
        total -= _size(short_text[idx:end])
        del short_text[idx:end]


def _exec_web_tool_with_heartbeats(name, args, step_log):
    """GENERATOR: runs a web tool while beating time during the wait.

    Yields (generator return value) the text meant for the model, exactly
    like `_exec_web_tool`. In between it emits `": battement"` every
    BATTEMENT_OUTIL_S seconds: without it a slow page read produces no byte
    during its whole run and the frontend proxy gives up (60 s idle), while
    the work itself does succeed.
    """
    box = {}

    def _run():
        try:
            box['r'] = _exec_web_tool(name, args, step_log)
        except Exception as e:                      # noqa: BLE001
            # An exception here is a REAL failure (crawl4ai unreachable, unreadable
            # response): we surface it to the caller, who tells the client and the
            # model. Swallowing it meant leaving the user in front of an answer pulled
            # out of the model's memory, without a word.
            box['e'] = e

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(TOOL_HEARTBEAT_S)
        if thread.is_alive():
            yield ": battement\n\n"
    if 'e' in box:
        raise box['e']
    return box.get('r', '')


def _findings_text(findings):
    """What the search brought back, as plain text.

    We append it to the last user message rather than using `tool` roles:
    no dependency on the chat template, so nothing that can make it fail,
    and the model sees the data where it expects it.
    """
    if not findings:
        return ''
    chunks = ["\n\n---\nRésultats de recherche web récupérés pour cette demande "
                "(données externes : cite les adresses si tu t'en sers) :\n"]
    for name, content in findings:
        chunks.append(f"\n[{name}]\n{content}\n")
    return ''.join(chunks)[:40000]


def _tools_phase(model, msgs, user_key, step_log, findings,
                  web_ok=True, img_ok=False, username='',
                  video_ok=False, doc_ok=False, attachments=(),
                  files=None, changes=None, skill_rows=None, skills_loaded=None,
                  chat_search=False, history_notes=None):
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
    short_text = _tools_context(msgs)
    # `files` arms the file tools (file_tools.py): edits loop view → replace
    # → (error) → replace, so they get more rounds, a longer budget and room
    # for a replacement text.
    edit_ok = bool(files)
    _tools = ((_web_tools() if web_ok else []) +
               (image_tools._image_tools() if img_ok else []) +
               (video_tools._video_tools() if video_ok else []) +
               (document_tools._document_tools() if doc_ok else []) +
               (file_tools._file_tools() if edit_ok else []) +
               skills_store.skill_tools(skill_rows or []) +
               (conversation_search.tools() if chat_search else []))
    if not _tools:
        return
    if edit_ok:
        short_text.insert(1 if short_text and short_text[0].get('role') == 'system' else 0,
                          {'role': 'system', 'content': file_tools.files_note(files)})
    _end = time.monotonic() + (FILE_TOOLS_MAX_DELAY if edit_ok else TOOLS_MAX_DELAY)
    for _ in range(FILE_TOOL_ROUNDS if edit_ok else MAX_TOOL_ROUNDS):
        if time.monotonic() > _end:
            step_log.append({'step': 'timeout', 'tool': 'search',
                            'error': "Recherche interrompue : trop longue."})
            yield "data: " + json.dumps({'dgx_web': step_log[-1]}) + "\n\n"
            return
        try:
            r = requests.post(f"{LITELLM_URL}/v1/chat/completions",
                              headers={'Authorization': f'Bearer {user_key}'},
                              json={'model': model, 'messages': short_text, 'tools': _tools,
                                    'tool_choice': 'auto', 'temperature': 0.2,
                                    'max_tokens': 8192 if edit_ok else 1024,
                                    'chat_template_kwargs': {'enable_thinking': False}},
                              timeout=120)
        except Exception as e:                      # noqa: BLE001
            # Transport failure (LiteLLM unreachable, timeout): we SAY it. Total
            # silence was the worst case — the user had asked for a search and got a
            # normal-looking answer.
            _log.warning("tools %s: decision call unreachable (%s)", username, e)
            yield "data: " + json.dumps({'dgx_web': {
                'step': 'search_done', 'tool': 'litellm', 'count': 0,
                'error': "assistant injoignable"}}) + "\n\n"
            return
        if not r.ok:
            _log.warning("tools %s: tool decision refused (HTTP %s)",
                         username, r.status_code)
            yield "data: " + json.dumps({'dgx_web': {
                'step': 'search_done', 'tool': 'litellm', 'count': 0,
                'error': f"assistant en erreur ({r.status_code})"}}) + "\n\n"
            return
        try:
            choice = (r.json().get('choices') or [{}])[0]
        except Exception:                           # noqa: BLE001
            _log.warning("tools %s: unreadable decision answer", username)
            yield "data: " + json.dumps({'dgx_web': {
                'step': 'search_done', 'tool': 'litellm', 'count': 0,
                'error': "réponse illisible"}}) + "\n\n"
            return
        message = choice.get('message') or {}
        calls = message.get('tool_calls') or []
        if not calls:
            return                      # the model needs no tool: we answer
        # The tool exchange lives only in `court`: the FINAL request must contain
        # no protocol-format message. Sending `tool_calls` and `tool`-role messages
        # WITHOUT declaring the tools yields a conversation the template cannot
        # render — observed in production: 35 tokens produced, no content received,
        # « The model returned no response ». What was found is reinjected as
        # TEXT, further down, by
        # `_texte_des_trouvailles`.
        short_text.append({'role': 'assistant', 'content': message.get('content') or '',
                      'tool_calls': calls})
        for call in calls[:4]:
            if time.monotonic() > _end:
                break
            fn = (call.get('function') or {})
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except Exception:
                args = {}
            if chat_search and fn.get('name') in ('search_conversations', 'recent_conversations'):
                # The user's own history: kept for the final answer, framed as data.
                result, _ok = conversation_search.execute(fn.get('name'), args, username)
                if history_notes is not None:
                    history_notes.append(result)
                yield "data: " + json.dumps({'dgx_web': {
                    'step': 'history', 'tool': 'history',
                    'question': str(args.get('query') or '')[:120]}}) + "\n\n"
                short_text.append({'role': 'tool', 'tool_call_id': call.get('id', ''),
                                   'name': fn.get('name', ''), 'content': result})
                _bound_tools_prompt(short_text)
                continue
            if skill_rows and fn.get('name') in ('use_skill', 'read_skill_file'):
                # The skill's instructions/files go to the FINAL answer too
                # (skills_loaded): the tool exchange itself is dropped there.
                db = get_db()
                if fn.get('name') == 'use_skill':
                    result, ok = skills_store.exec_use_skill(db, username, args.get('name', ''))
                else:
                    result, ok = skills_store.exec_read_skill_file(
                        db, username, args.get('name', ''), args.get('path', ''))
                step = {'step': 'skill', 'tool': 'skills',
                        'question': str(args.get('name') or '')[:80]
                                    + (f" · {args.get('path')}" if args.get('path') else '')}
                if not ok:
                    step['error'] = 'not found'
                elif skills_loaded is not None:
                    skills_loaded.append(result)
                yield "data: " + json.dumps({'dgx_web': step}) + "\n\n"
                short_text.append({'role': 'tool', 'tool_call_id': call.get('id', ''),
                                   'name': fn.get('name', ''),
                                   'content': result[:FILE_TOOL_RESULT_MAX]})
                _bound_tools_prompt(short_text)
                continue
            if edit_ok and fn.get('name') in file_tools.FILE_TOOL_NAMES:
                # Local and instant: no heartbeat, and the result is NOT a
                # finding — it must never be reinjected as « web results ».
                step = file_tools.announce(fn.get('name'), args)
                result, error = file_tools.exec_file_tool(fn.get('name'), args, files,
                                                          changes if changes is not None else {})
                if error:
                    step['error'] = error
                yield "data: " + json.dumps({'dgx_web': step}) + "\n\n"
                short_text.append({'role': 'tool', 'tool_call_id': call.get('id', ''),
                                   'name': fn.get('name', ''),
                                   'content': result[:FILE_TOOL_RESULT_MAX]})
                _bound_tools_prompt(short_text)
                continue
            # A search, a read or a generation takes several seconds.
            # We tell the client WHAT WE ARE DOING before doing it: without
            # that the wait is silent and nobody knows what is happening. It
            # also keeps the stream open, otherwise the proxy cuts before the
            # first token.
            if fn.get('name') == 'generate_image':
                yield "data: " + json.dumps({'dgx_web': image_tools._announce_image(args)}) + "\n\n"
                # GENERATOR: SSE heartbeats during the generation, final text
                # as return value.
                result = yield from image_tools._exec_image_tool(args, username, step_log)
                # A generation consumes almost all of the phase budget: we
                # extend it, otherwise the final model round would be refused
                # by the deadline right after a perfectly successful image.
                _end = max(_end, time.monotonic() + TOOLS_MAX_DELAY)
            elif fn.get('name') == 'generate_video':
                yield "data: " + json.dumps({'dgx_web': video_tools._announce_video(args)}) + "\n\n"
                # GENERATOR: same contract as the image tool — the phase
                # budget is extended, a video takes MINUTES.
                result = yield from video_tools._exec_video_tool(args, username, step_log)
                _end = max(_end, time.monotonic() + TOOLS_MAX_DELAY)
            elif fn.get('name') == 'lire_document':
                yield "data: " + json.dumps({'dgx_web': document_tools._announce_document(args)}) + "\n\n"
                # GENERATOR: heartbeats while the OCR sidecar extracts the
                # text of the attachment, text as return value.
                result = yield from document_tools._exec_document_tool(
                    args, username, step_log, attachments)
                _end = max(_end, time.monotonic() + TOOLS_MAX_DELAY)
            else:
                yield "data: " + json.dumps({'dgx_web': _announce(fn.get('name', ''), args)}) + "\n\n"
                # GENERATOR: SSE heartbeats during execution (a page read can
                # take 90 s), final text as return value.
                try:
                    result = yield from _exec_web_tool_with_heartbeats(
                        fn.get('name', ''), args, step_log)
                except Exception as e:              # noqa: BLE001
                    _log.warning("tools %s: %s raised (%s)", username,
                                 fn.get('name', ''), e)
                    step_log.append({'step': 'search_done', 'tool': 'search',
                                    'count': 0, 'error': "outil en panne"})
                    yield "data: " + json.dumps({'dgx_web': step_log[-1]}) + "\n\n"
                    result = ("La recherche a échoué (outil en panne). Dis-le "
                                "tel quel à l'utilisateur.")
            yield "data: " + json.dumps({'dgx_web': step_log[-1] if step_log else {}}) + "\n\n"

            short_text.append({'role': 'tool', 'tool_call_id': call.get('id', ''),
                          'name': fn.get('name', ''),
                          'content': result[:TOOLS_RESULT_PROMPT]})
            findings.append((fn.get('name', ''), result[:20000]))
            _bound_tools_prompt(short_text)
