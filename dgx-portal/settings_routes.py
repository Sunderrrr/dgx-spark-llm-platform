"""Reglages utilisateur : serveurs MCP, skills, personnalisation, quotas.

Extrait de app.py le 28/08. Regroupe ce que l'utilisateur peut regler pour
lui-meme, par opposition aux reglages GLOBAUX de la plateforme, qui sont dans
admin_routes.py.

_account_limits et _rate_used vivent ici parce que la page de reglages est le
seul endroit qui les affiche : ce sont les plafonds vus par l'utilisateur
(budget de tokens, debit de chat), pas les garde-fous qui les appliquent — ceux-la
sont dans guards.py.
"""
import re
import sqlite3
import time
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from auth import login_required
from config import AVATAR_IDS, AVATAR_LABELS, KEY_BUDGET, LANGS, THEME_IDS
from conversation_routes import CONVERSATIONS_MAX
from db import get_db, get_setting
from guards import CHAT_RATE_MAX, CHAT_RATE_WINDOW, _chat_rate_limited
from litellm_client import _litellm_user_info
from mcp_client import MCPClient, MCPError
from mcp_client import invalidate_tools as _invalidate_mcp_tools
from mcp_client import validate_mcp_url
from stats import _account_activity
from vllm_health import effective_ctx, get_running_models

bp = Blueprint('settings', __name__)

# AI logos served from dgx-portal-frontend/public/avatars/<id>.svg.
# Strict allowlist: /settings/avatar refuses any id outside this set
# (the id lands in an <img> src, we don't want free input).
# AVATAR_IDS / THEME_IDS / LANGS / AVATAR_LABELS : cf. config.py


@bp.route('/api/settings')
@login_required
def api_settings():
    db = get_db()
    username = session['username']
    servers = [dict(r) for r in db.execute(
        "SELECT id, name, url, description, allowed_tools, enabled, "
        "(auth_header IS NOT NULL) AS has_auth, created_at "
        "FROM mcp_servers WHERE username=? ORDER BY created_at DESC", (username,))]
    skills = [dict(r) for r in db.execute(
        "SELECT id, name, description, instructions, created_at FROM skills WHERE username=? "
        "ORDER BY created_at DESC", (username,))]
    pref = db.execute("SELECT avatar_id, theme_id, lang FROM user_prefs WHERE username=?",
                       (username,)).fetchone()
    acct = _litellm_user_info(username)
    return jsonify({
        'mcp_servers': servers,
        'skills': skills,
        'avatar_id': pref['avatar_id'] if pref else None,
        'theme_id': (pref['theme_id'] if pref else None) or 'neutral',
        'lang': (pref['lang'] if pref else None) or 'en',
        'theme_ids': THEME_IDS,
        'langs': LANGS,
        'avatars': [{'id': a, 'label': AVATAR_LABELS.get(a, a)} for a in AVATAR_IDS],
        'account': {
            'username': username,
            'fullname': session.get('fullname', username),
            'is_admin': bool(session.get('is_admin')),
            'spend': acct.get('spend') or 0,
            'max_budget': acct.get('max_budget'),
            'unlimited': bool(session.get('is_admin')),
            'key_count': db.execute("SELECT COUNT(*) c FROM api_keys WHERE username=?",
                                     (username,)).fetchone()['c'],
            'mcp_count': len(servers),
            'skill_count': len(skills),
        },
        'activity': _account_activity(username),
        'limits': _account_limits(username, acct, servers, skills),
    })


def _rate_used(username, bucket):
    """Number of requests already consumed in the current window (0 if expired)."""
    row = get_db().execute("SELECT fails, first_at FROM login_attempts WHERE key=?",
                            (f"{bucket}|{username}",)).fetchone()
    if not row or time.time() - row['first_at'] > CHAT_RATE_WINDOW:
        return 0
    return row['fails']


