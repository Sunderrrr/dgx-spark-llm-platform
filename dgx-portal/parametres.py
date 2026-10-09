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
from db import get_db, get_setting, set_setting

# The keys an operator tunes from Admin. Each: (key, label, secret?).
# A secret one is only ever reported as configured/absent.
CONFIGURABLE = [
    ('ldap_uri',        'LDAP — URI', False),
    ('ldap_base',       'LDAP — base DN', False),
    ('ldap_users_dn',   'LDAP — RDN des comptes', False),
    ('ldap_login_attr', 'LDAP — attribut de connexion', False),
    ('oidc_metadata_url', 'SSO — metadata URL', False),
    ('oidc_client_id',  'SSO — client ID', False),
    ('oidc_client_secret', 'SSO — client secret', True),
    ('oidc_admin_group', 'SSO — groupe administrateur', False),
    ('oidc_redirect_uri', 'SSO — URI de retour', False),
    ('smtp_host',       'SMTP — hôte', False),
    ('smtp_port',       'SMTP — port', False),
    ('smtp_user',       'SMTP — utilisateur', True),
    ('smtp_pass',       'SMTP — mot de passe', True),
    ('smtp_from',       'SMTP — adresse expéditrice', False),
]


def parametre(nom, defaut=''):
    """The effective value: the database first, then the environment."""
    bdd = get_setting('cfg_' + nom, '') if _bdd_dispo() else ''
    if bdd:
        return bdd
    import os
    return os.environ.get(nom.upper(), defaut) or defaut


def _bdd_dispo():
    try:
        get_db()
        return True
    except Exception:
        return False


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
    for nom, libelle, secret in CONFIGURABLE:
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
    return bool(_bdd_dispo() and get_setting('cfg_' + nom, ''))


def _valeur_env(nom):
    import os
    return bool(os.environ.get(nom.upper()))
