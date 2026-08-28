"""Gardes d'authentification et duree de vie des sessions.

Extrait de app.py le 28/08 — TROISIEME piece du noyau, apres db.py et config.py,
et celle sans laquelle aucun blueprint n'etait possible : un module de routes doit
importer login_required/admin_required, or ces decorateurs vivaient dans app.py,
que ce meme module ne peut pas reimporter.

Ne depend que de flask et de l'environnement. url_for('login') et url_for('index')
sont resolus A L'APPEL, pas a l'import : les routes correspondantes restent
enregistrees sur l'application dans app.py, donc rien a passer ici.
"""
import os
import time
from functools import wraps

from flask import abort, flash, redirect, request, session, url_for

_API_FETCH_PATHS = ('/playground/chat', '/support/chat', '/admin/runner/stream')


def _is_api_request():
    # Distinguishes fetch/JSON calls (Next.js driver) from classic navigation:
    # fetch() follows 302 redirects automatically and would return /login's HTML
    # with a 200 code, hiding the session expiry from the frontend.
    return request.path.startswith('/api/') or request.path in _API_FETCH_PATHS


# Absolute session lifetime (not inactivity: we don't extend it on
# each request, it really is a cap from login time). 12 h = one
# workday, the user reconnects the next day. Incidentally,
# this bounds how long a stale is_admin remains valid.
SESSION_MAX_AGE = int(os.environ.get('SESSION_MAX_AGE', 12 * 3600))


def _session_expired():
    if 'username' not in session:
        return False
    # Sessions created before auth_at was introduced: treated as
    # expired rather than eternal.
    return time.time() - session.get('auth_at', 0) > SESSION_MAX_AGE


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _session_expired():
            session.clear()
        if 'username' not in session:
            if _is_api_request():
                abort(401)
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    # Marqueur lu a l'execution par le test de garde des routes : @wraps efface
    # toute trace du decorateur, donc sans lui il faudrait analyser le source —
    # fragile, et aveugle a une route enregistree autrement qu'en litteral.
    decorated._garde = 'login'
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _session_expired():
            session.clear()
        if 'username' not in session:
            if _is_api_request():
                abort(401)
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            if _is_api_request():
                abort(403)
            flash("Accès réservé aux administrateurs.", "danger")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    decorated._garde = 'admin'          # cf. login_required
    return decorated
