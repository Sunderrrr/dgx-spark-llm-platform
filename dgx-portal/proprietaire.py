"""The owner account, created from the web on the first start.

« quand on installe la solution la première fois il faut créer un compte
owner, et depuis l'interface web on configure le reste » (operator,
2026-10-11). A fresh instance has no account at all: instead of seeding one
from `.env`, the login page offers to create the OWNER — a local administrator
the other admins cannot delete, demote or block.

Who may create it: whoever reads the setup token. The page is public, and a
fresh instance can be reachable from the internet before its operator opens it:
without a secret, the first visitor would own the platform. The token is
printed in the portal's log and written to `data/setup-token` (the data volume),
both of which only the server's operator can read; it is single-use and
disappears with the owner's creation.

Setup is only offered while NOTHING administers the instance: no enabled local
admin (own flag or group), no directory account with the admin right, no owner.
An existing deployment never sees it.
"""
import hmac
import os
import secrets
from datetime import datetime

from werkzeug.security import generate_password_hash

from db import DB_PATH, get_db, get_setting, set_setting

CLE_PROPRIETAIRE = 'owner_username'
CLE_JETON = 'setup_token'
FICHIER_JETON = os.path.join(os.path.dirname(DB_PATH), 'setup-token')


def proprietaire():
    return get_setting(CLE_PROPRIETAIRE, '') or ''


def est_proprietaire(username):
    p = proprietaire()
    return bool(p and username and p == username)


def setup_requis():
    if proprietaire():
        return False
    db = get_db()
    admin_local = db.execute(
        "SELECT 1 FROM local_users u LEFT JOIN user_groups g ON g.name = u.group_name "
        "WHERE u.enabled = 1 AND (u.is_admin = 1 OR g.is_admin = 1) LIMIT 1").fetchone()
    if admin_local:
        return False
    admin_annuaire = db.execute(
        "SELECT 1 FROM user_sources WHERE last_is_admin = 1 LIMIT 1").fetchone()
    return admin_annuaire is None


def jeton_setup():
    """The setup token, created on first need (shared by every worker: the first
    insert wins); None when setup is not required."""
    if not setup_requis():
        return None
    db = get_db()
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
               (CLE_JETON, secrets.token_urlsafe(24)))
    db.commit()
    jeton = get_setting(CLE_JETON, '')
    try:
        with open(FICHIER_JETON, 'w') as f:
            f.write(jeton + '\n')
        os.chmod(FICHIER_JETON, 0o600)
    except OSError:
        pass                     # the log line remains
    return jeton


def jeton_valide(propose):
    jeton = get_setting(CLE_JETON, '')
    return bool(jeton and propose and hmac.compare_digest(jeton, propose.strip()))


def creer_proprietaire(username, fullname, password):
    """Creates the owner (local admin) and closes setup. The caller validated
    the token, the identifier and the password policy."""
    db = get_db()
    if db.execute("SELECT 1 FROM local_users WHERE username=?", (username,)).fetchone():
        db.execute("UPDATE local_users SET password_hash=?, fullname=?, is_admin=1, enabled=1 "
                   "WHERE username=?", (generate_password_hash(password), fullname, username))
    else:
        db.execute(
            "INSERT INTO local_users (username, password_hash, fullname, is_admin, group_name, "
            "max_budget, enabled, created_at) VALUES (?,?,?,1,NULL,NULL,1,?)",
            (username, generate_password_hash(password), fullname, datetime.now().isoformat()))
    db.execute("DELETE FROM settings WHERE key=?", (CLE_JETON,))
    db.commit()
    set_setting(CLE_PROPRIETAIRE, username)
    try:
        os.remove(FICHIER_JETON)
    except OSError:
        pass
