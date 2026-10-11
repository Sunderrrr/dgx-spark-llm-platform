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
from directory import group_names
from litellm_client import _ensure_litellm_user, litellm_update_user_budget
from instance_config import setting

SOURCES = ('ldap', 'sso')
_NAME_MAX = 80


def _group(db, name):
    return db.execute("SELECT * FROM user_groups WHERE lower(name)=lower(?)", (name,)).fetchone()


def import_groups(username, by_source):
    """Records this account's groups, per source ({'ldap': [...], 'sso': [...]},
    DNs or names), creating or merging the group rows. A source absent from
    `par_source` keeps its previous memberships (an SSO login does not erase
    what an LDAP login recorded, and vice versa)."""
    db = get_db()
    now = datetime.now().isoformat()
    for source, values in by_source.items():
        if source not in SOURCES or values is None:
            continue
        db.execute("DELETE FROM user_directory_groups WHERE username=? AND source=?",
                   (username, source))
        for name in group_names(values):
            name = name[:_NAME_MAX]
            g = _group(db, name)
            if g is None:
                db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at, sources) "
                           "VALUES (?,?,?,?,?)", (name, None, 0, now, source))
                canon = name
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


def groups_of(username):
    """The account's directory groups (merged: one row per group)."""
    return get_db().execute(
        "SELECT g.name, g.max_budget, g.is_admin, g.sources FROM user_groups g "
        "WHERE g.name IN (SELECT group_name FROM user_directory_groups WHERE username=?) "
        "ORDER BY g.name", (username,)).fetchall()


def rights_from_groups(username):
    """(is_admin, quota) the account's directory groups grant: admin if one of
    them is the directory's admin group or carries the admin right; the most
    generous quota set on them, else None."""
    groups = groups_of(username)
    admin_group = (setting('oidc_admin_group') or '').strip().lower()
    is_admin = any(g['is_admin'] or (admin_group and g['name'].lower() == admin_group)
                   for g in groups)
    quotas = [g['max_budget'] for g in groups if g['max_budget'] is not None]
    return is_admin, (max(quotas) if quotas else None)


def apply_group_quota(username):
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
        # expires_at is ISO text in UTC (admin_routes writes utcnow().isoformat()):
        # compared with a number, SQLite ranks any text above it — every grant,
        # expired or not, looked running.
        if db.execute("SELECT 1 FROM budget_grants WHERE username=? AND expires_at > ?",
                      (username, datetime.utcnow().isoformat())).fetchone():
            return None
        _, quota = rights_from_groups(username)
        previous = rs['budget_auto']
        default = float(get_setting('default_key_budget', KEY_BUDGET))
        if quota is not None:
            target = float(quota)
        elif previous is not None:
            target = default            # left its quota group(s): back to the default
        else:
            return None               # never had a group quota: untouched
        if previous is not None and float(previous) == target:
            return None               # already applied
        duration = get_setting('default_key_duration', KEY_DURATION)
        _ensure_litellm_user(username, target, duration)
        if not litellm_update_user_budget(username, target, budget_duration=duration):
            return None
        db.execute("UPDATE user_sources SET budget_auto=? WHERE username=?",
                   (target if quota is not None else None, username))
        db.commit()
        return target
    except Exception as exc:                                     # noqa: BLE001
        print(f'[directory_groups] quota de groupe non appliqué pour {username} : {exc}')
        return None


def recompute_members(group_name_):
    """After an admin changed a group: its directory members' admin right and
    quota follow at once, not at their next login."""
    db = get_db()
    members = [r['username'] for r in db.execute(
        "SELECT DISTINCT d.username FROM user_directory_groups d "
        "LEFT JOIN local_users l ON l.username = d.username "
        "WHERE lower(d.group_name)=lower(?) AND l.username IS NULL", (group_name_,)).fetchall()]
    for u in members:
        is_admin, _ = rights_from_groups(u)
        db.execute("UPDATE user_sources SET last_is_admin=? WHERE username=?", (int(is_admin), u))
        db.commit()
        apply_group_quota(u)
    return members


def sync_ldap_groups(groups_by_login):
    """The background directory read's groups ({login: [names]}), applied to
    the directory accounts the portal knows (no local row): memberships,
    then admin right and quota when they changed."""
    db = get_db()
    accounts = [r['username'] for r in db.execute(
        "SELECT s.username FROM user_sources s LEFT JOIN local_users l ON l.username = s.username "
        "WHERE l.username IS NULL").fetchall()]
    for u in accounts:
        names = groups_by_login.get(u.lower())
        if names is None:
            continue                  # not in the directory: nothing to infer
        before = {r['group_name'] for r in db.execute(
            "SELECT group_name FROM user_directory_groups WHERE username=? AND source='ldap'", (u,))}
        if {n.lower() for n in before} == {n.lower() for n in names}:
            continue
        import_groups(u, {'ldap': names})
        is_admin, _ = rights_from_groups(u)
        db.execute("UPDATE user_sources SET last_is_admin=? WHERE username=?", (int(is_admin), u))
        db.commit()
        apply_group_quota(u)
