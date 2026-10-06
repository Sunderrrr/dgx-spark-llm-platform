"""LiteLLM client: API keys, budgets, user accounts.

Extracted from app.py on 28/08, from the « Helpers » banner which really
mixed three topics — this client, runner driving, and sidecar management.
Only the LiteLLM functions are here; add_announcement, comfyui_is_up and
get_voice_model, which were interleaved in the same lines, stayed in
app.py.

`_log` replaces `app.logger`: Flask exposes app.logger as
logging.getLogger(module_name), i.e. exactly logging.getLogger('app') here.
It is the SAME object — messages go to the same place as before, without
having to import the application (which would recreate a cycle).
"""
import hashlib
import logging
import sqlite3

import requests

from config import (AUTO_MODEL_NAME, KEY_BUDGET, KEY_DURATION, LITELLM_KEY,
                    LITELLM_URL, VLLM_API_BASE)
from db import DB_PATH, _spend_conn, get_setting
from vllm_health import ctx_split

_log = logging.getLogger('app')

def litellm_headers():
    return {'Authorization': f'Bearer {LITELLM_KEY}', 'Content-Type': 'application/json'}

def get_user_keys(username):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        local_keys = conn.execute(
            "SELECT key_alias, key_value, created_at FROM api_keys WHERE username=? ORDER BY created_at DESC",
            (username,)
        ).fetchall()
        conn.close()
    except Exception:
        return []
    infos = _infos_cles([k['key_value'] for k in local_keys])
    result = []
    for k in local_keys:
        depuis_litellm = infos.get(k['key_value'], {})
        result.append({
            'key_alias': k['key_alias'],
            'key': k['key_value'],
            'created_at': k['created_at'],
            'spend': depuis_litellm.get('spend', 0),
            'max_budget': depuis_litellm.get('max_budget'),
            'budget_reset_at': depuis_litellm.get('budget_reset_at'),
            # Last call with this key (LiteLLM's `last_active` column).
            # It was missing: one could not tell the key used this morning from one
            # created six months ago and never used — so no way to know which one to
            # revoke safely. `None` = never used according to LiteLLM, which is
            # information, not a gap.
            'last_active': depuis_litellm.get('last_active'),
        })
    return result

def _infos_cles(cles):
    """spend / max_budget / budget_reset_at for a list of keys IN CLEAR.

    Reads the LiteLLM database directly rather than its HTTP /key/info
    endpoint. Two reasons:

    1. LEAK. /key/info only accepts GET with the key as a URL parameter
       (POST answers 405, checked), and LiteLLM's access log records the
       full URL: `docker logs litellm` therefore exposed valid, usable API
       keys to anyone with access to the Docker daemon. Observed in prod on
       23/08.
    2. COST. The caller loops over a user's keys: it was one HTTP round-trip
       PER key, here a single query.

    LiteLLM stores the sha256 of the key, never the key: we hash to join.
    Returns {cle_en_clair: {...}}; a missing key simply has no entry.
    """
    cles = [c for c in cles if c]
    if not cles:
        return {}
    par_hash = {hashlib.sha256(c.encode()).hexdigest(): c for c in cles}
    conn = _spend_conn()
    if not conn:
        _log.warning("infos cles : base LiteLLM injoignable, budgets affiches a 0")
        return {}
    try:
        cur = conn.cursor()
        cur.execute('SELECT token, spend, max_budget, budget_reset_at, last_active '
                    'FROM "LiteLLM_VerificationToken" WHERE token = ANY(%s)',
                    (list(par_hash),))
        # `last_active` can be NULL (key never used): we keep the None, the UI
        # renders « Jamais utilisée ». The type varies with the LiteLLM version
        # (timestamp or text): we accept both — an exception here would drop the
        # WHOLE block into the `except`, thus showing 0 spent for keys that did
        # consume.
        return {par_hash[t]: {'spend': sp or 0, 'max_budget': mb,
                              'budget_reset_at': br or '',
                              'last_active': (la.isoformat() if hasattr(la, 'isoformat')
                                              else (str(la) if la else None))}
                for t, sp, mb, br, la in cur.fetchall() if t in par_hash}
    except Exception as e:                                   # noqa: BLE001
        _log.warning("infos cles : lecture LiteLLM impossible (%s)", type(e).__name__)
        return {}
    finally:
        conn.close()

