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
from werkzeug.security import check_password_hash, generate_password_hash

from auth import completer_origine_session, login_required
from config import AVATAR_IDS, AVATAR_LABELS, KEY_BUDGET, KEY_DURATION, LANGS, THEME_IDS
from conversation_routes import CONVERSATIONS_MAX
from db import get_db, get_setting, log_audit
from guards import CHAT_RATE_MAX, CHAT_RATE_WINDOW, _chat_rate_limited
from litellm_client import _litellm_user_info
from local_users import (GESTION_LDAP, GESTION_PORTAIL, gestion_mot_de_passe,
                         password_policy_error)
from user_lifecycle import compter_donnees, deprovisionner_compte, prevenir_mot_de_passe_change
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
            # `spend` est la consommation de la PÉRIODE d'enveloppe (LiteLLM
            # remet le compteur à zéro à `budget_reset_at`), pas celle du jour :
            # l'onglet « Mon compte » l'appelait « Consommé aujourd'hui » et
            # laissait donc croire à un compteur quotidien. On transmet la date
            # de remise à zéro et la durée pour que l'interface puisse le dire.
            'budget_reset_at': acct.get('budget_reset_at') or None,
            'budget_duration': get_setting('default_key_duration', KEY_DURATION),
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


# ── Compte : sessions ouvertes et mot de passe ──────────────────────────────
# Ajouté le 2026-09-13. Le portail savait révoquer les sessions d'un compte
# (côté admin) mais l'utilisateur ne pouvait ni les VOIR, ni couper celle d'un
# appareil perdu ; et un compte local ne pouvait pas changer son mot de passe
# sans passer par un administrateur — alors que le réflexe, après un doute, est
# de le changer soi-même tout de suite.

def _mes_sessions(username):
    """Sessions actives du compte, la plus récente d'abord.

    Le sid complet ne sort JAMAIS : c'est le secret du cookie de session, et
    le renvoyer au navigateur le remettrait dans une réponse JSON, donc dans
    les journaux de quiconque écoute. On n'expose que 12 caractères, largement
    assez pour désigner une session de façon unique.
    """
    now = time.time()
    sid_courant = session.get('sid')
    # Une session ouverte avant l'ajout des colonnes n'a ni IP ni user-agent :
    # on la complète ici, tant que c'est bien la sienne.
    completer_origine_session()
    out = []
    for r in get_db().execute(
            "SELECT sid, created_at, expires_at, ip, user_agent FROM user_sessions "
            "WHERE username=? AND revoked=0 AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT 50", (username, now)).fetchall():
        out.append({
            'id': (r['sid'] or '')[:12],
            'created_at': r['created_at'],
            'expires_at': r['expires_at'],
            'ip': r['ip'],
            'user_agent': r['user_agent'],
            'current': bool(sid_courant and r['sid'] == sid_courant),
        })
    return out


@bp.route('/api/account/sessions')
@login_required
def api_account_sessions():
    username = session['username']
    gestion = gestion_mot_de_passe(username)
    return jsonify({'sessions': _mes_sessions(username),
                    'local_account': gestion['local'],
                    'password_managed_by': gestion['gestion'],
                    'passkey_possible': gestion['passkey']})


