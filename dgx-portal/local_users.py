"""Comptes locaux geres par l'administrateur (table `local_users`).

Extrait de app.py le 28/08. Ces aides etaient dispersees : quatre au debut du
monolithe, _parse_budget deux mille lignes plus bas avec les routes qui s'en
servent. Elles forment un seul sujet — l'authentification et le budget d'un
compte local — et le blueprint d'administration en a besoin.

C'est la gestion normale de comptes, en base, par l'administrateur.
"""
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from config import KEY_BUDGET, KEY_DURATION
from db import get_db, get_setting
from litellm_client import _ensure_litellm_user, litellm_update_user_budget

def _local_group(name, groupes=None):
    """Ligne du groupe `name`, ou None.

    `groupes` (nom → ligne) évite un SELECT par appel quand l'appelant a déjà
    chargé la table : `/api/admin/users` parcourt tous les comptes et appelait
    cette fonction DEUX fois par compte (droits + quota effectif), soit 2N
    requêtes pour une table qu'il lit de toute façon en entier.
    """
    if not name:
        return None
    if groupes is not None:
        return groupes.get(name)
    return get_db().execute("SELECT * FROM user_groups WHERE name=?", (name,)).fetchone()

def _local_user_effective_budget(row, groupes=None):
    """Effective quota: user override → group quota → global default."""
    if row['max_budget'] is not None:
        return row['max_budget']
    g = _local_group(row['group_name'], groupes)
    if g and g['max_budget'] is not None:
        return g['max_budget']
    return float(get_setting('default_key_budget', KEY_BUDGET))

def _local_user_is_admin(row, groupes=None):
    g = _local_group(row['group_name'], groupes)
    return bool(row['is_admin']) or bool(g['is_admin'] if g else 0)

# Hash jetable pour égaliser le temps de réponse (cf. oracle d'énumération).
_HASH_FACTICE = generate_password_hash('mot-de-passe-factice')


def _local_user_auth(username, password):
    """(ok, is_admin, fullname) against local_users, HASHED password (werkzeug).
    """
    row = get_db().execute(
        "SELECT * FROM local_users WHERE username=? AND enabled=1", (username,)).fetchone()
    if row is None:
        # Compte inexistant : on paie QUAND MÊME le coût du KDF. Le `or` de la
        # version précédente court-circuitait avant `check_password_hash`, donc
        # le temps de réponse de /login distinguait « compte local existant » de
        # « inexistant » (oracle d'énumération gratuit, sans déclencher le
        # verrou : corps et statut identiques).
        check_password_hash(_HASH_FACTICE, password or '')
        return False, False, None
    if not check_password_hash(row['password_hash'], password):
        return False, False, None
    return True, _local_user_is_admin(row), (row['fullname'] or username)

def _sync_local_user_budget(username, row):
    """Propagates the local account's effective quota to LiteLLM (create + update).

    Renvoie False si l'enveloppe n'a PAS pu être écrite. L'ancienne version
    avalait l'exception : l'admin voyait « compte créé » alors que le quota
    n'existait pas côté LiteLLM, c'est-à-dire un compte de fait illimité. Un
    échec de quota est un échec de sécurité, il doit remonter à l'appelant.
    """
    try:
        eff = _local_user_effective_budget(row)
        # La durée accompagne TOUJOURS l'écriture : la docstring de
        # litellm_update_user_budget affirme qu'un update sans durée conserve
        # celle du compte, et la mesure du 2026-09-13 le confirme sur cette
        # version (7d et budget_reset_at inchangés après un update sans le
        # champ). Mais un compte qui n'a JAMAIS eu de durée resterait alors sans
        # fenêtre de remise à zéro, c'est-à-dire avec un plafond à vie — le
        # gotcha consigné le 2026-09-08. Écrire la durée partout évite d'avoir à
        # se souvenir de laquelle des deux lectures s'applique.
        duree = get_setting('default_key_duration', KEY_DURATION)
        _ensure_litellm_user(username, eff, duree)
        return bool(litellm_update_user_budget(username, eff, budget_duration=duree))
    except Exception as exc:                                     # noqa: BLE001
        print(f'[local_users] quota NON appliqué pour {username} : {exc}')
        return False


# Mots de passe refusés même s'ils font 8 caractères : ce sont les premiers
# essayés par un bourrage d'identifiants, et un compte local n'a pas forcément
# de second facteur pour rattraper le coup (la passkey est optionnelle).
_MOTS_DE_PASSE_INTERDITS = {
    'password', 'password1', 'password123', 'motdepasse', 'motdepasse1',
    '12345678', '123456789', '1234567890', 'azertyui', 'azertyuiop',
    'qwertyui', 'qwertyuiop', 'admin123', 'administrateur', 'changeme',
    'cronos123', 'dgxspark', 'iloveyou', 'soleil123', '00000000',
}


