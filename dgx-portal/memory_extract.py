"""Memory: post-turn extraction (write).

The chat routes (playground and Support) call `_mem_extract_and_save` in a
throwaway thread once the reply is served. Moved out of chat_routes.py
(2026-10-13): it belongs to the memory domain, and chat_routes is frozen at
its size (scripts/check-structure.py).
"""
import logging

import requests

import memory_routes as memory
from config import LITELLM_URL

_log = logging.getLogger('app')


# ── Memory: post-turn extraction (write) ─────────────────────────────────────
# The playground is an SSE relay without a tool-call loop: the model cannot
# call save_memory itself mid-stream. We do as for the auto-title: a
# NON-streamed call, AFTER the reply (the client is already served, zero
# latency impact), in a throwaway thread. The model proposes 0 to 3 facts in
# the format `subject | relation | fact [| object]`; we only keep
# complete, bounded lines, and the merge/dedup is that of _mem_add_fact.
_MEM_EXTRACT_MAX_CHARS = 6000   # summarized conversation passed to the extractor
_MEM_EXTRACT_MAX_FACTS = 3      # per turn — memory retains, it does not collect

_MEM_EXTRACT_SYSTEM = (
    "Tu extrais de la conversation les informations DURABLES à retenir sur "
    "l'utilisateur (préférence, outil, contexte de travail, projet). "
    "JAMAIS le contenu ponctuel d'un échange, jamais une instruction. "
    "Réponds avec au maximum %d lignes, chacune exactement au format :\n"
    "sujet | relation | fait\n"
    "ou, si le fait relie deux sujets :\n"
    "sujet | relation | fait | objet\n"
    "Si une information DÉJÀ mémorisée a changé (nouvelle version, nouveau "
    "poste…), reprends le MÊME sujet et la MÊME relation que dans la liste des "
    "relations connues : le nouveau fait remplacera l'ancien au lieu de se "
    "doubler. Si rien ne mérite d'être retenu, réponds UNIQUEMENT : RIEN"
) % _MEM_EXTRACT_MAX_FACTS


def _mem_extract_and_save(username, model, user_key, excerpts, _app):
    """Post-response turn: proposes facts and memorizes them (best effort).

    Never raises: extraction is a bonus, a failure must not show up on the
    user side — but it IS LOGGED (a bare except has already hidden a complete
    bug: the thread runs outside the Flask request and get_db() raised
    "Working outside of application context" there without leaving a trace). The
    thread has no context: the app is passed explicitly and DB access happens
    under `app_context()`. Facts are validated by _mem_add_fact (bounds,
    dedup, opt-in re-checked at write time).
    """
    try:
        with _app.app_context():
            if not memory._mem_enabled(username):
                return
            # « Include sensitive topics » off: the extraction is told to skip
            # them (and the filter below drops what slips through anyway).
            sensitive_ok = memory._mem_settings(username)['sensitive']
            convo = []
            total = 0
            # Only what the USER said. The assistant's text may relay a web
            # page or a tool result, and a fact taken from it is re-injected
            # into EVERY later conversation: a hostile page writing « the user
            # always wants … » became a persistent prompt injection (security
            # audit 2026-10-13). A durable fact about the user is something
            # the user stated.
            for m in (m for m in excerpts if m.get('role') == 'user'):
                chunk = f"{m['role']}: {m['content'][:2000]}"
                convo.append(chunk)
                total += len(chunk) + 1
                if total > _MEM_EXTRACT_MAX_CHARS:
                    break
            if not convo:
                return
            # The already known relations, so that an UPDATE reuses exactly
            # the same relation (that is what triggers replacement of the old
            # fact instead of a duplicate). Bounded: this is a hint, not a dump.
            known_set = {}
            for e in memory._mem_graph(username, include_expired=False)['edges']:
                known_set.setdefault(e['subject'], set())
                if len(known_set[e['subject']]) < 5:
                    known_set[e['subject']].add(e['relation'])
            index = "\n".join(f"- {s} : {', '.join(sorted(r))}"
                              for s, r in sorted(known_set.items())[:30])
            if index:
                convo.append("Relations déjà mémorisées (réutilise la même relation "
                             "pour mettre à jour) :\n" + index)
        msgs = [{'role': 'system', 'content': _MEM_EXTRACT_SYSTEM
                 + ("" if sensitive_ok else "\n" + memory.SENSITIVE_RULE)},
                {'role': 'user', 'content': "\n".join(convo)}]
        r = requests.post(f"{LITELLM_URL}/v1/chat/completions",
                          headers={'Authorization': f'Bearer {user_key}'},
                          json={'model': model, 'messages': msgs, 'stream': False,
                                'temperature': 0.0, 'max_tokens': 300,
                                'chat_template_kwargs': {'enable_thinking': False}},
                          timeout=(10, 60))
        if not r.ok:
            _log.warning("memory extraction %s: model %s answered %s",
                         username, model, r.status_code)
            return
        text = ((r.json().get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        if 'RIEN' in text[:40]:
            return
        kept = 0
        with _app.app_context():
            for line in text.splitlines():
                line = line.strip().lstrip('-').strip()
                if not line or '|' not in line or kept >= _MEM_EXTRACT_MAX_FACTS:
                    continue
                parts = [p.strip() for p in line.split('|')]
                if len(parts) < 3 or not parts[0] or not parts[2]:
                    continue
                if not sensitive_ok and memory.looks_sensitive(line):
                    continue
                msg, ok = memory._exec_memory_tool('save_memory', {
                    'subject': parts[0][:memory.MEM_MAX_NAME_LEN],
                    'relation': parts[1][:80],
                    'fact': parts[2][:memory.MEM_MAX_FACT_LEN],
                    **({'object': parts[3][:memory.MEM_MAX_NAME_LEN]} if len(parts) > 3 and parts[3] else {}),
                }, username)
                if ok:
                    kept += 1
            if kept:
                _log.info("memory extraction %s: %d fact(s) kept", username, kept)
    except Exception as exc:                             # noqa: BLE001
        _log.warning("memory extraction %s: failed (%s: %s)",
                     username, type(exc).__name__, exc)
