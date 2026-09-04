"""Assistant Support : outils exposes au modele et execution securisee.

Extrait de app.py le 28/08. Aucune route ici — la route /support/chat reste
dans app.py avec le chat du playground, les deux partageant la mecanique de
flux SSE. Ce module fournit le CATALOGUE d'outils, leur execution, et le
contexte injecte au modele.

Rappel de la posture de securite, portee par _exec_support_tool : le resultat
d'un outil MCP ou d'une skill est du texte ecrit par un tiers, reinjecte dans le
contexte du modele — c'est un vecteur d'injection direct. Des qu'un tel contenu
est entre, les outils privilegies (revocation de cle, lancement/arret de modele)
sont refuses pour le reste du tour.
"""
import json
import re
import time
from datetime import datetime

from announcements import _announce_launch
from db import get_db
from litellm_client import (_litellm_user_info, create_litellm_key,
                            get_user_keys, revoke_litellm_key)
from mcp_client import MCPClient, MCPError, list_tools_cached
from notify import (notify_budget_discord, notify_budget_email, notify_discord,
                    notify_email)
from sidecars import runner_launch, runner_logs, runner_status, runner_stop
from stats import user_hourly
from vllm_health import effective_ctx, get_running_models

SUPPORT_FAQ = (
    "FAQ plateforme Cronos :\n"
    "- Plateforme IA interne et GRATUITE (pas de facturation, pas de plan payant).\n"
    "- API compatible OpenAI. Endpoint public : configuré dans « Mes clés API ».\n"
    "- Budget PAR COMPTE, partagé par toutes les clés d'un même utilisateur, "
    "réinitialisé chaque jour. Le quota compte les vrais tokens : 1 token de prompt = 1, 1 token généré = 1.\n"
    "- Obtenir plus de budget : demande envoyée à un admin (bouton « Demander plus de "
    "tokens » ou via toi, Cronos). Un admin valide.\n"
    "- Demander un nouveau modèle : via la page « Demander un modèle » (identifiant "
    "Hugging Face) ou via toi ; un admin le valide puis le lance.\n"
    "- Intégrations : OpenCode, Hermes Agent, Codex, Aider, Cursor, Continue, "
    "Python/cURL — snippets prêts sur « Mes clés API ».\n"
    "- Un seul modèle tourne à la fois sur le GPU (mémoire unifiée du DGX Spark)."
)

SUPPORT_SYSTEM = (
    "Tu es Cronos, l'assistant IA de la plateforme Cronos (NVIDIA DGX Spark, "
    "auto-hébergée). Tu aides les utilisateurs en français, de façon concise et "
    "concrète, sur les clés API, le budget/quota, les intégrations, l'accès aux "
    "modèles et le dépannage.\n"
    "Tu peux AGIR pour l'utilisateur via des outils (tools) — toujours au nom du "
    "compte connecté, jamais pour quelqu'un d'autre :\n"
    "- create_api_key : créer une clé API.\n"
    "- revoke_api_key : supprimer une de ses clés (DESTRUCTIF).\n"
    "- request_budget : déposer une demande d'augmentation de budget.\n"
    "- request_model : demander l'ajout d'un modèle (identifiant Hugging Face).\n"
    "- launch_model / stop_model : (admin uniquement) piloter le modèle du GPU.\n"
    "Règles d'usage des outils :\n"
    "- N'appelle un outil QUE pour une action explicitement demandée (créer/"
    "révoquer une clé, demander du budget/un modèle, lancer/arrêter). Pour toute "
    "question de dépannage, d'information ou d'explication, réponds DIRECTEMENT en "
    "texte, SANS appeler d'outil (tu as déjà les logs et l'état dans le contexte).\n"
    "- Confirme TOUJOURS avec l'utilisateur avant une action destructive ou "
    "impactante (revoke_api_key, stop_model, launch_model qui coupe le modèle "
    "actif) : demande « tu confirmes ? » et n'appelle l'outil qu'après un oui.\n"
    "- create_api_key et request_* peuvent être faits directement si la demande est "
    "claire.\n"
    "- Quand tu crées une clé, AFFICHE la clé complète une seule fois à l'utilisateur "
    "(c'est sa nouvelle clé) et rappelle-lui de la copier.\n"
    "Règles générales :\n"
    "- Appuie-toi sur le CONTEXTE et la FAQ fournis. N'invente rien (ni plan payant, "
    "ni page de facturation, ni fonctionnalité inexistante).\n"
    "- Les clés du CONTEXTE sont MASQUÉES : ne tente jamais d'en reconstituer une.\n"
    "- IMPORTANT : réponds DIRECTEMENT, en français, sans montrer ton raisonnement "
    "ni de préambule interne. Va droit au but."
)