def _account_limits(username, acct, servers, skills):
    """Real account quotas. Each entry describes a limit actually
    applied by the platform — nothing informative-decorative.
    """
    db = get_db()
    is_admin = bool(session.get('is_admin'))
    default_budget = float(get_setting('default_key_budget', KEY_BUDGET))
    max_budget = acct.get('max_budget') if acct.get('exists') else default_budget
    n_conv = db.execute("SELECT COUNT(*) c FROM conversations WHERE username=?",
                        (username,)).fetchone()['c']
    running = get_running_models()
    ctx = None
    if running:
        row = db.execute("SELECT vllm_args, engine FROM model_configs WHERE name=?",
                          (running[0],)).fetchone()
        if row:
            ctx = effective_ctx(row['vllm_args'], row['engine'] or 'vllm')
    return [
        {'key': 'budget', 'label': "Budget de tokens",
         'desc': "Quota quotidien partagé par toutes tes clés API.",
         'used': round(acct.get('spend') or 0), 'max': None if is_admin else round(max_budget or 0),
         'unit': 'tokens', 'unlimited': is_admin},
        {'key': 'rate-support', 'label': "Messages Support",
         'desc': f"Maximum {CHAT_RATE_MAX} messages par minute.",
         'used': _rate_used(username, 'rl-support'), 'max': CHAT_RATE_MAX,
         'unit': 'messages / min', 'unlimited': False},
        {'key': 'rate-playground', 'label': "Messages Playground",
         'desc': f"Maximum {CHAT_RATE_MAX} messages par minute.",
         'used': _rate_used(username, 'rl-playground'), 'max': CHAT_RATE_MAX,
         'unit': 'messages / min', 'unlimited': False},
        {'key': 'conversations', 'label': "Conversations enregistrées",
         'desc': "Au-delà, les plus anciennes sont supprimées automatiquement.",
         'used': n_conv, 'max': CONVERSATIONS_MAX, 'unit': 'conversations', 'unlimited': False},
        {'key': 'mcp', 'label': "Serveurs MCP connectés",
         'desc': "Serveurs distants dont l'assistant peut utiliser les outils.",
         'used': len(servers), 'max': None, 'unit': 'serveurs', 'unlimited': True},
        {'key': 'skills', 'label': "Compétences définies",
         'desc': "Instructions réutilisables chargées à la demande.",
         'used': len(skills), 'max': None, 'unit': 'compétences', 'unlimited': True},
        {'key': 'context', 'label': "Fenêtre de contexte du modèle",
         'desc': running[0] if running else "Aucun modèle actif.",
         'used': None, 'max': ctx, 'unit': 'tokens', 'unlimited': False},
    ]


# Per-account caps. Each active MCP server costs, on every chat
# message, an outbound network round-trip that blocks a gunicorn thread (we have
# 16 per worker) for the duration of its timeout. Without a cap, a user can
# register hundreds of them and make Support unusable for everyone
# else. Skills only cost a SQLite read, wider cap.
MAX_MCP_SERVERS = 10
MAX_SKILLS = 50


