"""Liaison d'un compte Discord (OAuth2 « identify »).

Extrait de app.py le 28/08. La banniere « Discord account linking » couvrait en
realite la page d'accueil, la gestion des cles et la deconnexion en plus de la
liaison — encore une frontiere mal placee. Seules les quatre routes Discord
sont ici ; index, logout, api_home et keys restent dans app.py, les deux
premieres parce qu'elles sont visees par url_for et doivent garder leur nom
d'endpoint.

Les deux url_for('discord_callback') deviennent url_for('discord.discord_callback') :
un blueprint prefixe ses endpoints de son nom. Les CHEMINS, eux, ne changent
pas — pas d'url_prefix — donc l'URL de rappel enregistree chez Discord reste
valable.
"""
import re
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode

import requests
from flask import (Blueprint, flash, jsonify, redirect, request, session,
                   url_for)

from auth import login_required
from config import (DISCORD_API, DISCORD_BOT_TOKEN, DISCORD_CLIENT_ID,
                    DISCORD_CLIENT_SECRET, DISCORD_LINK_ENABLED,
                    DISCORD_REDIRECT_URI)
from db import get_db

bp = Blueprint('discord', __name__)

# Opt-in: a logged-in user links their Discord account so the bot can DM them
# announcements. Manual OAuth2 code flow (requests) — no coupling to the OIDC
# client above, no gateway bot.
@bp.route('/discord/link')
@login_required
def discord_link():
    if not DISCORD_LINK_ENABLED:
        flash("La liaison Discord n'est pas configurée.", "danger")
        return redirect('/?discord=unavailable')
    state = secrets.token_urlsafe(24)
    session['discord_oauth_state'] = state
    redirect_uri = DISCORD_REDIRECT_URI or url_for('discord.discord_callback', _external=True)
    params = urlencode({
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'identify',
        'state': state,
        'prompt': 'consent',
    })
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")


@bp.route('/discord/callback')
@login_required
def discord_callback():
    if not DISCORD_LINK_ENABLED:
        return redirect('/?discord=unavailable')
    state = request.args.get('state')
    if not state or state != session.pop('discord_oauth_state', None):
        flash("Discord : échec de la vérification. Réessaie.", "danger")
        return redirect('/?discord=error')
    code = request.args.get('code')
    if not code:
        flash("Discord : autorisation refusée.", "warning")
        return redirect('/?discord=error')
    redirect_uri = DISCORD_REDIRECT_URI or url_for('discord.discord_callback', _external=True)
    try:
        tok = requests.post(f"{DISCORD_API}/oauth2/token",
                            data={'client_id': DISCORD_CLIENT_ID,
                                  'client_secret': DISCORD_CLIENT_SECRET,
                                  'grant_type': 'authorization_code',
                                  'code': code,
                                  'redirect_uri': redirect_uri},
                            headers={'Content-Type': 'application/x-www-form-urlencoded'},
                            timeout=8)
        access = tok.json().get('access_token') if tok.ok else None
        if not access:
            raise RuntimeError('token exchange failed')
        me = requests.get(f"{DISCORD_API}/users/@me",
                          headers={'Authorization': f'Bearer {access}'}, timeout=8)
        info = me.json() if me.ok else {}
        did = info.get('id')
        dname = info.get('global_name') or info.get('username') or ''
        if info.get('discriminator') and info.get('discriminator') not in ('0', 0, None):
            dname = f"{info.get('username', dname)}#{info['discriminator']}"
        if not did:
            raise RuntimeError('no user id')
    except Exception:
        flash("Discord : échec de la liaison. Réessaie.", "danger")
        return redirect('/?discord=error')
    try:
        db = get_db()
        db.execute(
            "INSERT OR REPLACE INTO discord_links (username, discord_id, discord_name, linked_at) VALUES (?,?,?,?)",
            (session['username'], str(did), dname, datetime.now().isoformat()))
        db.commit()
    except Exception:
        flash("Discord : erreur d'enregistrement de la liaison.", "danger")
        return redirect('/?discord=error')
    flash("Compte Discord lié — tu recevras les annonces en message privé.", "success")
    return redirect('/?discord=linked')


@bp.route('/discord/unlink', methods=['POST'])
@login_required
def discord_unlink():
    try:
        db = get_db()
        db.execute("DELETE FROM discord_links WHERE username=?", (session['username'],))
        db.commit()
    except Exception:
        pass
    flash("Compte Discord délié.", "success")
    return redirect('/?discord=unlinked')


@bp.route('/api/discord/status')
@login_required
def api_discord_status():
    name = None
    try:
        db = get_db()
        row = db.execute("SELECT discord_name FROM discord_links WHERE username=?",
                         (session['username'],)).fetchone()
        name = row['discord_name'] if row else None
    except Exception:
        pass
    return jsonify({
        'linkable': DISCORD_LINK_ENABLED,
        'dm_enabled': bool(DISCORD_BOT_TOKEN),
        'linked': name is not None,
        'discord_name': name or '',
    })
