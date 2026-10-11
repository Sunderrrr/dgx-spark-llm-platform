"""Groups brought down from the directory (LDAP) and the SSO provider.

At each login, the account's groups — LDAP `memberOf`, the SSO token's
`groups` claim — are imported into the portal's groups (`user_groups`). A group
is ONE row whatever brought it: the same name in LDAP and in SSO, or a local
group of that name, is merged (names compared case-insensitively), and
`sources` lists where it comes from. An admin then sets a quota or the admin
right on it, exactly as on a local group, and it applies to its directory
members:

  - admin right: a member of an admin group is an admin (on top of the
    directory's own admin group, Admin → Config);
  - quota: the most generous quota among the member's groups becomes its
    LiteLLM cap — unless an admin set this account's budget by hand, or a
    temporary grant is running: an admin decision is never overwritten.

Local accounts keep their single local group (`local_users.group_name`); this
module only acts on accounts without a local row.
"""
from datetime import datetime

from config import KEY_BUDGET, KEY_DURATION
from db import get_db, get_setting
from directory import noms_de_groupes
from litellm_client import _ensure_litellm_user, litellm_update_user_budget
from parametres import parametre

SOURCES = ('ldap', 'sso')
_NOM_MAX = 80


def _groupe(db, nom):
    return db.execute("SELECT * FROM user_groups WHERE lower(name)=lower(?)", (nom,)).fetchone()


def importer_groupes(username, par_source):
    """Records this account's groups, per source ({'ldap': [...], 'sso': [...]},
    DNs or names), creating or merging the group rows. A source absent from
    `par_source` keeps its previous memberships (an SSO login does not erase
    what an LDAP login recorded, and vice versa)."""
    db = get_db()
    now = datetime.now().isoformat()
    for source, valeurs in par_source.items():
        if source not in SOURCES or valeurs is None:
            continue
        db.execute("DELETE FROM user_directory_groups WHERE username=? AND source=?",
                   (username, source))
        for nom in noms_de_groupes(valeurs):
            nom = nom[:_NOM_MAX]
            g = _groupe(db, nom)
            if g is None:
                db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at, sources) "
                           "VALUES (?,?,?,?,?)", (nom, None, 0, now, source))
                canon = nom
            else:
                canon = g['name']
                sources = set((g['sources'] or '').split(',')) - {''}
                if source not in sources:
                    db.execute("UPDATE user_groups SET sources=? WHERE name=?",
                               (','.join(sorted(sources | {source})), canon))
            db.execute("INSERT OR REPLACE INTO user_directory_groups "
                       "(username, group_name, source, seen_at) VALUES (?,?,?,?)",
                       (username, canon, source, now))
    db.commit()


def groupes_de(username):
    """The account's directory groups (merged: one row per group)."""
    return get_db().execute(
        "SELECT g.name, g.max_budget, g.is_admin, g.sources FROM user_groups g "
        "WHERE g.name IN (SELECT group_name FROM user_directory_groups WHERE username=?) "
        "ORDER BY g.name", (username,)).fetchall()


def droits_par_groupes(username):
    """(is_admin, quota) the account's directory groups grant: admin if one of
    them is the directory's admin group or carries the admin right; the most
    generous quota set on them, else None."""
    groupes = groupes_de(username)
    groupe_admin = (parametre('oidc_admin_group') or '').strip().lower()
    is_admin = any(g['is_admin'] or (groupe_admin and g['name'].lower() == groupe_admin)
                   for g in groupes)
    quotas = [g['max_budget'] for g in groupes if g['max_budget'] is not None]
    return is_admin, (max(quotas) if quotas else None)


def appliquer_quota_groupes(username):
    """Writes the group quota as this directory account's LiteLLM cap. Returns
    the cap written, or None when nothing was (local account, budget set by
    hand, grant running, no group quota). Never raises: a login must not fail
    on a quota."""
    try:
        db = get_db()
        if db.execute("SELECT 1 FROM local_users WHERE username=?", (username,)).fetchone():
            return None
        rs = db.execute("SELECT budget_manual, budget_auto FROM user_sources WHERE username=?",
                        (username,)).fetchone()
        if rs is None or rs['budget_manual']:
            return None
        if db.execute("SELECT 1 FROM budget_grants WHERE username=? AND expires_at > ?",
                      (username, datetime.now().timestamp())).fetchone():
            return None
        _, quota = droits_par_groupes(username)
        precedent = rs['budget_auto']
        defaut = float(get_setting('default_key_budget', KEY_BUDGET))
        if quota is not None:
            cible = float(quota)
        elif precedent is not None:
            cible = defaut            # left its quota group(s): back to the default
        else:
            return None               # never had a group quota: untouched
        if precedent is not None and float(precedent) == cible:
            return None               # already applied
        duree = get_setting('default_key_duration', KEY_DURATION)
        _ensure_litellm_user(username, cible, duree)
        if not litellm_update_user_budget(username, cible, budget_duration=duree):
            return None
        db.execute("UPDATE user_sources SET budget_auto=? WHERE username=?",
                   (cible if quota is not None else None, username))
        db.commit()
        return cible
    except Exception as exc:                                     # noqa: BLE001
        print(f'[directory_groups] quota de groupe non appliqué pour {username} : {exc}')
        return None


def recalculer_membres(nom_groupe):
    """After an admin changed a group: its directory members' admin right and
    quota follow at once, not at their next login."""
    db = get_db()
    membres = [r['username'] for r in db.execute(
        "SELECT DISTINCT d.username FROM user_directory_groups d "
        "LEFT JOIN local_users l ON l.username = d.username "
        "WHERE lower(d.group_name)=lower(?) AND l.username IS NULL", (nom_groupe,)).fetchall()]
    for u in membres:
        is_admin, _ = droits_par_groupes(u)
        db.execute("UPDATE user_sources SET last_is_admin=? WHERE username=?", (int(is_admin), u))
        db.commit()
        appliquer_quota_groupes(u)
    return membres


def synchroniser_groupes_ldap(groupes_par_login):
    """The background directory read's groups ({login: [names]}), applied to
    the directory accounts the portal knows (no local row): memberships,
    then admin right and quota when they changed."""
    db = get_db()
    comptes = [r['username'] for r in db.execute(
        "SELECT s.username FROM user_sources s LEFT JOIN local_users l ON l.username = s.username "
        "WHERE l.username IS NULL").fetchall()]
    for u in comptes:
        noms = groupes_par_login.get(u.lower())
        if noms is None:
            continue                  # not in the directory: nothing to infer
        avant = {r['group_name'] for r in db.execute(
            "SELECT group_name FROM user_directory_groups WHERE username=? AND source='ldap'", (u,))}
        if {n.lower() for n in avant} == {n.lower() for n in noms}:
            continue
        importer_groupes(u, {'ldap': noms})
        is_admin, _ = droits_par_groupes(u)
        db.execute("UPDATE user_sources SET last_is_admin=? WHERE username=?", (int(is_admin), u))
        db.commit()
        appliquer_quota_groupes(u)