_THINK_RE = re.compile(r'<think>.*?</think>|<reasoning>.*?</reasoning>', re.S | re.I)


def _clean_reply(text):
    """Strips any reasoning blocks left in the response."""
    text = _THINK_RE.sub('', text or '')
    # Some models emit a plaintext CoT then the final answer: if we
    # detect a final-answer marker, we keep what follows.
    for marker in ('### Réponse', 'Réponse finale :', 'Final answer:', 'Voici ma réponse'):
        idx = text.rfind(marker)
        if idx != -1:
            text = text[idx + len(marker):]
    return text.strip().lstrip(':').strip()


def _mask_key(k):
    return (k[:6] + '…' + k[-4:]) if k and len(k) > 12 else '—'


_LOG_HINT_RE = re.compile(
    r'log|erreur|error|marche pas|répond|repond|crash|plante|lent|500|502|503|bug|'
    r'démarr|demarr|charge|timeout|down|hs|ko', re.I)

def _support_context(username, is_admin, user_msg=''):
    """Context injected into the bot, STRICTLY limited to the logged-in user.
    The (large) server logs are included only if the question is about a technical
    issue → a much lighter prompt for everyday questions.
    """
    db = get_db()
    lines = [f"Utilisateur connecté : {username}" + (" (admin)" if is_admin else "")]

    # ── Account budget + keys ────
    acct = _litellm_user_info(username)
    if is_admin:
        lines.append("Budget du compte : illimité (admin).")
    elif acct['exists'] and acct['max_budget'] is not None:
        s, b = acct['spend'] or 0, acct['max_budget']
        lines.append(("Budget du compte : {:,.0f} / {:,.0f} tokens utilisés"
                      .format(s, b)).replace(',', ' ')
                     + (f" (reset {acct['budget_reset_at'][:10]})" if acct['budget_reset_at'] else ""))
    keys = get_user_keys(username)
    if keys:
        lines.append("Clés de l'utilisateur (masquées, alias = identifiant pour les actions) :")
        for k in keys:
            lines.append("  - {} : {}, dépensé {:,.0f}".format(
                k.get('key_alias', '—'), _mask_key(k.get('key', '')),
                k.get('spend', 0) or 0).replace(',', ' '))
    else:
        lines.append("L'utilisateur n'a aucune clé API pour l'instant.")

    # ── Today's consumption ──
    try:
        u = user_hourly(username)
        if u and u.get('has_data'):
            lines.append("Conso aujourd'hui : {:,.0f} tokens réels (pic vers {}h)."
                         .format(u['total'], u['peak_hour']).replace(',', ' '))
    except Exception:
        pass

    # ── Catalog of launchable models ─────
    running = set(get_running_models())
    cat = []
    for row in db.execute("SELECT name, vllm_args, engine FROM model_configs ORDER BY name"):
        eng = row['engine'] or 'vllm'
        ctx = effective_ctx(row['vllm_args'], eng)
        args = row['vllm_args'] or ''
        # vLLM requires an explicit parser (--tool-call-parser / --enable-auto-tool-choice);
        # llama.cpp and ds4 do tool-calling NATIVELY via the model's chat
        # template (verified live on Ling — no need for --jinja on recent builds).
        has_tools = (eng in ('llamacpp', 'ds4')
                     or '--tool-call-parser' in args or '--enable-auto-tool-choice' in args
                     or '--jinja' in args)
        flag = " [ACTIVE]" if row['name'] in running else ""
        cat.append("  - {}{} : contexte {}, tool-calling {}".format(
            row['name'], flag,
            f"{ctx:,}".replace(',', ' ') if ctx else "?",
            "oui" if has_tools else "non"))
    if cat:
        lines.append("Catalogue des modèles (le [ACTIVE] est celui chargé sur le GPU) :\n"
                     + "\n".join(cat))
    st = runner_status()
    lines.append("Runner vLLM : " + st.get('status', '?')
                 + (" — aucun modèle chargé" if not running else ""))

    # ── User's pending requests ─────────────
    mreqs = db.execute("SELECT model_id, status FROM model_requests WHERE username=? "
                       "ORDER BY created_at DESC LIMIT 5", (username,)).fetchall()
    if mreqs:
        lines.append("Demandes de modèle de l'utilisateur : "
                     + ", ".join(f"{r['model_id']} ({r['status']})" for r in mreqs))
    breqs = db.execute("SELECT status FROM budget_requests WHERE username=? "
                       "ORDER BY created_at DESC LIMIT 3", (username,)).fetchall()
    if breqs:
        lines.append("Demandes de budget de l'utilisateur : "
                     + ", ".join(r['status'] for r in breqs))

    # ── Server logs (troubleshooting, ADMINS ONLY) ───
    # The is_admin guard is not cosmetic: the two other accesses to these
    # logs (/admin/runner/logs and /admin/runner/stream) are @admin_required.
    # Without it, any user writing "it's slow" or "error"
    # would get the runner's log tail injected into the system prompt, then
    # ask the assistant to copy it back — engine command line,
    # host paths, startup traces, and other users' prompts
    # as soon as request logging is enabled.
    if is_admin and _LOG_HINT_RE.search(user_msg or ''):
        logs = runner_logs(n=20)
        if logs:
            tail = [l[:200] for l in logs[-12:]]
            lines.append("Derniers logs du serveur de modèle :\n" + "\n".join(tail))

    return SUPPORT_FAQ + "\n\n" + "\n".join(lines)


