"""Client LiteLLM : cles API, budgets, comptes utilisateurs.

Extrait de app.py le 28/08, depuis la banniere « Helpers » qui melangeait en
realite trois sujets — ce client, le pilotage du runner, et la gestion des
sidecars. Seules les fonctions LiteLLM sont ici ; add_announcement,
comfyui_is_up et get_voice_model, qui etaient intercalees dans les memes lignes,
sont restees dans app.py.

`_log` remplace `app.logger` : Flask expose app.logger comme
logging.getLogger(nom_du_module), soit exactement logging.getLogger('app') ici.
C'est le MEME objet — les messages partent au meme endroit qu'avant, sans avoir
a importer l'application (ce qui recreerait un cycle).
"""
import hashlib
import logging
import sqlite3
import time
from datetime import datetime

import requests

from config import (AUTO_MODEL_NAME, KEY_BUDGET, KEY_DURATION, LITELLM_KEY,
                    LITELLM_URL, VLLM_API_BASE)
from db import DB_PATH, _spend_conn, get_db, get_setting
from discord_notify import _discord_announce
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
        })
    return result

def _infos_cles(cles):
    """spend / max_budget / budget_reset_at pour une liste de cles EN CLAIR.

    Lit directement la base LiteLLM plutot que son endpoint HTTP /key/info.
    Deux raisons :

    1. FUITE. /key/info n'accepte que le GET avec la cle en parametre d'URL
       (le POST repond 405, verifie), et le journal d'acces de LiteLLM
       enregistre l'URL complete : `docker logs litellm` exposait donc des cles
       API valides et utilisables a quiconque a acces au demon Docker.
       Constate en prod le 23/08.
    2. COUT. L'appelant boucle sur les cles d'un utilisateur : c'etait un
       aller-retour HTTP PAR cle, ici une seule requete.

    LiteLLM stocke le sha256 de la cle, jamais la cle : on hache pour joindre.
    Renvoie {cle_en_clair: {...}} ; une cle absente n'a simplement pas d'entree.
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
        cur.execute('SELECT token, spend, max_budget, budget_reset_at '
                    'FROM "LiteLLM_VerificationToken" WHERE token = ANY(%s)',
                    (list(par_hash),))
        return {par_hash[t]: {'spend': sp or 0, 'max_budget': mb, 'budget_reset_at': br or ''}
                for t, sp, mb, br in cur.fetchall() if t in par_hash}
    except Exception as e:                                   # noqa: BLE001
        _log.warning("infos cles : lecture LiteLLM impossible (%s)", type(e).__name__)
        return {}
    finally:
        conn.close()

def _ensure_litellm_user(username, max_budget, budget_duration):
    """Create/update the LiteLLM user with an ACCOUNT budget, shared by all
    their keys (user_id). Does not overwrite the budget if the user already exists —
    only the amount may have been adjusted by an admin.
    """
    body = {"user_id": username, "metadata": {"created_by": "dgx-portal"}}
    try:
        # /user/info already exists? otherwise we create it with the default budget.
        info = _litellm_user_info(username)
        if info.get('exists'):
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


def litellm_update_user_budget(username, new_max_budget):
    try:
        r = requests.post(f"{LITELLM_URL}/user/update", headers=litellm_headers(),
                          json={'user_id': username, 'max_budget': float(new_max_budget)},
                          timeout=5)
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

def litellm_key_info(key_value):
    """cf. _infos_cles : lecture en base, jamais la cle dans une URL."""
    return _infos_cles([key_value]).get(key_value, {})

def litellm_update_key_budget(key_value, new_max_budget):
    try:
        r = requests.post(f"{LITELLM_URL}/key/update", headers=litellm_headers(),
                          json={'key': key_value, 'max_budget': new_max_budget}, timeout=5)
        return r.ok
    except Exception:
        return False

def revoke_litellm_key(key_value):
    r = requests.post(f"{LITELLM_URL}/key/delete",
                      headers=litellm_headers(),
                      json={"keys": [key_value]}, timeout=5)
    return r.ok


# ── Enregistrement des modeles dans LiteLLM ──────────────────────────────────
# Rapatrie de app.py le 28/08. C'est ce qui supprime la derniere rustine :
# sidecars.py appelait app._point_auto_model par import differe faute de
# pouvoir l'importer sans cycle. Il vit desormais ici, avec le reste du
# client LiteLLM, et sidecars l'importe normalement.

def _litellm_model_id(name):
    """LiteLLM id of the model carrying this model_name, or None."""
    try:
        r = requests.get(f"{LITELLM_URL}/model/info", headers=litellm_headers(), timeout=5)
        for m in r.json().get('data', []):
            if m.get('model_name') == name:
                return m.get('model_info', {}).get('id')
    except Exception:
        pass
    return None

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
            # Les modèles servis ici sont des « thinking models » : sans ça, le
            # raisonnement part dans la réponse et consomme tout le budget de
            # sortie. Une requête qui passe explicitement chat_template_kwargs
            # (le bouton « Raisonnement » du playground) l'emporte toujours —
            # vérifié, ce réglage n'est qu'un défaut.
            "chat_template_kwargs": {"enable_thinking": False},
            # Le MÊME réglage, en double, pour l'endpoint Anthropic /v1/messages
            # (utilisé par Claude Code) : l'adaptateur Anthropic de LiteLLM
            # ignore le chat_template_kwargs de haut niveau et ne transmet que
            # extra_body. Sans cette ligne, un appel court revient VIDE —
            # content: [] et stop_reason: max_tokens, le raisonnement ayant
            # mangé tout le budget (mesuré : 41 tokens contre 3).
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
        existing = _litellm_model_id(public_name)
        if existing:
            requests.post(f"{LITELLM_URL}/model/delete", headers=litellm_headers(),
                          json={"id": existing}, timeout=5)
        r = requests.post(f"{LITELLM_URL}/model/new", headers=litellm_headers(),
                          json=body, timeout=8)
        return r.status_code < 300
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
    if not LITELLM_KEY:
        return
    mid = _litellm_model_id(name)
    if mid:
        try:
            requests.post(f"{LITELLM_URL}/model/delete", headers=litellm_headers(),
                          json={"id": mid}, timeout=5)
        except Exception:
            pass