def _ensure_litellm_user(username, max_budget, budget_duration):
    """Create/update the LiteLLM user with an ACCOUNT budget, shared by all
    their keys (user_id). If the user already exists WITH a budget, hands off —
    only the amount may have been adjusted by an admin. An existing user
    WITHOUT any budget (accounts predating the feature, or users created by
    side paths) is repaired with the default — otherwise it stays uncapped
    forever (real hole found on 2026-09-08).
    """
    body = {"user_id": username, "metadata": {"created_by": "dgx-portal"}}
    try:
        # /user/info already exists? otherwise we create it with the default budget.
        info = _litellm_user_info(username)
        if info.get('exists'):
            if info.get('max_budget') is None:
                # Repair: existing account that never got a budget.
                requests.post(f"{LITELLM_URL}/user/update", headers=litellm_headers(),
                              json={"user_id": username,
                                    "max_budget": float(max_budget),
                                    "budget_duration": budget_duration}, timeout=8)
            return True
        body["max_budget"] = float(max_budget)
        body["budget_duration"] = budget_duration
        r = requests.post(f"{LITELLM_URL}/user/new", headers=litellm_headers(),
                          json=body, timeout=8)
        return r.status_code < 300
    except Exception:
        return False


def _litellm_user_info(username):
    """Budget/spend at the ACCOUNT level (LiteLLM user object)."""
    out = {'spend': 0, 'max_budget': None, 'budget_reset_at': '', 'exists': False}
    try:
        r = requests.get(f"{LITELLM_URL}/user/info", headers=litellm_headers(),
                         params={'user_id': username}, timeout=5)
        if r.ok:
            d = r.json()
            ui = d.get('user_info') or d
            if ui:
                out['exists'] = True
                out['spend'] = ui.get('spend', 0) or 0
                out['max_budget'] = ui.get('max_budget')
                out['budget_reset_at'] = ui.get('budget_reset_at', '') or ''
    except Exception:
        pass
    return out


def litellm_update_user_budget(username, new_max_budget, budget_duration=None):
    """Updates the account envelope. `budget_duration` is SENT AGAIN at every
    update: a /user/update that does not carry it leaves the account without
    a reset window (the amount would be a lifelong cap, never reset).
    None = keep the account's existing duration (do not send the field)."""
    payload = {'user_id': username, 'max_budget': float(new_max_budget)}
    if budget_duration:
        payload['budget_duration'] = budget_duration
    try:
        r = requests.post(f"{LITELLM_URL}/user/update", headers=litellm_headers(),
                          json=payload, timeout=5)
        return r.ok
    except Exception:
        return False


def delete_litellm_user(username):
    """Deletes the account's LiteLLM envelope (budget + cumulative spend).

    To be called only AFTER revoking its keys: the user object carries no
    authorization of its own, but leaving it in place made an account
    recreated under the same name inherit the previous one's spend — a new
    colleague could thus start above their quota.
    Checked on 2026-09-13: /user/delete accepts {"user_ids": [...]} and answers 200.
    """
    try:
        r = requests.post(f"{LITELLM_URL}/user/delete", headers=litellm_headers(),
                          json={'user_ids': [username]}, timeout=8)
        return r.ok
    except Exception:
        return False


def create_litellm_key(alias, username, is_admin=False):
    payload = {
        "key_alias": alias,
        "metadata": {"user": username, "created_by": "dgx-portal"},
    }
    if not is_admin:
        # Budget at the ACCOUNT level (shared by all the account's keys), not at the
        # key level: the key carries user_id and LiteLLM caps the sum of the user's
        # spend across all their keys.
        _ensure_litellm_user(username,
                             float(get_setting('default_key_budget', KEY_BUDGET)),
                             get_setting('default_key_duration', KEY_DURATION))
        payload["user_id"] = username
    r = requests.post(f"{LITELLM_URL}/key/generate",
                      headers=litellm_headers(), json=payload, timeout=10)
    if r.ok:
        return r.json().get('key')
    return None