def _support_tools(is_admin):
    """Schemas of the self-service tools exposed to the model (function-calling format)."""
    t = [
        {"type": "function", "function": {
            "name": "create_api_key",
            "description": "Crée une nouvelle clé API pour l'utilisateur connecté et la retourne.",
            "parameters": {"type": "object", "properties": {
                "alias": {"type": "string", "description": "Nom court de la clé (ex: mon-laptop). Optionnel."}}}}},
        {"type": "function", "function": {
            "name": "revoke_api_key",
            "description": "Révoque (supprime) une clé de l'utilisateur, par son alias. Destructif : confirmer avant.",
            "parameters": {"type": "object", "properties": {
                "alias": {"type": "string", "description": "Alias exact de la clé à révoquer."}},
                "required": ["alias"]}}},
        {"type": "function", "function": {
            "name": "request_budget",
            "description": "Dépose une demande d'augmentation de budget pour l'utilisateur (envoyée à un admin).",
            "parameters": {"type": "object", "properties": {
                "reason": {"type": "string", "description": "Raison (optionnel)."}}}}},
        {"type": "function", "function": {
            "name": "request_model",
            "description": "Demande l'ajout d'un modèle par son identifiant Hugging Face (envoyée à un admin).",
            "parameters": {"type": "object", "properties": {
                "hf_model_id": {"type": "string", "description": "Ex: Qwen/Qwen3-Coder-30B-A3B-Instruct."},
                "reason": {"type": "string", "description": "Pourquoi ce modèle (optionnel)."}},
                "required": ["hf_model_id"]}}},
    ]
    if is_admin:
        t += [
            {"type": "function", "function": {
                "name": "launch_model",
                "description": "(Admin) Lance un modèle du catalogue par son nom. Remplace le modèle actif — confirmer avant.",
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Nom du modèle dans le catalogue."}},
                    "required": ["name"]}}},
            {"type": "function", "function": {
                "name": "stop_model",
                "description": "(Admin) Arrête le modèle actuellement chargé. Confirmer avant.",
                "parameters": {"type": "object", "properties": {}}}},
        ]
    return t


