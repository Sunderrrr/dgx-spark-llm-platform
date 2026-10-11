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
_PAR_NOM = {c[0]: c for c in CONFIGURABLE}

# Sent by the panel to clear a secret (an empty secret field means « unchanged »:
# the browser never has the value to send it back).
EFFACER = '__effacer__'


def parametre(nom, defaut=None):
    """The effective value: the database, else the code default."""
    bdd = read_setting('cfg_' + nom, '')    # works in notification threads too
    if bdd:
        return bdd
    if defaut is None:
        entree = _PAR_NOM.get(nom)
        defaut = entree[4] if entree else ''
    return defaut


def importer_env_une_fois():
    """Copies the legacy `.env` values into the database, ONCE per key.

    Only empty settings are filled, and every key considered is remembered
    (`cfg_env_importe`, a comma list): an operator who clears a field in Admin
    means it, a value left in `.env` does not come back at the next restart —
    while a key added to the panel later (Discord, 2026-10-11) still gets its
    one import. Returns the keys imported."""
    marque = get_setting('cfg_env_importe', '') or ''
    if marque == '1':
        # The first version kept a plain flag, set before Discord joined the
        # panel: every non-Discord key had been considered.
        traitees = {c[0] for c in CONFIGURABLE if not c[0].startswith('discord_')}
    else:
        traitees = {k for k in marque.split(',') if k}
    importes = []
    for nom, _libelle, _secret, env, _defaut in CONFIGURABLE:
        if nom in traitees:
            continue
        v = (os.environ.get(env) or '').strip()
        if v and not read_setting('cfg_' + nom, ''):
            set_setting('cfg_' + nom, v)
            importes.append(nom)
        traitees.add(nom)
    set_setting('cfg_env_importe', ','.join(sorted(traitees)))
    return importes


def ldap_config():
    """LDAP connection details, service account included."""
    return {
        'uri': parametre('ldap_uri'),
        'base': parametre('ldap_base'),
        'users_dn': parametre('ldap_users_dn'),
        'login_attr': parametre('ldap_login_attr'),
        'bind_dn': parametre('ldap_bind_dn'),
        'bind_pw': parametre('ldap_bind_pw'),
    }


def oidc_config():
    c = {k: parametre('oidc_' + k) for k in
         ('metadata_url', 'client_id', 'client_secret', 'admin_group', 'redirect_uri', 'logout_url')}
    c['enabled'] = bool(c['metadata_url'] and c['client_id'] and c['client_secret'])
    return c


def smtp_config():
    try:
        port = int(parametre('smtp_port') or 587)
    except ValueError:
        port = 587
    c = {'host': parametre('smtp_host'), 'port': port, 'user': parametre('smtp_user'),
         'password': parametre('smtp_pass'), 'from': parametre('smtp_from'),
         'admin_email': parametre('admin_email')}
    c['configured'] = bool(c['host'] and c['user'] and c['password'])
    return c


def discord_config():
    """Discord: the admin-alert webhook, the bot that DMs announcements, and the
    OAuth2 app that links accounts. Each part is off while its fields are empty."""
    c = {'webhook': parametre('discord_webhook_url'),
         'bot_token': parametre('discord_bot_token'),
         'client_id': parametre('discord_client_id'),
         'client_secret': parametre('discord_client_secret'),
         'redirect_uri': parametre('discord_redirect_uri')}
    c['link_enabled'] = bool(c['client_id'] and c['client_secret'])
    return c


def definir(nom, valeur):
    """Writes a value from Admin. An empty field means « back to the default »;
    for a secret, only EFFACER clears it (empty = unchanged)."""
    entree = _PAR_NOM.get(nom)
    secret = bool(entree and entree[2])
    if secret and not valeur:
        return False
    if valeur and valeur != EFFACER:
        set_setting('cfg_' + nom, valeur)
    else:
        db = get_db()
        db.execute("DELETE FROM settings WHERE key=?", ('cfg_' + nom,))
        db.commit()
    return True


def etat_config():
    """What the admin panel shows: the value if it is NOT a secret, and only
    « configured/absent » if it is."""
    out = []
    for nom, libelle, secret, _env, _defaut in CONFIGURABLE:
        v = parametre(nom)
        out.append({
            'nom': nom,
            'libelle': libelle,
            'secret': secret,
            'valeur': '' if secret else v,
            'configure': bool(v),
            # where the value comes from — the operator's first question
            # when a setting « does not take ».
            'source': 'interface' if read_setting('cfg_' + nom, '') else 'défaut',
        })
    return out
