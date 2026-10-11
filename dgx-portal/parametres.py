"""Instance configuration: base > .env > code default.

« je veux que sur l'admin on puisse conf le ldap, le sso, le smtp, bref tout …
c'est mieux que les vars importantes soient stockées sur l'interface web et non
dans les fichiers » (operator, 2026-10-09). `.env` is the STARTING point — it
carries the secrets — but the values an operator tunes live in the database and
are editable from Admin, without touching a file or redeploying.

The precedence is the whole point:
  1. the `settings` table, written from Admin (wins);
  2. the environment, written in `.env` (the deployer's first answer);
  3. the code default (a plain clone).

Secrets are a separate matter: they NEVER come from the database and never go
to the browser. A key is reported as « configured » or « absent », never shown.
"""
from db import get_db, read_setting, set_setting

# The keys an operator tunes from Admin: (key, label, secret?, env var, code
# default). The env var is the `.env` fallback — named as the platform has
# always named it (SMTP_PASSWORD, not SMTP_PASS: the panel used to report a
# configured password as « absent »). A secret is only reported as
# configured/absent, never sent to the browser.
#
# These are read LIVE (`parametre`) by the code that uses them — LDAP login,
# the SSO client, e-mail sending, security.txt — so a change made in Admin
# applies on the next request, without a redeploy. Until 2026-10-11 the panel
# stored the values but nothing read them: only the .env value was ever used.
CONFIGURABLE = [
    ('ldap_uri',        'LDAP — URI', False, 'LDAP_URI', ''),
    ('ldap_base',       'LDAP — base DN', False, 'LDAP_BASE', 'dc=example,dc=org'),
    ('ldap_users_dn',   'LDAP — RDN des comptes', False, 'LDAP_USERS_DN', 'ou=users'),
    ('ldap_login_attr', 'LDAP — attribut de connexion', False, 'LDAP_LOGIN_ATTR', 'cn'),
    ('oidc_metadata_url', 'SSO — metadata URL', False, 'OIDC_METADATA_URL', ''),
    ('oidc_client_id',  'SSO — client ID', False, 'OIDC_CLIENT_ID', ''),
    ('oidc_client_secret', 'SSO — client secret', True, 'OIDC_CLIENT_SECRET', ''),
    ('oidc_admin_group', 'SSO — groupe administrateur', False, 'OIDC_ADMIN_GROUP', 'admins'),
    ('oidc_redirect_uri', 'SSO — URI de retour', False, 'OIDC_REDIRECT_URI', ''),
    ('oidc_logout_url', 'SSO — URL de déconnexion', False, 'OIDC_LOGOUT_URL', ''),
    ('smtp_host',       'SMTP — hôte', False, 'SMTP_HOST', ''),
    ('smtp_port',       'SMTP — port', False, 'SMTP_PORT', '587'),
    ('smtp_user',       'SMTP — utilisateur', True, 'SMTP_USER', ''),
    ('smtp_pass',       'SMTP — mot de passe', True, 'SMTP_PASSWORD', ''),
    ('smtp_from',       'SMTP — adresse expéditrice', False, 'SMTP_FROM', ''),
    ('security_contact', 'Sécurité — contact (security.txt)', False, 'DGX_SECURITY_CONTACT', ''),
]
_PAR_NOM = {c[0]: c for c in CONFIGURABLE}


def _env_de(nom):
    entree = _PAR_NOM.get(nom)
    return entree[3] if entree else nom.upper()


def parametre(nom, defaut=None):
    """The effective value: the database, then the environment, then the default."""
    bdd = read_setting('cfg_' + nom, '')    # works in notification threads too
    if bdd:
        return bdd
    import os
    if defaut is None:
        entree = _PAR_NOM.get(nom)
        defaut = entree[4] if entree else ''
    return os.environ.get(_env_de(nom), '') or defaut


def ldap_config():
    """LDAP connection details. The service account (bind DN/password) stays a
    .env secret; it is not editable from the browser."""
    import os
    return {
        'uri': parametre('ldap_uri'),
        'base': parametre('ldap_base'),
        'users_dn': parametre('ldap_users_dn'),
        'login_attr': parametre('ldap_login_attr'),
        'bind_dn': os.environ.get('LDAP_BIND_DN', ''),
        'bind_pw': os.environ.get('LDAP_BIND_PW', ''),
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
         'password': parametre('smtp_pass'), 'from': parametre('smtp_from')}
    c['configured'] = bool(c['host'] and c['user'] and c['password'])
    return c


def definir(nom, valeur):
    """Writes a value from Admin. An empty field means « back to .env ». """
    if valeur:
        set_setting('cfg_' + nom, valeur)
    else:
        db = get_db()
        db.execute("DELETE FROM settings WHERE key=?", ('cfg_' + nom,))
        db.commit()


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
            'source': ('interface' if _valeur_bdd(nom) else ('.env' if _valeur_env(nom) else 'défaut')),
        })
    return out


def _valeur_bdd(nom):
    return bool(read_setting('cfg_' + nom, ''))


def _valeur_env(nom):
    import os
    return bool(os.environ.get(_env_de(nom)))