def revoke_litellm_key(key_value):
    r = requests.post(f"{LITELLM_URL}/key/delete",
                      headers=litellm_headers(),
                      json={"keys": [key_value]}, timeout=5)
    return r.ok


def renommer_cle_litellm(key_value, nouvel_alias):
    """Updates the key alias AT LiteLLM, not only in the portal.

    The alias is duplicated: the portal keeps a copy (`api_keys.key_alias`)
    to list keys without querying LiteLLM, and LiteLLM shows its own in its
    own dashboard. Updating only one of the two would make the two views
    diverge — the portal would show « portable-nora » where the admin would
    still read « cle-27 ».

    The key travels in the request BODY, never in the URL: this is the rule
    that made us abandon `/key/info` (see `_infos_cles`, leak observed in
    the proxy logs on 23/08).
    """
    try:
        r = requests.post(f"{LITELLM_URL}/key/update", headers=litellm_headers(),
                          json={"key": key_value, "key_alias": nouvel_alias}, timeout=5)
        return r.ok
    except Exception:
        return False


# ── Model registration in LiteLLM ───────────────────────────────────────────
# Brought over from app.py on 28/08. This removes the last patch:
# sidecars.py called app._point_auto_model via a deferred import for lack of
# a way to import it without a cycle. It now lives here, with the rest of
# the LiteLLM client, and sidecars imports it normally.

def _litellm_model_entry(name):
    """(id, full entry) of the LiteLLM entry carrying this model_name.

    The full entry is used to RESTORE the model if its recreation fails (see
    _litellm_upsert): « supprimer puis recreer » leaves, when the creation
    fails, a model that was serving to vanish from routing.
    """
    try:
        r = requests.get(f"{LITELLM_URL}/model/info", headers=litellm_headers(), timeout=5)
        for m in r.json().get('data', []):
            if m.get('model_name') == name:
                return m.get('model_info', {}).get('id'), m
    except Exception:
        pass
    return None, None

def _litellm_model_id(name):
    """LiteLLM id of the model carrying this model_name, or None."""
    return _litellm_model_entry(name)[0]

def _restaurer_modele(entree):
    """Reinstalls a LiteLLM entry as it was (best effort).

    `model_info` is filtered: LiteLLM refuses at creation the fields it
    generates itself (`id`, `db_model`). Returns True if the restoration was
    accepted.
    """
    info = {k: v for k, v in (entree.get('model_info') or {}).items()
            if k not in ('id', 'db_model')}
    corps = {'model_name': entree.get('model_name'),
             'litellm_params': entree.get('litellm_params') or {},
             'model_info': info}
    try:
        r = requests.post(f"{LITELLM_URL}/model/new", headers=litellm_headers(),
                          json=corps, timeout=8)
        return r.status_code < 300
    except Exception:
        return False

def _model_upstream(name, engine):
    """Name actually expected by the backend on :8000 for this model.

    ds4 starts in "thinking" mode by default: it then IGNORES max_tokens
    ("client sampling knobs are ignored like the official API") and generates
    thousands of tokens at ~10 tok/s. Since the engine is single-slot, one
    request blocks the whole platform. So we route to the reserved name
    `deepseek-chat`, which selects the NON-thinking mode (cf. ds4's --help).
    """
    return 'deepseek-chat' if engine == 'ds4' else name