@bp.route('/mcp', methods=['POST'])
@login_required
def mcp_servers_route():
    username = session['username']
    db = get_db()
    action = request.form.get('action')
    if action in ('create', 'update'):
        # create/update make a live outbound connection (initialize +
        # tools/list) to a user-supplied URL: without a rate
        # limit, the route becomes a port scanner/amplifier driven
        # from the outside.
        wait = _chat_rate_limited(username, 'rl-mcp')
        if wait:
            return jsonify({'ok': False,
                            'error': f"Trop de tentatives, réessaie dans {wait} s."}), 429
    if action == 'create':
        count = db.execute("SELECT COUNT(*) c FROM mcp_servers WHERE username=?",
                           (username,)).fetchone()['c']
        if count >= MAX_MCP_SERVERS:
            return jsonify({'ok': False,
                            'error': f"Maximum {MAX_MCP_SERVERS} serveurs MCP par compte."})
        name = request.form.get('name', '').strip()[:60]
        url = request.form.get('url', '').strip()
        auth_header = request.form.get('auth_header', '').strip() or None
        description = request.form.get('description', '').strip()[:300]
        allowed_tools = request.form.get('allowed_tools', '').strip()[:500]
        if not name or not url:
            return jsonify({'ok': False, 'error': "Nom et URL requis."})
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', name):
            return jsonify({'ok': False, 'error': "Lettres, chiffres, underscores et tirets uniquement."})
        ok, err = validate_mcp_url(url)
        if not ok:
            return jsonify({'ok': False, 'error': err})
        try:
            client = MCPClient(url, auth_header)
            client.initialize()
            discovered = client.list_tools()
        except MCPError as e:
            return jsonify({'ok': False, 'error': f"Connexion au serveur MCP impossible : {e}"})
        except Exception:
            return jsonify({'ok': False, 'error': "Connexion au serveur MCP impossible."})
        try:
            db.execute(
                "INSERT INTO mcp_servers (username, name, url, auth_header, description, "
                "allowed_tools, enabled, created_at) VALUES (?,?,?,?,?,?,1,?)",
                (username, name, url, auth_header, description, allowed_tools,
                 datetime.now().isoformat()))
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify({'ok': False, 'error': "Tu as déjà un serveur MCP avec ce nom."})
        return jsonify({'ok': True, 'tool_count': len(discovered)})
    elif action == 'update':
        server_id = request.form.get('id', '')
        row = db.execute("SELECT auth_header FROM mcp_servers WHERE id=? AND username=?",
                          (server_id, username)).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': "Serveur introuvable."})
        name = request.form.get('name', '').strip()[:60]
        url = request.form.get('url', '').strip()
        description = request.form.get('description', '').strip()[:300]
        allowed_tools = request.form.get('allowed_tools', '').strip()[:500]
        # Authorization field left empty on redisplay = "do not change"
        # (we never send the secret back to the client, so we can't
        # distinguish it from a deliberate deletion; clearing it is done via the
        # explicit marker below).
        raw_auth = request.form.get('auth_header', '')
        auth_header = row['auth_header'] if raw_auth == '' else (raw_auth.strip() or None)
        if raw_auth.strip() == '-':
            auth_header = None
        if not name or not url:
            return jsonify({'ok': False, 'error': "Nom et URL requis."})
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', name):
            return jsonify({'ok': False, 'error': "Lettres, chiffres, underscores et tirets uniquement."})
        ok, err = validate_mcp_url(url)
        if not ok:
            return jsonify({'ok': False, 'error': err})
        try:
            client = MCPClient(url, auth_header)
            client.initialize()
            discovered = client.list_tools()
        except MCPError as e:
            return jsonify({'ok': False, 'error': f"Connexion au serveur MCP impossible : {e}"})
        except Exception:
            return jsonify({'ok': False, 'error': "Connexion au serveur MCP impossible."})
        try:
            db.execute("UPDATE mcp_servers SET name=?, url=?, auth_header=?, description=?, "
                       "allowed_tools=? WHERE id=? AND username=?",
                       (name, url, auth_header, description, allowed_tools, server_id, username))
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify({'ok': False, 'error': "Tu as déjà un serveur MCP avec ce nom."})
        _invalidate_mcp_tools(int(server_id))
        return jsonify({'ok': True, 'tool_count': len(discovered)})
    elif action == 'toggle':
        server_id = request.form.get('id', '')
        enabled = 1 if request.form.get('enabled') == '1' else 0
        db.execute("UPDATE mcp_servers SET enabled=? WHERE id=? AND username=?",
                   (enabled, server_id, username))
        db.commit()
        return jsonify({'ok': True})
    elif action == 'delete':
        server_id = request.form.get('id', '')
        db.execute("DELETE FROM mcp_servers WHERE id=? AND username=?", (server_id, username))
        db.commit()
        _invalidate_mcp_tools(int(server_id) if str(server_id).isdigit() else server_id)
    return ('', 204)


@bp.route('/skills', methods=['POST'])
@login_required
def skills_route():
    username = session['username']
    db = get_db()
    action = request.form.get('action')
    if action == 'create':
        count = db.execute("SELECT COUNT(*) c FROM skills WHERE username=?",
                           (username,)).fetchone()['c']
        if count >= MAX_SKILLS:
            return jsonify({'ok': False,
                            'error': f"Maximum {MAX_SKILLS} compétences par compte."})
        name = request.form.get('name', '').strip()[:60]
        description = request.form.get('description', '').strip()[:300]
        instructions = request.form.get('instructions', '').strip()[:20000]
        if not name or not description or not instructions:
            return jsonify({'ok': False, 'error': "Nom, description et instructions requis."})
        try:
            db.execute(
                "INSERT INTO skills (username, name, description, instructions, created_at) "
                "VALUES (?,?,?,?,?)",
                (username, name, description, instructions, datetime.now().isoformat()))
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify({'ok': False, 'error': "Tu as déjà une compétence avec ce nom."})
        return jsonify({'ok': True})
    elif action == 'update':
        skill_id = request.form.get('id', '')
        name = request.form.get('name', '').strip()[:60]
        description = request.form.get('description', '').strip()[:300]
        instructions = request.form.get('instructions', '').strip()[:20000]
        if not name or not description or not instructions:
            return jsonify({'ok': False, 'error': "Nom, description et instructions requis."})
        try:
            cur = db.execute(
                "UPDATE skills SET name=?, description=?, instructions=? WHERE id=? AND username=?",
                (name, description, instructions, skill_id, username))
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify({'ok': False, 'error': "Tu as déjà une compétence avec ce nom."})
        if not cur.rowcount:
            return jsonify({'ok': False, 'error': "Compétence introuvable."})
        return jsonify({'ok': True})
    elif action == 'delete':
        skill_id = request.form.get('id', '')
        db.execute("DELETE FROM skills WHERE id=? AND username=?", (skill_id, username))
        db.commit()
    return ('', 204)