@bp.route('/api/account/sessions/revoke', methods=['POST'])
@login_required
def api_account_sessions_revoke():
    """Révoque une de SES sessions (`id`), ou toutes les autres (`all`).

    Le périmètre est le compte appelant, vérifié en SQL : un identifiant de
    session appartenant à quelqu'un d'autre ne peut pas être visé, même en le
    devinant.
    """
    username = session['username']
    corps = request.get_json(silent=True) or request.form
    db = get_db()
    if corps.get('all') in (True, '1', 'true', 'on'):
        n = db.execute(
            "UPDATE user_sessions SET revoked=1 WHERE username=? AND sid<>? AND revoked=0",
            (username, session.get('sid'))).rowcount
        db.commit()
        log_audit(username, 'account.sessions.revoke',
                  f"{n} autre(s) session(s) fermée(s) par l'utilisateur")
        return jsonify({'ok': True, 'revoked': n})

    ident = (corps.get('id') or '').strip()
    if len(ident) < 8:
        return jsonify({'ok': False, 'error': "Identifiant de session invalide."}), 400
    lignes = db.execute(
        "SELECT sid FROM user_sessions WHERE username=? AND revoked=0 AND sid LIKE ?",
        (username, ident + '%')).fetchall()
    if not lignes:
        return jsonify({'ok': False, 'error': "Session introuvable (déjà fermée ?)."}), 404
    if len(lignes) > 1:
        # 12 caractères de token_urlsafe(32) : une collision ici signifie un
        # identifiant tronqué, on refuse plutôt que de fermer au hasard.
        return jsonify({'ok': False,
                        'error': "Identifiant de session ambigu, réessaie."}), 409
    cible = lignes[0]['sid']
    db.execute("UPDATE user_sessions SET revoked=1 WHERE sid=?", (cible,))
    db.commit()
    courant = bool(session.get('sid') == cible)
    log_audit(username, 'account.sessions.revoke',
              "fermeture de la session courante" if courant else f"fermeture de la session {ident}")
    return jsonify({'ok': True, 'revoked': 1, 'current': courant})


@bp.route('/api/account/password', methods=['POST'])
@login_required
def api_account_password():
    """Changement de mot de passe en autonomie (comptes locaux seulement).

    Un compte LDAP/SSO n'a pas de mot de passe ici : il vit dans l'annuaire,
    et le portail ne doit pas laisser croire le contraire.
    """
    username = session['username']
    corps = request.get_json(silent=True) or request.form
    actuel = corps.get('current') or ''
    nouveau = corps.get('new') or ''
    db = get_db()
    row = db.execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    if row is None:
        return jsonify({'ok': False, 'error': "Compte géré par l'annuaire (LDAP/SSO) : "
                                              "le mot de passe se change là-bas."}), 400
    if not check_password_hash(row['password_hash'], actuel):
        # Journalisé : une rafale de ces échecs sur un compte connecté est le
        # signe d'une session volée, pas d'une faute de frappe.
        log_audit(username, 'account.password.echec', 'mot de passe actuel incorrect')
        return jsonify({'ok': False, 'error': "Mot de passe actuel incorrect."}), 400
    if check_password_hash(row['password_hash'], nouveau):
        return jsonify({'ok': False,
                        'error': "Le nouveau mot de passe est identique à l'actuel."}), 400
    err = password_policy_error(nouveau, username)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    db.execute("UPDATE local_users SET password_hash=? WHERE username=?",
               (generate_password_hash(nouveau), username))
    db.commit()
    # Un changement de mot de passe ferme les AUTRES sessions : c'est
    # précisément ce qu'on attend quand on le change par précaution. La
    # session courante survit — se faire déconnecter par sa propre action est
    # le genre de détail qui pousse à ne plus jamais changer de mot de passe.
    n = db.execute(
        "UPDATE user_sessions SET revoked=1 WHERE username=? AND sid<>? AND revoked=0",
        (username, session.get('sid'))).rowcount
    db.commit()
    log_audit(username, 'account.password', f'mot de passe changé, {n} session(s) fermée(s)')
    prevenir_mot_de_passe_change(username)
    return jsonify({'ok': True})


