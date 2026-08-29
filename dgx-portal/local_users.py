"""Comptes locaux geres par l'administrateur (table `local_users`).

Extrait de app.py le 28/08. Ces aides etaient dispersees : quatre au debut du
monolithe, _parse_budget deux mille lignes plus bas avec les routes qui s'en
servent. Elles forment un seul sujet — l'authentification et le budget d'un
compte local — et le blueprint d'administration en a besoin.

C'est la gestion normale de comptes, en base, par l'administrateur.
"""
from datetime import datetime

from werkzeug.security import check_password_hash

from config import KEY_BUDGET, KEY_DURATION
from db import get_db, get_setting
from litellm_client import _ensure_litellm_user, litellm_update_user_budget

def _local_group(name):
    if not name:
        return None
    return get_db().execute("SELECT * FROM user_groups WHERE name=?", (name,)).fetchone()

def _local_user_effective_budget(row):
    """Effective quota: user override → group quota → global default."""
    if row['max_budget'] is not None:
        return row['max_budget']
    g = _local_group(row['group_name'])
    if g and g['max_budget'] is not None:
        return g['max_budget']
    return float(get_setting('default_key_budget', KEY_BUDGET))

def _local_user_is_admin(row):
    g = _local_group(row['group_name'])
    return bool(row['is_admin']) or bool(g['is_admin'] if g else 0)

def _local_user_auth(username, password):
    """(ok, is_admin, fullname) against local_users, HASHED password (werkzeug).
    """
    row = get_db().execute(
        "SELECT * FROM local_users WHERE username=? AND enabled=1", (username,)).fetchone()
    if not row or not check_password_hash(row['password_hash'], password):
        return False, False, None
    return True, _local_user_is_admin(row), (row['fullname'] or username)

def _sync_local_user_budget(username, row):
    """Propagates the local account's effective quota to LiteLLM (create + update)."""
    try:
        eff = _local_user_effective_budget(row)
        _ensure_litellm_user(username, eff, get_setting('default_key_duration', KEY_DURATION))
        litellm_update_user_budget(username, eff)
    except Exception:
        pass

def _record_user_source(username, source, fullname=None):
    """Records that a user logged in via `source` (local/ldap/
    sso). Cumulative: an account present in LDAP AND in SSO ends with both.
    Feeds the admin "Users" view (Source column).
    """
    try:
        db = get_db()
        row = db.execute("SELECT sources FROM user_sources WHERE username=?", (username,)).fetchone()
        srcs = set((row['sources'] or '').split(',')) if row else set()
        srcs.discard('')
        srcs.add(source)
        now = datetime.now().isoformat()
        db.execute(
            "INSERT INTO user_sources (username, sources, fullname, last_source, last_seen) "
            "VALUES (?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
            "sources=excluded.sources, fullname=COALESCE(excluded.fullname, user_sources.fullname), "
            "last_source=excluded.last_source, last_seen=excluded.last_seen",
            (username, ','.join(sorted(srcs)), fullname, source, now))
        db.commit()
    except Exception:
        pass

def _parse_budget(raw):
    """'' → None (will inherit from group/default); otherwise a positive integer or an error."""
    raw = (raw or '').strip().replace(' ', '')
    if not raw:
        return None, None
    try:
        v = int(float(raw))
        if v <= 0:
            return None, "Le quota doit être un entier positif."
        return v, None
    except ValueError:
        return None, "Quota invalide."
