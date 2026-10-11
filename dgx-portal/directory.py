"""What the LDAP directory says about accounts the portal does not own.

LDAP and SSO accounts have no `local_users` row: the portal had no notion of
whether they were still active, and showed « — » in the Users list. This
module reads the directory with the service account — one paged search of all
its users, in the BACKGROUND, refreshed every two minutes — and the pages read
that snapshot.

States, as the directory exposes them:
  - Authentik's LDAP outpost: `ak-active` TRUE/FALSE;
  - Active Directory: `userAccountControl` bit 2 (ACCOUNTDISABLE);
  - 389-DS / FreeIPA: `nsAccountLock` TRUE;
  - OpenLDAP ppolicy: a `pwdAccountLockedTime` present.
An account the search does not return is « absent » (deleted, renamed). When
the directory cannot be asked (not configured, unreachable), the answer is
None — never a guess: missing data is not proof of absence.

Groups: `memberOf` DNs are reduced to their `cn` (« cn=platform-admins,ou=groups,… »
→ « platform-admins »), the same name a token's `groups` claim carries.
"""
import threading
import time

from ldap3 import NONE, SIMPLE, Connection, Server
from ldap3.utils.conv import escape_filter_chars

from instance_config import ldap_config

ACTIVE, DISABLED, ABSENT = 'actif', 'desactive', 'absent'

# A bind on Authentik's LDAP outpost alone costs ~3 s (measured 2026-10-11),
# a search ~0.7 s: the directory is NEVER asked on a request's path. One search
# of ALL its users is refreshed in the background, and pages read that.
_FRESH_S = 120
_state = {'t': 0.0, 'states': None, 'running': False}
_lock = threading.Lock()


def _connexion(c):
    if not (c['uri'] and c.get('bind_dn') and c.get('bind_pw')):
        return None
    # get_info=NONE: no schema download, so attributes a given directory does
    # not define (ak-active on AD, userAccountControl on Authentik) are not
    # rejected client-side.
    return Connection(Server(c['uri'], get_info=NONE, connect_timeout=5),
                      user=c['bind_dn'], password=c['bind_pw'],
                      authentication=SIMPLE, auto_bind=True, receive_timeout=15)


def _true(v):
    return str(v).strip().upper() in ('TRUE', '1', 'YES')


def entry_state(attrs):
    """ACTIF or DESACTIVE from one entry's attributes (a dict of lists)."""
    def first(name):
        v = attrs.get(name)
        if isinstance(v, (list, tuple)):
            return v[0] if v else None
        return v
    ak = first('ak-active')
    if ak is not None and not _true(ak):
        return DISABLED
    uac = first('userAccountControl')
    if uac is not None:
        try:
            if int(uac) & 2:
                return DISABLED
        except (TypeError, ValueError):
            pass
    if _true(first('nsAccountLock') or ''):
        return DISABLED
    if first('pwdAccountLockedTime'):
        return DISABLED
    return ACTIVE


def read_directory():
    """{'states': {login: ACTIF | DESACTIVE}, 'groups': {login: [group names]}}
    for every user of the directory (logins lowercase), or None when it cannot
    be asked. Slow (seconds): background only."""
    c = ldap_config()
    try:
        conn = _connexion(c)
    except Exception:
        return None
    if conn is None:
        return None
    attr = c['login_attr']
    states, groups = {}, {}
    try:
        for e in conn.extend.standard.paged_search(
                f"{c['users_dn']},{c['base']}", f'({escape_filter_chars(attr)}=*)',
                attributes=[attr, 'ak-active', 'userAccountControl',
                            'nsAccountLock', 'pwdAccountLockedTime', 'memberOf'],
                paged_size=500, generator=True):
            if e.get('type') != 'searchResEntry':
                continue
            d = e.get('attributes') or {}
            name = d.get(attr)
            name = name[0] if isinstance(name, (list, tuple)) and name else name
            if name:
                states[str(name).lower()] = entry_state(d)
                groups[str(name).lower()] = group_names(d.get('memberOf') or [])
    except Exception:
        return None
    finally:
        try:
            conn.unbind()
        except Exception:
            pass
    return {'states': states, 'groups': groups}


def _refresh(app):
    try:
        read_result = read_directory()
        with _lock:
            # A failed read keeps the previous answer (an outage must not turn
            # every account « absent »), but stops it from being refreshed
            # every call: retried after the same delay.
            if read_result is not None:
                _state['states'] = read_result['states']
            _state['t'] = time.time()
        if read_result is not None and app is not None:
            # The same read brings the LDAP groups down for the accounts the
            # portal knows, between logins too (a removal included).
            with app.app_context():
                from directory_groups import sync_ldap_groups
                sync_ldap_groups(read_result['groups'])
    except Exception as exc:                                     # noqa: BLE001
        print(f'[directory] synchronisation des groupes impossible : {exc}')
    finally:
        _state['running'] = False


def directory_states_for(usernames):
    """{username: ACTIF | DESACTIVE | ABSENT} for these accounts, from the last
    background read — None until one succeeded. Never waits on the directory:
    a stale read schedules a refresh and answers with what it has."""
    names = [u for u in usernames if u]
    with _lock:
        stale = time.time() - _state['t'] > _FRESH_S
        launch = stale and not _state['running']
        if launch:
            _state['running'] = True
        states = _state['states']
    if launch:
        # The read runs outside the request: it needs the app to write groups.
        try:
            from flask import current_app
            app = current_app._get_current_object()
        except RuntimeError:
            app = None
        # Short-lived (one read): unnamed, unlike the long-lived threads
        # test_regression_guards counts.
        threading.Thread(target=_refresh, args=(app,), daemon=True).start()
    if states is None:
        return None
    return {u: states.get(u.lower(), ABSENT) for u in names}


def group_names(values):
    """Group names from memberOf DNs or plain names (a token's claim)."""
    names = []
    for v in values or []:
        v = str(v).strip()
        if not v:
            continue
        if '=' in v.split(',')[0]:
            attr, _, val = v.split(',')[0].partition('=')
            if attr.strip().lower() != 'cn':
                continue
            v = val.strip()
        if v and v not in names:
            names.append(v)
    return names
