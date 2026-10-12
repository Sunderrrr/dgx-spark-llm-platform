"""Instance configuration: set from the web interface, nowhere else.

« je veux que le SSO, le LDAP et le SMTP soient configurables que depuis
l'interface web […] je n'aime pas avoir trop de choses dans le .env »
(operator, 2026-10-11). `.env` keeps what the stack needs to START (its two
public origins and the generated infrastructure secrets); everything an
operator tunes — LDAP, SSO, SMTP, the alert recipient, the security contact —
— and Discord — lives in the portal database and is edited from Admin → Config:

  1. the `settings` table, written from Admin;
  2. the code default (a fresh install: nothing configured, local accounts only).

An older deployment that still carries these values in `.env` is not cut off:
at the first start of this version they are copied into the database once
(`importer_env_une_fois`), after which `.env` is never read for them again and
the lines can be deleted.

Secrets (passwords, client secret) are written from the browser but never read
back: the panel only says « configured » or « absent ».
"""
import os

from db import get_db, get_setting, read_setting, set_setting

# (key, label, secret?, legacy env var — read ONCE by the import, code default)
CONFIGURABLE = [
    ('ldap_uri',        'LDAP — URI', False, 'LDAP_URI', ''),
    ('ldap_base',       'LDAP — base DN', False, 'LDAP_BASE', 'dc=example,dc=org'),
    ('ldap_users_dn',   'LDAP — RDN des comptes', False, 'LDAP_USERS_DN', 'ou=users'),
    ('ldap_login_attr', 'LDAP — attribut de connexion', False, 'LDAP_LOGIN_ATTR', 'cn'),
    ('ldap_bind_dn',    'LDAP — compte de service (DN)', False, 'LDAP_BIND_DN', ''),
    ('ldap_bind_pw',    'LDAP — mot de passe du compte de service', True, 'LDAP_BIND_PW', ''),
    ('oidc_metadata_url', 'SSO — metadata URL', False, 'OIDC_METADATA_URL', ''),
    ('oidc_client_id',  'SSO — client ID', False, 'OIDC_CLIENT_ID', ''),
    ('oidc_client_secret', 'SSO — client secret', True, 'OIDC_CLIENT_SECRET', ''),
    ('oidc_admin_group', 'SSO — groupe administrateur', False, 'OIDC_ADMIN_GROUP', 'admins'),
    ('oidc_redirect_uri', 'SSO — URI de retour', False, 'OIDC_REDIRECT_URI', ''),
    ('oidc_logout_url', 'SSO — URL de déconnexion', False, 'OIDC_LOGOUT_URL', ''),
    ('smtp_host',       'SMTP — hôte', False, 'SMTP_HOST', ''),
    ('smtp_port',       'SMTP — port', False, 'SMTP_PORT', '587'),
    ('smtp_user',       'SMTP — utilisateur', False, 'SMTP_USER', ''),
    ('smtp_pass',       'SMTP — mot de passe', True, 'SMTP_PASSWORD', ''),
    ('smtp_from',       'SMTP — adresse expéditrice', False, 'SMTP_FROM', ''),
    ('admin_email',     'SMTP — destinataire des alertes', False, 'ADMIN_EMAIL', ''),
    ('discord_webhook_url', 'Discord — webhook des alertes admin', True, 'DISCORD_WEBHOOK_URL', ''),
    ('discord_bot_token', 'Discord — jeton du bot (messages privés)', True, 'DISCORD_BOT_TOKEN', ''),
    ('discord_client_id', 'Discord — client ID (liaison des comptes)', False, 'DISCORD_CLIENT_ID', ''),
    ('discord_client_secret', 'Discord — client secret', True, 'DISCORD_CLIENT_SECRET', ''),
    ('discord_redirect_uri', 'Discord — URI de retour', False, 'DISCORD_REDIRECT_URI', ''),
    ('security_contact', 'Sécurité — contact (security.txt)', False, 'DGX_SECURITY_CONTACT', ''),
]
_BY_NAME = {c[0]: c for c in CONFIGURABLE}

# Sent by the panel to clear a secret (an empty secret field means « unchanged »:
# the browser never has the value to send it back).
CLEAR = '__effacer__'