def password_policy_error(password, username=None):
    """None si le mot de passe est acceptable, sinon le message à afficher.

    Règle volontairement courte : longueur minimale + refus des évidences.
    Pas de rotation forcée ni d'exigence de classes de caractères — elles
    poussent aux mots de passe prévisibles (« Ete2026! ») sans rien apporter.
    """
    if not password or len(password) < 8:
        return 'Mot de passe trop court (8 caractères minimum).'
    p = password.lower()
    if p in _MOTS_DE_PASSE_INTERDITS:
        return 'Ce mot de passe est trop courant : choisis-en un autre.'
    if username:
        u = username.lower()
        if len(u) >= 4 and u in p:
            return "Le mot de passe ne doit pas contenir l'identifiant."
    if len(set(password)) < 4:
        return 'Mot de passe trop répétitif (trop peu de caractères différents).'
    return None

def _record_user_source(username, source, fullname=None, is_admin=None):
    """Records that a user logged in via `source` (local/ldap/
    sso). Cumulative: an account present in LDAP AND in SSO ends with both.
    `is_admin` (0/1/None) is the role this login granted; it is stored as
    last_is_admin so the admin "Users" view can show the effective role with
    the directory (SSO/LDAP) taking precedence over the local record.
    """
    try:
        db = get_db()
        row = db.execute("SELECT sources FROM user_sources WHERE username=?", (username,)).fetchone()
        srcs = set((row['sources'] or '').split(',')) if row else set()
        srcs.discard('')
        srcs.add(source)
        now = datetime.now().isoformat()
        admin_val = int(is_admin) if is_admin is not None else None
        db.execute(
            "INSERT INTO user_sources (username, sources, fullname, last_source, last_seen, last_is_admin) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
            "sources=excluded.sources, fullname=COALESCE(excluded.fullname, user_sources.fullname), "
            "last_source=excluded.last_source, last_seen=excluded.last_seen, "
            "last_is_admin=excluded.last_is_admin",
            (username, ','.join(sorted(srcs)), fullname, source, now, admin_val))
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


# ── D'où vient le mot de passe d'un compte ───────────────────────────────────
# Le portail connaît TROIS chemins de connexion (local_users → LDAP → SSO) et
# les consigne de façon CUMULATIVE dans `user_sources` : un même compte peut
# avoir une ligne locale ET s'être connecté en SSO. Annoncer « compte SSO » à
# quelqu'un qui possède aussi un mot de passe local serait donc faux — et lui
# cacher un formulaire qui fonctionne serait pire que de l'afficher.
#
# Règle de vérité, dans cet ordre :
#   1. une ligne `local_users` → le portail détient le hash, il fait autorité ;
#   2. sinon `ldap` → le mot de passe vit dans l'annuaire (bind LDAP) ;
#   3. sinon `sso`  → il vit chez le fournisseur d'identité ;
#   4. sinon → on ne SAIT PAS, et on le dit.
# L'absence de source consignée n'est pas une preuve d'absence : c'est déjà la
# règle d'`auth.etat_compte`, et l'inverse avait déconnecté tout le monde.
GESTION_PORTAIL = 'portail'
GESTION_LDAP = 'annuaire-ldap'
GESTION_SSO = 'fournisseur-sso'
GESTION_INCONNUE = 'inconnu'


def gestion_mot_de_passe(username):
    """{'gestion', 'sources', 'local'} : qui détient le mot de passe du compte.

    Sert à l'interface, qui ne montre le formulaire de changement que si
    `gestion == 'portail'` et dit où le changer sinon. Le serveur refuse de
    toute façon (`/api/account/password` ne touche que `local_users`) — mais un
    formulaire qui échoue après coup n'est pas une réponse acceptable.
    """
    db = get_db()
    local = db.execute("SELECT 1 FROM local_users WHERE username=?",
                       (username,)).fetchone() is not None
    row = db.execute("SELECT sources FROM user_sources WHERE username=?",
                     (username,)).fetchone()
    sources = sorted(s for s in ((row['sources'] if row else '') or '').split(',') if s)
    if local:
        gestion = GESTION_PORTAIL
    elif 'ldap' in sources:
        gestion = GESTION_LDAP
    elif 'sso' in sources:
        gestion = GESTION_SSO
    else:
        gestion = GESTION_INCONNUE
    return {'gestion': gestion, 'sources': sources, 'local': local,
            'passkey': gestion in (GESTION_PORTAIL, GESTION_LDAP)}


def passkey_possible(username):
    """La double authentification par passkey est-elle proposable ?

    Ajouter, retirer une clé ou basculer la 2FA exige une RE-VÉRIFICATION par
    mot de passe (`/api/security/*`). Un compte SSO n'en a aucun que le portail
    puisse vérifier : lui montrer l'interrupteur ne mènerait qu'à « Mot de passe
    incorrect », ce qui est faux. Portée produit : local + LDAP.
    """
    return gestion_mot_de_passe(username)['passkey']