@bp.route('/api/account/delete', methods=['POST'])
@login_required
def api_account_delete():
    """Suppression de SON compte, décidée par l'intéressé.

    Deux cas, et les confondre serait le mensonge le plus coûteux de cet écran :

    - compte LOCAL : le portail détient la ligne `local_users`, il peut donc
      tout retirer — clés révoquées chez LiteLLM, enveloppe supprimée, sessions
      coupées, données purgées. On passe par `deprovisionner_compte`, le MÊME
      chemin que la suppression par un admin : une seconde implémentation
      finirait par diverger (c'est exactement ce qui avait laissé des clés
      vivantes après une suppression).
    - compte d'ANNUAIRE (LDAP/SSO) : le portail ne PEUT PAS supprimer le
      compte, qui vit dans l'annuaire ou chez le fournisseur d'identité. Il
      efface ce qu'il détient (données, préférences, clés) et le DIT : l'accès
      se retire côté annuaire, par un administrateur (blocage).

    Preuves exigées : `confirm=DELETE` (irréversible) et, pour un compte local,
    le mot de passe — vérifié par `_verify_password_locked`, donc soumis au même
    verrou 6 essais / 15 min que la page de connexion, sans quoi une session
    détournée pourrait tester des mots de passe à la vitesse du réseau. Un
    compte d'annuaire n'a aucun mot de passe que le portail puisse vérifier : la
    session en cours est la seule preuve disponible, et l'interface le dit.
    """
    from webauthn_routes import _verify_password_locked   # import tardif : évite un cycle
    username = session['username']
    corps = request.get_json(silent=True) or request.form
    if (corps.get('confirm') or '').strip().upper() != 'DELETE':
        return jsonify({'ok': False, 'needs_confirm': True,
                        'donnees': compter_donnees(username),
                        'error': "Confirmation requise : cette suppression est définitive."}), 409

    db = get_db()
    gestion = gestion_mot_de_passe(username)
    local = gestion['local']
    if local:
        # Même garde que côté admin, mais ici l'auto-suppression est le but :
        # elle n'est refusée que si elle emporterait le DERNIER admin local.
        from admin_routes import _dernier_admin_local
        if session.get('is_admin') and _dernier_admin_local(username):
            return jsonify({'ok': False,
                            'error': "Tu es le dernier administrateur local : nomme un "
                                     "autre administrateur avant de supprimer ton compte."}), 409

    # Vérification du mot de passe : on l'exige dès que le portail SAIT le
    # vérifier, c'est-à-dire pour un compte du portail ET pour un compte LDAP —
    # `_verify_password_locked` interroge l'annuaire. L'ancien test (`if local:`)
    # ne couvrait que les comptes locaux : pour un compte d'annuaire, la seule
    # preuve demandée était le cookie, alors que la suppression emporte les clés
    # API et l'enveloppe LiteLLM. Le commentaire qui justifiait ce trou
    # (« un compte d'annuaire n'a aucun mot de passe que le portail puisse
    # vérifier ») n'était vrai que pour le SSO, où `_verify_password_locked`
    # répond de lui-même 400. Un compte d'origine inconnue (`inconnu`) garde le
    # comportement d'avant : exiger un mot de passe que personne ne peut vérifier
    # rendrait la sortie impossible.
    if gestion['gestion'] in (GESTION_PORTAIL, GESTION_LDAP):
        motdepasse = corps.get('password') or ''
        if not motdepasse:
            return jsonify({'ok': False,
                            'error': "Mot de passe requis pour supprimer ton compte."}), 400
        ok, err = _verify_password_locked(username, motdepasse)
        if not ok:
            return jsonify(err[0]), err[1]

    if local:
        # La ligne locale d'abord : elle porte l'accès, le reste (clés,
        # enveloppe, données) s'en passe.
        db.execute("DELETE FROM local_users WHERE username=?", (username,))
        db.commit()
        rapport = deprovisionner_compte(username, username, action='account.self_delete')
        reponse = {'ok': True, 'deleted': True, 'purged': rapport}
        if rapport['keys_failed'] or not rapport['litellm_user']:
            reponse['warning'] = (
                f"{rapport['keys_failed']} clé(s) n'ont pas pu être révoquées et/ou "
                "l'enveloppe LiteLLM n'a pas pu être supprimée (service injoignable) : "
                "préviens un administrateur, ces clés peuvent encore fonctionner.")
        return jsonify(reponse)

    rapport = deprovisionner_compte(username, username, action='account.self_purge')
    return jsonify({'ok': True, 'deleted': False, 'purged': rapport})