def _exec_support_tool(name, args, username, fullname, is_admin):
    """Runs a self-service action, ALWAYS on behalf of the session user
    (the model never chooses "for whom"). Returns (result_text, ok).
    """
    db = get_db()
    try:
        if name == 'create_api_key':
            raw = (args.get('alias') or '').strip()
            alias = re.sub(r'[^a-zA-Z0-9_-]', '-', raw)[:40] if raw else f"{username}-{int(time.time())}"
            newkey = create_litellm_key(alias, username, is_admin=is_admin)
            if not newkey:
                return "Échec de la création (alias déjà pris ou LiteLLM injoignable).", False
            db.execute("INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) "
                       "VALUES (?,?,?,?)", (username, alias, newkey, datetime.now().isoformat()))
            db.commit()
            return f"Clé créée (alias={alias}). CLÉ COMPLÈTE à montrer une fois : {newkey}", True

        if name == 'revoke_api_key':
            alias = (args.get('alias') or '').strip()
            row = db.execute("SELECT key_value FROM api_keys WHERE username=? AND key_alias=?",
                             (username, alias)).fetchone()
            if not row:
                return f"Aucune clé « {alias} » pour cet utilisateur.", False
            if revoke_litellm_key(row['key_value']):
                db.execute("DELETE FROM api_keys WHERE username=? AND key_alias=?", (username, alias))
                db.commit()
                return f"Clé « {alias} » révoquée.", True
            return "Échec de la révocation côté LiteLLM.", False

        if name == 'request_budget':
            reason = (args.get('reason') or '').strip()
            if db.execute("SELECT 1 FROM budget_requests WHERE username=? AND status='pending'",
                          (username,)).fetchone():
                return "Une demande de budget est déjà en attente.", True
            current = _litellm_user_info(username).get('max_budget')
            db.execute("INSERT INTO budget_requests (username, fullname, key_alias, current_budget, "
                       "reason, status, created_at) VALUES (?,?,?,?,?,?,?)",
                       (username, fullname, '(compte)', current, reason, 'pending',
                        datetime.now().isoformat()))
            db.commit()
            notify_budget_discord(username, fullname, '(compte)', current, reason)
            notify_budget_email(username, fullname, '(compte)', current, reason)
            return "Demande de budget envoyée à un admin.", True

        if name == 'request_model':
            hf = (args.get('hf_model_id') or '').strip()
            if not hf:
                return "Identifiant de modèle manquant.", False
            reason = (args.get('reason') or '').strip()
            if db.execute("SELECT 1 FROM model_requests WHERE username=? AND model_id=? AND status='pending'",
                          (username, hf)).fetchone():
                return f"Une demande pour « {hf} » est déjà en attente.", True
            db.execute("INSERT INTO model_requests (username, fullname, model_id, reason, status, created_at) "
                       "VALUES (?,?,?,?,?,?)",
                       (username, fullname, hf, reason, 'pending', datetime.now().isoformat()))
            db.commit()
            notify_discord(hf, username, fullname, reason)
            notify_email(hf, username, fullname, reason)
            return f"Demande d'ajout du modèle « {hf} » envoyée à un admin.", True

        if name == 'launch_model':
            if not is_admin:
                return "Action réservée aux admins.", False
            mname = (args.get('name') or '').strip()
            cfg = db.execute("SELECT hf_model_id, name, vllm_args, engine FROM model_configs WHERE name=?",
                             (mname,)).fetchone()
            if not cfg:
                return f"Modèle « {mname} » introuvable dans le catalogue.", False
            ok, _motif = runner_launch(cfg['hf_model_id'], cfg['name'], cfg['vllm_args'] or '',
                               cfg['engine'] or 'vllm')
            if ok:
                _announce_launch(cfg['name'])
            return (f"Lancement de « {mname} » demandé (démarrage en cours)." if ok
                    else "Runner injoignable."), ok

        if name == 'stop_model':
            if not is_admin:
                return "Action réservée aux admins.", False
            ok = runner_stop()
            return ("Modèle arrêté." if ok else "Runner injoignable."), ok

        return f"Outil inconnu : {name}", False
    except Exception as e:
        return f"Erreur lors de l'exécution de l'action ({type(e).__name__}).", False


# Tools we refuse to run once external content (an MCP result
# or skill text) has entered the context: destructive
# (key revocation) or global server-scope (the GPU is shared).
GUARDED_TOOLS = {'revoke_api_key', 'launch_model', 'stop_model'}