def setting(name, default=None):
    """The effective value: the database, else the code default."""
    db_value = read_setting('cfg_' + name, '')    # works in notification threads too
    if db_value:
        return db_value
    if default is None:
        entry = _BY_NAME.get(name)
        default = entry[4] if entry else ''
    return default


def import_env_once():
    """Copies the legacy `.env` values into the database, ONCE per key.

    Only empty settings are filled, and every key considered is remembered
    (`cfg_env_imported`, a comma list): an operator who clears a field in Admin
    means it, a value left in `.env` does not come back at the next restart —
    while a key added to the panel later (Discord, 2026-10-11) still gets its
    one import. Returns the keys imported."""
    brand = get_setting('cfg_env_imported', '') or ''
    if brand == '1':
        # The first version kept a plain flag, set before Discord joined the
        # panel: every non-Discord key had been considered.
        considered = {c[0] for c in CONFIGURABLE if not c[0].startswith('discord_')}
    else:
        considered = {k for k in brand.split(',') if k}
    imported = []
    for name, _label, _secret, env, _default in CONFIGURABLE:
        if name in considered:
            continue
        v = (os.environ.get(env) or '').strip()
        if v and not read_setting('cfg_' + name, ''):
            set_setting('cfg_' + name, v)
            imported.append(name)
        considered.add(name)
    set_setting('cfg_env_imported', ','.join(sorted(considered)))
    return imported


def ldap_config():
    """LDAP connection details, service account included."""
    return {
        'uri': setting('ldap_uri'),
        'base': setting('ldap_base'),
        'users_dn': setting('ldap_users_dn'),
        'login_attr': setting('ldap_login_attr'),
        'bind_dn': setting('ldap_bind_dn'),
        'bind_pw': setting('ldap_bind_pw'),
    }


def oidc_config():
    c = {k: setting('oidc_' + k) for k in
         ('metadata_url', 'client_id', 'client_secret', 'admin_group', 'redirect_uri', 'logout_url')}
    c['enabled'] = bool(c['metadata_url'] and c['client_id'] and c['client_secret'])
    return c


def smtp_config():
    try:
        port = int(setting('smtp_port') or 587)
    except ValueError:
        port = 587
    c = {'host': setting('smtp_host'), 'port': port, 'user': setting('smtp_user'),
         'password': setting('smtp_pass'), 'from': setting('smtp_from'),
         'admin_email': setting('admin_email')}
    c['configured'] = bool(c['host'] and c['user'] and c['password'])
    return c


def discord_config():
    """Discord: the admin-alert webhook, the bot that DMs announcements, and the
    OAuth2 app that links accounts. Each part is off while its fields are empty."""
    c = {'webhook': setting('discord_webhook_url'),
         'bot_token': setting('discord_bot_token'),
         'client_id': setting('discord_client_id'),
         'client_secret': setting('discord_client_secret'),
         'redirect_uri': setting('discord_redirect_uri')}
    c['link_enabled'] = bool(c['client_id'] and c['client_secret'])
    return c


def set_value(name, value):
    """Writes a value from Admin. An empty field means « back to the default »;
    for a secret, only EFFACER clears it (empty = unchanged)."""
    entry = _BY_NAME.get(name)
    secret = bool(entry and entry[2])
    if secret and not value:
        return False
    if value and value != CLEAR:
        set_setting('cfg_' + name, value)
    else:
        db = get_db()
        db.execute("DELETE FROM settings WHERE key=?", ('cfg_' + name,))
        db.commit()
    return True


def config_state():
    """What the admin panel shows: the value if it is NOT a secret, and only
    « configured/absent » if it is."""
    out = []
    for name, label, secret, _env, _default in CONFIGURABLE:
        v = setting(name)
        out.append({
            'name': name,
            'label': label,
            'secret': secret,
            'value': '' if secret else v,
            'configure': bool(v),
            # where the value comes from — the operator's first question
            # when a setting « does not take ».
            'source': 'interface' if read_setting('cfg_' + name, '') else 'défaut',
            # The code default, shown as the field's placeholder (never a secret).
            'default': '' if secret else _default,
        })
    return out