def _litellm_upsert(public_name, upstream, max_input, max_output):
    """Creates (or refreshes) a LiteLLM entry `public_name` routing to the
    model `upstream` served on :8000. Returns True if LiteLLM accepted.
    """
    if not LITELLM_KEY:
        return False
    body = {
        "model_name": public_name,
        "litellm_params": {
            "model": f"openai/{upstream}",
            "api_base": VLLM_API_BASE,
            "api_key": "dummy",
            "input_cost_per_token": 1,
            "output_cost_per_token": 1,
            # The models served here are « thinking models »: without this, the
            # reasoning goes into the response and eats the whole output budget. A
            # request explicitly passing chat_template_kwargs (the playground's
            # « Raisonnement » button) always wins — checked, this setting is only a
            # default.
            "chat_template_kwargs": {"enable_thinking": False},
            # The SAME setting, duplicated, for the Anthropic /v1/messages endpoint
            # (used by Claude Code): LiteLLM's Anthropic adapter ignores the top-level
            # chat_template_kwargs and only forwards extra_body. Without this line, a
            # short call comes back EMPTY — content: [] and stop_reason: max_tokens,
            # the reasoning having eaten the whole budget (measured: 41 tokens against
            # 3).
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
        "model_info": {
            "mode": "chat",
            "supports_function_calling": True,
            "max_input_tokens": max_input,
            "max_output_tokens": max_output,
        },
    }
    try:
        existing, entree_avant = _litellm_model_entry(public_name)
        if existing:
            requests.post(f"{LITELLM_URL}/model/delete", headers=litellm_headers(),
                          json={"id": existing}, timeout=5)
        r = requests.post(f"{LITELLM_URL}/model/new", headers=litellm_headers(),
                          json=body, timeout=8)
        if r.status_code < 300:
            return True
        # The creation failed AFTER the deletion: without restoration, a model that
        # was answering vanishes from routing (the old entry was deleted first, to
        # avoid leaving two entries of the same name). So we put back the old one —
        # captured BEFORE the deletion, otherwise there is nothing left to
        # restaurer.
        if existing and entree_avant:
            restaure = _restaurer_modele(entree_avant)
            _log.warning("LiteLLM : création de « %s » refusée (HTTP %s) — entrée "
                         "précédente %s", public_name, r.status_code,
                         "restaurée" if restaure else "PERDUE")
        return False
    except Exception:
        return False

def _register_litellm_model(name, vllm_args, engine='vllm'):
    """Registers (or refreshes) the model in LiteLLM at runtime. The context is
    deduced from the engine args (--max-model-len for vLLM, --ctx-size for llama.cpp).
    Both serve an OpenAI API on :8000 → same litellm_params.

    NB: registering a model in the CATALOG does not make it run. The
    `auto-model` alias therefore does NOT follow this call — it only follows real launches
    (see _point_auto_model, called from runner_launch).
    """
    max_input, max_output = ctx_split(vllm_args, engine)
    return _litellm_upsert(name, _model_upstream(name, engine), max_input, max_output)

def _point_auto_model(name, vllm_args, engine='vllm'):
    """Re-routes the virtual model `auto-model` to the chat model that was
    just launched, so clients wire this name ONCE and automatically follow
    the current model, without touching their code on each
    switch. The real names stay registered in parallel and still work.
    Called on each successful launch (runner_launch).
    """
    max_input, max_output = ctx_split(vllm_args, engine)
    return _litellm_upsert(AUTO_MODEL_NAME, _model_upstream(name, engine), max_input, max_output)

def _unregister_litellm_model(name):
    """Removes the LiteLLM entry of this model. Returns True if it no longer exists.

    It returned NOTHING (and swallowed its exceptions): the caller announced
    « retiré de LiteLLM » without ever knowing whether it was true, and a
    LiteLLM entry without a catalog row is a routing that no screen allows
    cleaning up anymore. Returning False = « I could not », for the caller
    to say so.
    """
    if not LITELLM_KEY:
        return False
    mid = _litellm_model_id(name)
    if not mid:
        return True                      # nothing to remove: this is the intended result
    try:
        r = requests.post(f"{LITELLM_URL}/model/delete", headers=litellm_headers(),
                          json={"id": mid}, timeout=5)
        return r.status_code < 300
    except Exception:
        return False