def _support_tool_target(name, args):
    """Short label for a tool call's target, for the ChatToolCalls display."""
    if name in ('create_api_key', 'revoke_api_key'):
        return (args.get('alias') or '').strip() or None
    if name == 'request_model':
        return (args.get('hf_model_id') or '').strip() or None
    if name == 'launch_model':
        return (args.get('name') or '').strip() or None
    return None


TOOL_LABELS = {
    'create_api_key': "Créer une clé API",
    'revoke_api_key': "Révoquer une clé API",
    'request_budget': "Demander du budget",
    'request_model': "Demander un modèle",
    'launch_model': "Lancer un modèle",
    'stop_model': "Arrêter le modèle",
}


def _mcp_tool_name(server_id, original_name):
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', original_name)[:60]
    return f"mcp_{server_id}_{safe}"


def _user_extra_tools(username):
    """A user's dynamic tools: their MCP servers (tools discovered
    live, with a short cache) + a use_skill tool if they have skills.
    Returns (tool_schemas, routing_table) where the routing table maps
    the prefixed tool name to how to run and display it.
    """
    db = get_db()
    tools = []
    routing = {}
    for row in db.execute("SELECT id, name, url, auth_header, allowed_tools FROM mcp_servers "
                          "WHERE username=? AND enabled=1", (username,)):
        try:
            discovered = list_tools_cached(row['id'], row['url'], row['auth_header'])
        except Exception:
            discovered = []
        # Optional filter: user-entered tool allowlist
        # (empty = all the server's tools are exposed to the model).
        allowed = {t.strip() for t in (row['allowed_tools'] or '').split(',') if t.strip()}
        if allowed:
            discovered = [t for t in discovered if t.get('name') in allowed]
        for t in discovered:
            prefixed = _mcp_tool_name(row['id'], t.get('name', ''))
            tools.append({"type": "function", "function": {
                "name": prefixed,
                "description": f"[Serveur MCP « {row['name']} »] {t.get('description', '') or t.get('name', '')}",
                "parameters": t.get('inputSchema') or {"type": "object", "properties": {}},
            }})
            routing[prefixed] = {
                'kind': 'mcp', 'server_id': row['id'], 'server_name': row['name'],
                'tool_name': t.get('name', ''),
            }
    skill_rows = list(db.execute("SELECT name, description FROM skills WHERE username=?", (username,)))
    if skill_rows:
        tools.append({"type": "function", "function": {
            "name": "use_skill",
            "description": "Charge les instructions détaillées d'une compétence (skill) définie "
                           "par l'utilisateur pour t'aider sur sa tâche.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "enum": [r['name'] for r in skill_rows],
                          "description": "Nom exact de la compétence à charger."}},
                "required": ["name"]}}})
        routing['use_skill'] = {'kind': 'skill'}
    return tools, routing


def _exec_mcp_tool(server_id, tool_name, args, username):
    """Runs a tool from an MCP server registered by the user (never
    another's — the row is always scoped to username).
    """
    db = get_db()
    row = db.execute("SELECT url, auth_header FROM mcp_servers "
                      "WHERE id=? AND username=? AND enabled=1",
                      (server_id, username)).fetchone()
    if not row:
        return "Serveur MCP introuvable.", False
    try:
        client = MCPClient(row['url'], row['auth_header'])
        client.initialize()
        return client.call_tool(tool_name, args)
    except MCPError as e:
        return str(e), False
    except Exception as e:
        return f"Erreur MCP ({type(e).__name__}).", False


def _exec_skill(name, username):
    db = get_db()
    row = db.execute("SELECT instructions FROM skills WHERE username=? AND name=?",
                      (username, name)).fetchone()
    if not row:
        return f"Compétence « {name} » introuvable.", False
    return row['instructions'], True



def _sse_tool_event(tc_id, name, target, status, duration_ms=None, error=None):
    """SSE event for a tool invocation on the Support side (rendered by the
    frontend via the Astryx ChatToolCalls component), distinct from the text
    deltas of _sse_chunks.
    """
    payload = {'tool_call': {'id': tc_id, 'name': name, 'status': status}}
    if target:
        payload['tool_call']['target'] = target
    if duration_ms is not None:
        payload['tool_call']['duration_ms'] = duration_ms
    if error:
        payload['tool_call']['error'] = error
    return f"data: {json.dumps(payload)}\n\n"
