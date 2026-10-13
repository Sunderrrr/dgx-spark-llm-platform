"""Admin — accounts: local users, groups, sessions, blocking, deletion and
purge, the per-account detail, the audit log, and token budgets (requests,
grants and their expiry).

Routes of the `admin` blueprint (defined in admin_routes.py): the endpoint
names stay `admin.*` whatever module holds them.
"""
import re
from datetime import datetime, timedelta

from flask import (jsonify, request,
                   session)
from werkzeug.security import generate_password_hash

from auth import (USERNAME_RE,
                  _revoke_user_sessions,
                  admin_required, block_account, complete_session_origin,
                  unblock_account, is_blocked)
from config import KEY_BUDGET, KEY_DURATION
from db import (add_notification, get_db, get_setting, log_audit)
from litellm_client import (_litellm_user_info, get_user_keys, litellm_update_user_budget)
from local_users import (_local_group, _local_user_effective_budget,
                         _local_user_is_admin, _parse_budget,
                         _sync_local_user_budget, password_policy_error)
from user_lifecycle import (count_data, deprovision_account,
                            notify_password_changed, revoke_account_keys)
from directory import directory_states_for
from directory_groups import recompute_members
from owner import owner
from stats import (_account_activity, admin_get_user_consumption)

from admin_routes import _json_error, _json_ok, _owner_protected, bp

# ── Local user management (admin) ───────────────────────────────────────────

@bp.route('/api/admin/users')
@admin_required
def api_admin_users():
    """UNIFIED view of all known accounts, with their source(s):
      - local  : account managed here (local_users table, edit actions)
      - ldap   : has already logged in via LDAP
      - sso    : has already logged in via SSO/Authentik
    An account can carry several sources at once (e.g. ldap + sso). Accounts
    that have used the platform (LiteLLM keys/budget) but whose login we haven't
    yet observed since this addition appear as "external".
    """
    db = get_db()
    managed = {u['username']: u for u in db.execute("SELECT * FROM local_users").fetchall()}
    recorded = {r['username']: r for r in db.execute("SELECT * FROM user_sources").fetchall()}
    spend = {s['username']: s for s in (admin_get_user_consumption() or [])}
    blocked = {b['username']: b for b in db.execute("SELECT * FROM blocked_users").fetchall()}
    # Chosen avatar (NULL = avatar generated from the nickname): the admin saw
    # initials where the person concerned sees their avatar.
    avatars = {a['username']: a['avatar_id'] for a in
               db.execute("SELECT username, avatar_id FROM user_prefs").fetchall()}
    # Anti-brute-force lockout in progress: the key is composite
    # ('ip|compte' or 'user:compte'), we attach it to the account by the suffix.
    locked = {}
    now = datetime.now().timestamp()
    for r in db.execute("SELECT key, locked_until FROM login_attempts WHERE locked_until > ?",
                        (now,)).fetchall():
        account = r['key'].split('|')[-1].removeprefix('user:')
        locked[account] = max(locked.get(account, 0), int((r['locked_until'] - now) // 60) + 1)

    # The groups table is small and was ALREADY read after the loop
    # (for the response): we load it here and pass it to the helpers, which
    # removes the 2 SELECTs per account of `_local_group`.
    groups = db.execute("SELECT name, max_budget, is_admin, sources FROM user_groups "
                        "ORDER BY name").fetchall()
    groups_by_name = {g['name']: g for g in groups}
    # Directory groups (LDAP memberOf, SSO claim), merged: one name per group.
    directory_groups_by_user = {}
    for r in db.execute("SELECT DISTINCT d.username, d.group_name FROM user_directory_groups d "
                        "JOIN user_groups g ON g.name = d.group_name ORDER BY d.group_name").fetchall():
        directory_groups_by_user.setdefault(r['username'], []).append(r['group_name'])
    names = set(managed) | set(recorded) | set(spend)
    # Accounts the portal does not own (LDAP, SSO): their state lives in the
    # directory. One search for all of them, cached a minute; None when the
    # directory cannot be asked (the UI then shows nothing, it never guesses).
    directory_states = directory_states_for([n for n in names if n not in managed])
    owner_name = owner()
    out = []
    for name in sorted(names):
        srcs = set()
        if name in managed:
            srcs.add('local')
        if name in recorded:
            srcs |= {s for s in (recorded[name]['sources'] or '').split(',') if s}
        # Used the platform but no source observed → external (LDAP/SSO).
        if not srcs and name in spend:
            srcs.add('external')
        local_row = managed.get(name)
        fullname = (local_row['fullname'] if local_row else None) or (recorded[name]['fullname'] if name in recorded else None)
        sp = spend.get(name)
        rs = recorded.get(name)
        last_source = rs['last_source'] if rs else None
        last_is_admin = rs['last_is_admin'] if rs else None
        local_admin = _local_user_is_admin(local_row, groups_by_name) if local_row else None
        # EFFECTIVE role, as it is actually APPLIED to requests. A single
        # authority, and it is the portal: as soon as a `local_users` row
        # exists, `auth.etat_compte` rules on it alone (`_local_user_is_admin`)
        # and rewrites `session['is_admin']` — whatever `last_source`. The Users
        # page claimed the opposite (« the directory wins »): a local admin account
        # removed from `cn=<groupe-admin>` stayed admin in practice, with no signal
        # for the operator who had just done it. So we display what applies, and
        # the comment says the same thing as auth.py.
        if local_row is not None:
            effective_admin = local_admin
            role_source = 'local'
        else:
            # External account: the directory is the only source of rights.
            effective_admin = bool(last_is_admin) if last_is_admin is not None else None
            role_source = last_source if last_source in ('sso', 'ldap') else 'external'
        # Management origin for the "Géré" column: an account that has logged in
        # via the directory (SSO/LDAP) is effectively managed by Authentik, even
        # if a historical local_users row still exists (edit actions remain
        # available because the row exists, but its rights/budget are delegated).
        # A directory account stays « managed by the directory » even if a
        # historical local row remains; without a local row, the directory is
        # the only possible authority.
        managed_by = 'repertoire' if (local_row is None or last_source in ('sso', 'ldap')) else 'local'
        if name == owner_name:
            effective_admin = True       # the owner is always an admin (auth.etat_compte)
        out.append({
            'username': name,
            'fullname': fullname,
            'avatar_id': avatars.get(name),
            'sources': sorted(srcs),
            'managed': bool(local_row),
            'managed_by': managed_by,
            'id': local_row['id'] if local_row else None,
            'group_name': local_row['group_name'] if local_row else None,
            'enabled': local_row['enabled'] if local_row else 1,
            'is_admin': local_row['is_admin'] if local_row else None,
            'effective_admin': effective_admin,
            'role_source': role_source,
            'last_source': last_source,
            'effective_budget': (_local_user_effective_budget(local_row, groups_by_name) if local_row
                                 else (sp['max_budget'] if sp else None)),
            'unlimited': (sp['unlimited'] if sp else False),
            'spend': (sp['spend'] if sp else 0),
            'key_count': (sp['key_count'] if sp else 0),
            'last_seen': recorded[name]['last_seen'] if name in recorded else None,
            # Refusal and lockout state: without them, the page cannot
            # distinguish a blocked account from a merely inactive one — yet
            # that is exactly what an admin comes to check.
            'blocked': blocked.get(name) is not None,
            'block_reason': (blocked[name]['reason'] if name in blocked else None),
            'blocked_at': (blocked[name]['blocked_at'] if name in blocked else None),
            'locked_minutes': locked.get(name, 0),
            'directory_status': (directory_states or {}).get(name) if local_row is None else None,
            'directory_groups': directory_groups_by_user.get(name, []),
            'owner': name == owner_name,
        })
    # Members per group: local accounts (their one group) + directory accounts.
    members = {}
    for u in out:
        for g in set(([u['group_name']] if u['group_name'] else []) + u['directory_groups']):
            members[g] = members.get(g, 0) + 1
    return jsonify({'users': out,
                    'groups': [{**dict(g), 'sources': [x for x in (g['sources'] or '').split(',') if x],
                                'members': members.get(g['name'], 0)} for g in groups],
                    'default_budget': float(get_setting('default_key_budget', KEY_BUDGET)),
                    # The quota period (« 7d »): every cap of this page is per period,
                    # not per day as the UI used to say.
                    'budget_duration': get_setting('default_key_duration', KEY_DURATION)})

@bp.route('/admin/audit')
@admin_required
def admin_audit():
    """Audit log (admin): the latest sensitive events, filterable
    by user (?username=). Reverse-chronological read, capped."""
    username = (request.args.get('username') or '').strip()
    db = get_db()
    if username:
        rows = db.execute(
            "SELECT id, username, action, detail, created_at FROM audit_log WHERE username=? "
            "ORDER BY id DESC LIMIT 100", (username,)).fetchall()
    else:
        rows = db.execute(
            "SELECT id, username, action, detail, created_at FROM audit_log "
            "ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/admin/users/create', methods=['POST'])
@admin_required
def admin_users_create():
    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '')
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Identifiant invalide (a-z, 0-9, . _ - , max 64)."}), 400
    pw_err = password_policy_error(password, username)
    if pw_err:
        return jsonify({'ok': False, 'error': pw_err}), 400
    db = get_db()
    if db.execute("SELECT 1 FROM local_users WHERE username=?", (username,)).fetchone():
        return jsonify({'ok': False, 'error': "Cet utilisateur existe déjà."}), 409
    group = (request.form.get('group', '').strip() or None)
    if group and not _local_group(group):
        return jsonify({'ok': False, 'error': "Groupe inconnu."}), 400
    budget, err = _parse_budget(request.form.get('max_budget'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    is_admin = request.form.get('is_admin') in ('1', 'true', 'on')
    fullname = request.form.get('fullname', '').strip()[:120] or None
    db.execute(
        "INSERT INTO local_users (username, password_hash, fullname, is_admin, group_name, max_budget, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,1,?)",
        (username, generate_password_hash(password), fullname, int(is_admin), group, budget,
         datetime.now().isoformat()))
    db.commit()
    row = db.execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    quota_ok = _sync_local_user_budget(username, row)
    log_audit(session.get('username'), 'user.create',
              f"création de {username}" + (f" (groupe {group})" if group else "")
              + ('' if quota_ok else ' — QUOTA NON APPLIQUÉ'))
    # A quota that could not be written on the LiteLLM side is a de-facto
    # unlimited account: say so, instead of letting a complete creation be believed.
    return jsonify({'ok': True} if quota_ok else {
        'ok': True,
        'warning': "Compte créé, mais son quota n'a PAS pu être appliqué sur LiteLLM : "
                   "le compte est sans plafond tant que le budget n'est pas redéfini."})

@bp.route('/admin/users/update/<int:uid>', methods=['POST'])
@admin_required
def admin_users_update(uid):
    db = get_db()
    row = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': "Utilisateur introuvable."}), 404
    refusal = _owner_protected(row['username'])
    if refusal:
        return refusal
    sets, vals = [], []
    password = request.form.get('password', '')
    if password:
        pw_err = password_policy_error(password, row['username'])
        if pw_err:
            return jsonify({'ok': False, 'error': pw_err}), 400
        sets.append("password_hash=?"); vals.append(generate_password_hash(password))
    if 'group' in request.form:
        group = request.form.get('group', '').strip() or None
        if group and not _local_group(group):
            return jsonify({'ok': False, 'error': "Groupe inconnu."}), 400
        # Admin rights come from `local_users.is_admin` OR the group
        # (`_local_user_is_admin`). Changing GROUP is therefore a rights removal
        # like any other, and this branch was the only one of the three not to
        # go through the guard: an admin holding their rights from their group
        # could send it empty and lose the administration (audit of
        # 2026-10-02). We simulate the state AFTER the change, not just
        # `is_admin`.
        new_group = _local_group(group) if group else None
        if not (row['is_admin'] or (new_group and new_group['is_admin'])):
            refusal = _admin_removal_refusal(row, "retirer du groupe administrateur")
            if refusal:
                return _json_error(refusal, 409)
        sets.append("group_name=?"); vals.append(group)
    if 'max_budget' in request.form:
        budget, err = _parse_budget(request.form.get('max_budget'))
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        sets.append("max_budget=?"); vals.append(budget)
    if 'is_admin' in request.form:
        new_admin = int(request.form.get('is_admin') in ('1', 'true', 'on'))
        if not new_admin:
            refusal = _admin_removal_refusal(row, "rétrograder")
            if refusal:
                return _json_error(refusal, 409)
        sets.append("is_admin=?"); vals.append(new_admin)
    if 'enabled' in request.form:
        new_active = int(request.form.get('enabled') in ('1', 'true', 'on'))
        if not new_active:
            refusal = _admin_removal_refusal(row, "désactiver")
            if refusal:
                return _json_error(refusal, 409)
        sets.append("enabled=?"); vals.append(new_active)
    if 'fullname' in request.form:
        sets.append("fullname=?"); vals.append(request.form.get('fullname', '').strip()[:120] or None)
    if sets:
        db.execute(f"UPDATE local_users SET {', '.join(sets)} WHERE id=?", (*vals, uid))
        db.commit()
    updated = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    # Locking an account (enabled=0) immediately revokes its sessions:
    # it loses its access without waiting for HTTP expiration.
    revoked = 0
    keys_failed = 0
    if 'enabled' in request.form and not updated['enabled']:
        revoked = _revoke_user_sessions(updated['username'])
        # Disabling cuts PORTAL access, not API access: LiteLLM validates the
        # keys itself and the portal is not in the path. Without this
        # revocation, a « désactivé » account kept a working key
        # (audit of 2026-10-02) — the same hole as blocking, on the other
        # offboarding path.
        keys_ok, keys_failed, keys_total = revoke_account_keys(
            updated['username'], session.get('username'), 'désactivation du compte')
    if password:
        # Changing the password must close the OTHER sessions: without that,
        # a change motivated by doubt about a stolen cookie protected
        # nothing, the old session continuing to validate.
        revoked = max(revoked, _revoke_user_sessions(updated['username']))
    # The quota resync was UNCONDITIONAL: renaming an account, changing its
    # password or fixing its name rewrote the LiteLLM envelope from
    # local_users — which ERASED a grant in progress, since budget_grants only
    # lives on the LiteLLM side. The reaper then noticed the drift, concluded
    # « the admin stepped in meanwhile » and deleted the row: the account
    # lost its quota with nobody being warned.
    reloaded = request.form.get('enabled') in ('1', 'true', 'on')
    if {'group', 'max_budget'} & set(request.form.keys()) or reloaded:
        quota_ok = _sync_local_user_budget(updated['username'], updated)
    else:
        quota_ok = True
    log_audit(session.get('username'), 'user.update',
              f"mise à jour de {updated['username']}"
              + (f" — mot de passe changé par l'admin, {revoked} session(s) fermée(s)"
                 if password else '')
              + (f" — compte désactivé, {revoked} session(s) fermée(s)"
                 if 'enabled' in request.form and not updated['enabled'] else '')
              + (f" — {keys_failed} clé(s) API NON révoquée(s)" if keys_failed else '')
              + ('' if quota_ok else ' — QUOTA NON APPLIQUÉ'))
    if password:
        notify_password_changed(updated['username'], by_admin=session.get('username'))
    warnings = []
    if not quota_ok:
        warnings.append("le quota n'a PAS pu être appliqué sur LiteLLM : "
                              "redéfinis le budget du compte")
    if keys_failed:
        warnings.append(f"{keys_failed} clé(s) API n'ont PAS pu être révoquées : "
                              "vérifie côté LiteLLM avant de considérer ce compte comme parti")
    if warnings:
        return jsonify({'ok': True,
                        'warning': "Modifications enregistrées, mais " + " ; ".join(warnings) + "."})
    return jsonify({'ok': True})


@bp.route('/admin/users/<username>/revoke-sessions', methods=['POST'])
@admin_required
def admin_revoke_sessions(username):
    """Revokes at will all active sessions of an account (even a
    stolen cookie becomes unusable immediately)."""
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    n = _revoke_user_sessions(username)
    # A revocation is a security action: it is logged like the others.
    # It was not, only disabling was.
    log_audit(session.get('username'), 'session.revoke',
              f"{username} — {n} session(s) révoquée(s)")
    return jsonify({'ok': True, 'revoked': n})

def _body():
    """Request body, form OR JSON.

    The Admin sends form data (postForm) and the new UI JSON (fetch): reading
    only request.form silently ignored a `confirm=DELETE` that was indeed sent,
    and the route answered 409 forever.
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form


def _last_local_admin(username):
    """True if `username` is the last active LOCAL administrator.

    The portal would then have nobody left to administer it locally —
    the directory admins (LDAP/SSO) depend on an external directory, which
    is no reason to cut off one's own hand.

    A BLOCKED account does not count (audit of 2026-10-02): it is refused at
    login even with the right password, so it can no longer administer anything.
    Without this exclusion, blocking the last two local admins one after the
    other passed — each saw the other, still `enabled=1`, as a recourse — and
    the platform ended up with no reachable local administrator. The `enabled=0`
    path, in contrast, already filtered itself out.
    """
    db = get_db()
    for r in db.execute(
            "SELECT * FROM local_users WHERE enabled=1 "
            "AND username NOT IN (SELECT username FROM blocked_users)").fetchall():
        if r['username'] != username and _local_user_is_admin(r):
            return False
    return True


def _local_admins_after(simulation):
    """Counts the active local admins AFTER a simulated removal.

    `simulation` = {username: (is_admin, group_name)} of the accounts whose
    state changes. Needed for the case `_dernier_admin_local` cannot see:
    a group where TWO members hold their admin rights. Taken one by one, each
    sees the other as admin and concludes they may leave; removing both of
    them leaves none.
    """
    db = get_db()
    n = 0
    for r in db.execute(
            "SELECT * FROM local_users WHERE enabled=1 "
            "AND username NOT IN (SELECT username FROM blocked_users)").fetchall():
        state = simulation.get(r['username'])
        if state is None:
            if _local_user_is_admin(r):
                n += 1
            continue
        is_admin, group_name = state
        if is_admin:
            n += 1
            continue
        g = _local_group(group_name) if group_name else None
        if g and g['is_admin']:
            n += 1
    return n


def _admin_removal_refusal(row, action):
    """Reason for refusal when removing the admin rights of `row` is forbidden.

    Two cases, same reason: the portal would become locally unadministrable.
    - disabling or demoting ONESELF escapes any repair: no administration
      session is left at all;
    - removing the LAST local admin leaves the platform to the directory
      admins alone, who depend on an external directory.

    Deletion and blocking have this guard; MODIFICATION did not, although a
    single POST sufficed — and the revalidation of the account state on every
    request makes the loss of access immediate (no need to wait for the cookie
    to expire).
    """
    if not _local_user_is_admin(row):
        return None
    if row['username'] == session.get('username'):
        return (f"Tu ne peux pas te {action} toi-même : tu perdrais immédiatement "
                f"l'accès à l'administration. Demande-le à un autre administrateur.")
    if _local_admins_after({row['username']: (0, None)}) == 0:
        return (f"{row['username']} est le dernier administrateur local actif : le "
                f"portail n'aurait plus personne pour l'administrer en local.")
    return None


@bp.route('/admin/users/delete/<int:uid>', methods=['POST'])
@admin_required
def admin_users_delete(uid):
    """Deletes an account: removes ALL its access, then purges its data.

    Before 2026-09-13 this route only did a DELETE in local_users:
    the account's API keys stayed valid (LiteLLM validates them itself) and
    its browser session survived up to 12 h. Three guards were
    added with the rest:
    - no self-deletion (the admin would cut their own access mid-action);
    - no deletion of the last local administrator;
    - `confirm=DELETE` required: the operation takes away personal data
      (memory, conversations, shares, preferences) and must not start from
      an accidental POST.
    """
    db = get_db()
    row = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': "Utilisateur introuvable."}), 404
    username = row['username']
    me = session.get('username')
    if username == me:
        return jsonify({'ok': False,
                        'error': "Tu ne peux pas supprimer ton propre compte."}), 400
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    if _local_user_is_admin(row) and _last_local_admin(username):
        return jsonify({'ok': False,
                        'error': "Dernier administrateur local : nomme un autre "
                                 "administrateur avant de supprimer celui-ci."}), 400
    data_counts = count_data(username)
    if (_body().get('confirm') or '').strip().upper() != 'DELETE':
        return jsonify({'ok': False, 'needs_confirm': True, 'data': data_counts,
                        'error': "Confirmation requise : cette suppression emporte "
                                 "les accès ET les données du compte."}), 409
    # The local row first: it carries the access, and everything after it
    # concerns it (keys, envelope, data) without depending on it.
    db.execute("DELETE FROM local_users WHERE id=?", (uid,))
    db.commit()
    report = deprovision_account(username, me)
    response = {'ok': True, 'purged': report}
    # API access is half the problem: if LiteLLM did not answer, some
    # keys may still work, and the admin must know it right away
    # rather than discovering it in the audit log.
    if report['keys_failed'] or not report['litellm_user']:
        response['warning'] = (
            f"{report['keys_failed']} clé(s) n'ont pas pu être révoquées et/ou "
            "l'enveloppe LiteLLM n'a pas pu être supprimée (service injoignable) : "
            "vérifie les clés de ce compte dans LiteLLM avant de le considérer comme parti.")
    return jsonify(response)


@bp.route('/admin/users/<username>/purge', methods=['POST'])
@admin_required
def admin_user_purge(username):
    """Erases the DATA of an account without a local row (directory account).

    An LDAP/SSO account does not exist in `local_users`: the deletion route,
    which starts from a local id, can therefore do nothing for it — and its
    conversations, memory, preferences and keys stayed in the database
    indefinitely after it left. This route erases them, without touching its
    access: BLOCKING remains the offboarding lever, since a directory
    account can log back in and would then find an empty account.
    """
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    if username == session.get('username'):
        return jsonify({'ok': False,
                        'error': "Tu ne peux pas purger ton propre compte."}), 400
    db = get_db()
    if db.execute("SELECT 1 FROM local_users WHERE username=?", (username,)).fetchone():
        return jsonify({'ok': False,
                        'error': "Ce compte est local : utilise la suppression, "
                                 "qui retire aussi l'accès."}), 400
    if (_body().get('confirm') or '').strip().upper() != 'DELETE':
        return jsonify({'ok': False, 'needs_confirm': True,
                        'data': count_data(username),
                        'error': "Confirmation requise : cette opération efface "
                                 "définitivement les données du compte."}), 409
    report = deprovision_account(username, session.get('username'),
                                    action='user.purge')
    return jsonify({'ok': True, 'purged': report})


@bp.route('/admin/users/<username>/block', methods=['POST'])
@admin_required
def admin_user_block(username):
    """Refuses an account at login, whatever its source.

    This is the portal's ONLY lever for an LDAP/SSO account: it has no
    row in local_users, hence no `enabled` to flip, and revoking its
    sessions was pointless — it logged right back in. Blocking is
    reversible and does not touch its local state: unblocking restores the
    account exactly as it was.
    """
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    if username == session.get('username'):
        return jsonify({'ok': False, 'error': "Tu ne peux pas bloquer ton propre compte."}), 400
    if is_blocked(username):
        return jsonify({'ok': True, 'revoked_sessions': 0, 'already_blocked': True})
    row = get_db().execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    if row is not None and _local_user_is_admin(row) and _last_local_admin(username):
        return jsonify({'ok': False,
                        'error': "Dernier administrateur local : nomme un autre "
                                 "administrateur avant de bloquer celui-ci."}), 400
    reason = (_body().get('reason') or '').strip()[:200] or None
    report = block_account(username, reason, session.get('username'))
    response = {'ok': True,
               'revoked_sessions': report['sessions'],
               'revoked_keys': report['keys_revoked'],
               'keys_total': report['keys']}
    # A revocation failure must NEVER be silent: a key that LiteLLM refused
    # to remove remains a live access to the GPU, and the admin must know it
    # right away (audit of 2026-10-02).
    if report['keys_failed']:
        response['warning'] = (f"{report['keys_failed']} clé(s) API n'ont PAS pu être "
                              f"révoquées — vérifie côté LiteLLM avant de considérer "
                              f"ce compte comme parti.")
    return jsonify(response)


@bp.route('/admin/users/<username>/unblock', methods=['POST'])
@admin_required
def admin_user_unblock(username):
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    n = unblock_account(username, session.get('username'))
    return jsonify({'ok': True, 'unblocked': bool(n)})


@bp.route('/admin/users/<username>/detail')
@admin_required
def admin_user_detail(username):
    """Everything the portal knows about an account, in one response.

    The Users page lists; this view answers the questions actually asked in
    front of an account: what does it consume, does it have keys, how many
    memories, where did it log in from, and what has been done to it. Key
    values NEVER leave here: only their alias is returned.
    """
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    db = get_db()
    local_row = db.execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    rs = db.execute("SELECT * FROM user_sources WHERE username=?", (username,)).fetchone()
    bl = db.execute("SELECT * FROM blocked_users WHERE username=?", (username,)).fetchone()
    litellm = _litellm_user_info(username)
    data_counts = count_data(username)
    keys = []
    for k in (get_user_keys(username) or []):
        keys.append({'alias': k.get('key_alias'), 'created_at': k.get('created_at'),
                     'spend': k.get('spend', 0)})
    # The row of the calling sid is its own: if the admin opens their own
    # account, it is filled in like in self-service (session opened before
    # the IP/user-agent columns were added). That of ANOTHER account is never
    # touched — one would not write the admin's IP into someone else's session.
    complete_session_origin()
    current_sid = session.get('sid')
    now = datetime.now().timestamp()
    sessions = []
    for r in db.execute(
            "SELECT sid, auth_at, created_at, expires_at, ip, user_agent FROM user_sessions "
            "WHERE username=? AND revoked=0 AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT 20", (username, now)).fetchall():
        sessions.append({
            # Never the full sid: it is the session cookie's secret.
            'id': (r['sid'] or '')[:12],
            'created_at': r['created_at'], 'expires_at': r['expires_at'],
            'ip': r['ip'], 'user_agent': r['user_agent'],
            'current': bool(current_sid and r['sid'] == current_sid),
        })
    # Actions PERFORMED by this account (audit_log.username is the actor, the
    # target is in `detail`): this is the useful reading for an admin account.
    audit = [{'action': r['action'], 'detail': r['detail'], 'at': r['created_at']}
             for r in db.execute(
                 "SELECT action, detail, created_at FROM audit_log WHERE username=? "
                 "ORDER BY id DESC LIMIT 20", (username,)).fetchall()]
    last_source = rs['last_source'] if rs else None
    if local_row is not None:
        # Same single authority as the list (and as `auth.etat_compte`): the
        # local row rules, the directory is not added to it.
        role = 'admin' if _local_user_is_admin(local_row) else 'user'
    else:
        role = 'admin' if (rs and rs['last_is_admin']) else 'user'
    return jsonify({
        'ok': True,
        'username': username,
        'fullname': (local_row['fullname'] if local_row else None) or (rs['fullname'] if rs else None),
        'sources': (rs['sources'] if rs else '') or '',
        'last_source': last_source,
        'last_seen': rs['last_seen'] if rs else None,
        'local': local_row is not None,
        'enabled': bool(local_row['enabled']) if local_row is not None else True,
        'group': local_row['group_name'] if local_row else None,
        'max_budget': local_row['max_budget'] if local_row else None,
        'effective_budget': _local_user_effective_budget(local_row) if local_row else litellm['max_budget'],
        'role': role,
        'blocked': {'blocked': bl is not None,
                    'reason': bl['reason'] if bl else None,
                    'at': bl['blocked_at'] if bl else None,
                    'by': bl['blocked_by'] if bl else None},
        'litellm': {'exists': bool(litellm.get('exists')),
                    'max_budget': litellm.get('max_budget'),
                    'spend': litellm.get('spend', 0),
                    'budget_duration': litellm.get('budget_reset_at') or None},
        'keys': keys,
        'memory_facts': data_counts['memory_facts'],
        'conversations': data_counts['conversations'],
        'sessions': sessions,
        'audit': audit,
        # The same 6-month daily series as « Mon compte »: which days the
        # account was actually active, whatever its source (local, LDAP, SSO).
        'activity': _account_activity(username),
        # Media usage and model requests: what the « Rechercher un utilisateur »
        # card of the Users tab showed — now in the one place an admin opens.
        'media': {k: dict(zip(('count', 'last'), db.execute(
            f"SELECT COUNT(*), MAX(created_at) FROM {t} WHERE username=?", (username,)).fetchone()))
                  for k, t in (('image', 'image_jobs'),
                               ('video', 'video_jobs'), ('voice', 'voice_jobs'),
                               ('ocr', 'ocr_jobs'))},
        'model_requests': [dict(r) for r in db.execute(
            "SELECT model_id, status, created_at FROM model_requests WHERE username=? "
            "ORDER BY created_at DESC LIMIT 10", (username,)).fetchall()],
        'directory_status': ((directory_states_for([username]) or {}).get(username)
                             if local_row is None else None),
        'data': data_counts,
    })

@bp.route('/admin/groups/create', methods=['POST'])
@admin_required
def admin_groups_create():
    name = request.form.get('name', '').strip()
    # 80: directory groups come down with their own names (« authentik Admins »).
    if not re.match(r'^[\w .-]{1,80}$', name):
        return jsonify({'ok': False, 'error': "Nom de groupe invalide (max 80)."}), 400
    budget, err = _parse_budget(request.form.get('max_budget'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    is_admin = request.form.get('is_admin') in ('1', 'true', 'on')
    db = get_db()
    # Groups merge by name whatever the case (an LDAP « Famille » and an SSO
    # « famille » are one group): editing goes to the existing row.
    existing = db.execute("SELECT name FROM user_groups WHERE lower(name)=lower(?)", (name,)).fetchone()
    if existing:
        name = existing['name']
    # Symmetric case of deletion: editing an existing group to REMOVE its
    # admin right (upsert ON CONFLICT) makes its members lose their rights —
    # without going through deletion. Same guard.
    old = _local_group(name)
    if old and old['is_admin'] and not is_admin:
        members = db.execute("SELECT * FROM local_users WHERE group_name=?", (name,)).fetchall()
        if any(_local_user_is_admin(m) for m in members) and \
                _local_admins_after({m['username']: (0, None) for m in members}) == 0:
            return _json_error(
                f"Retirer le droit d'administration du groupe {name} laisserait le "
                f"portail sans aucun administrateur local. Ajoute un autre "
                f"administrateur local d'abord.", 409)
    db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) VALUES (?,?,?,?) "
               "ON CONFLICT(name) DO UPDATE SET max_budget=excluded.max_budget, is_admin=excluded.is_admin",
               (name, budget, int(is_admin), datetime.now().isoformat()))
    db.commit()
    # Propagates the group's new quota to its members (who have no override).
    for u in db.execute("SELECT * FROM local_users WHERE group_name=? AND max_budget IS NULL", (name,)):
        _sync_local_user_budget(u['username'], u)
    # ...and to its directory members (LDAP/SSO), admin right included.
    recompute_members(name)
    log_audit(session.get('username'), 'group.create', f"groupe {name}")
    return jsonify({'ok': True})

@bp.route('/admin/groups/delete/<name>', methods=['POST'])
@admin_required
def admin_groups_delete(name):
    db = get_db()
    members = db.execute("SELECT * FROM local_users WHERE group_name=?", (name,)).fetchall()
    # A group can CARRY the admin rights of its members
    # (user_groups.is_admin): deleting it can therefore leave the portal with
    # no local administrator at all, exactly like demoting the last one. The
    # group deletion checked nothing.
    group = _local_group(name)
    if group and group['is_admin'] and any(_local_user_is_admin(m) for m in members):
        if _local_admins_after({m['username']: (0, None) for m in members}) == 0:
            return _json_error(
                f"Le groupe {name} porte les droits d'administration de ses membres et "
                f"aucun administrateur local ne subsisterait après sa suppression. "
                f"Ajoute un autre administrateur local d'abord.", 409)
    db.execute("UPDATE local_users SET group_name=NULL WHERE group_name=?", (name,))
    db.execute("DELETE FROM user_groups WHERE name=?", (name,))
    db.commit()
    # Directory members lose what the group granted, at once. (A directory
    # group comes back at its members' next login, without quota or right.)
    recompute_members(name)
    # The members lose the group's quota: their NEW effective cap
    # (personal override, else global default) must be rewritten on the LiteLLM
    # side. Group creation did it, deletion did not — the members therefore
    # silently kept an envelope sometimes more generous than the default, that
    # is, a phantom quota.
    failures = []
    for m in members:
        if m['max_budget'] is not None:
            continue                     # personal cap: nothing to propagate
        fresh = db.execute("SELECT * FROM local_users WHERE username=?",
                           (m['username'],)).fetchone()
        if fresh and not _sync_local_user_budget(m['username'], fresh):
            failures.append(m['username'])
    log_audit(session.get('username'), 'group.delete',
              f"suppression du groupe {name} — {len(members)} membre(s) rebasculé(s) "
              f"sur le défaut" + (f", QUOTA NON APPLIQUÉ pour {', '.join(failures)}" if failures else ""))
    return jsonify({'ok': True, 'members': len(members), 'quota_failures': failures})


@bp.route('/admin/budget/approve/<int:req_id>', methods=['POST'])
@admin_required
def approve_budget(req_id):
    amount = request.form.get('amount', '').strip()
    db = get_db()
    breq = db.execute("SELECT * FROM budget_requests WHERE id=?", (req_id,)).fetchone()
    if not breq or breq['status'] != 'pending':
        return _json_error("Demande introuvable ou déjà traitée.", 404)
    try:
        amount_val = float(amount)
        if amount_val <= 0:
            raise ValueError
    except ValueError:
        return _json_error("Le montant à ajouter doit être un nombre positif.")
    if amount_val > 1_000_000_000_000:
        # Guardrail: an absurd amount (typo « 6666726666666 ») makes the
        # account unlimited de facto — it happened on this server without
        # anyone seeing it. Beyond 1e12 tokens, it is a typing
        # error, we refuse.
        return _json_error("Montant irréaliste (> 1e12 tokens) — vérifie la saisie.")
    # Budget at the ACCOUNT level: we increment the LiteLLM user's envelope.
    info = _litellm_user_info(breq['username'])
    current_budget = info.get('max_budget') or 0
    # The reset window always accompanies the update: without it the amount
    # becomes a lifelong cap, never reset to zero. We align on the global
    # default (weekly since 2026-09-08).
    grant_duration = get_setting('default_key_duration', KEY_DURATION)
    # « durée »: empty = permanent (the old behaviour); N days = the extra is
    # NOT a new lifelong limit — a reaper brings the account back to its base
    # cap at the deadline.
    grant = db.execute(
        "SELECT * FROM budget_grants WHERE username=? AND expires_at > ?",
        (breq['username'], datetime.utcnow().isoformat())).fetchone()
    duration = request.form.get('grant_days', '').strip()
    expires_at = None
    if duration:
        try:
            days = int(duration)
            if not 1 <= days <= 365:
                raise ValueError
        except ValueError:
            return _json_error("Durée de la subvention : nombre de jours entre 1 et 365 "
                                "(vide = permanent).")
        expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat()
    if expires_at:
        # TEMPORARY grant: the base cap is frozen ONCE (at the first
        # boost); a 2nd boost during the period stacks on top of it,
        # the deadline still returns to the SAME base cap.
        base = grant['base_budget'] if grant else current_budget
        new_budget = (grant['current_budget'] if grant else base) + amount_val
        if not litellm_update_user_budget(breq['username'], new_budget,
                                          budget_duration=grant_duration):
            return _json_error("Erreur lors de la mise à jour du budget sur LiteLLM.", 503)
        now_iso = datetime.utcnow().isoformat()
        if grant:
            db.execute(
                "UPDATE budget_grants SET current_budget=?, expires_at=?, updated_at=? WHERE id=?",
                (new_budget, expires_at, now_iso, grant['id']))
        else:
            db.execute(
                "INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (breq['username'], base, new_budget, expires_at, now_iso, now_iso))
    else:
        new_budget = current_budget + amount_val
        if not litellm_update_user_budget(breq['username'], new_budget,
                                          budget_duration=grant_duration):
            return _json_error("Erreur lors de la mise à jour du budget sur LiteLLM.", 503)
        if grant:
            # PERMANENT raise during a grant: it must survive the deadline,
            # so it raises the base cap itself.
            db.execute("UPDATE budget_grants SET base_budget=?, current_budget=?, updated_at=? WHERE id=?",
                       (grant['base_budget'] + amount_val, grant['current_budget'] + amount_val,
                        datetime.utcnow().isoformat(), grant['id']))
    db.execute(
        "UPDATE budget_requests SET status='approved', granted_amount=?, updated_at=? WHERE id=?",
        (amount_val, datetime.now().isoformat(), req_id)
    )
    db.commit()
    add_notification(breq['username'], 'request',
                     f"Budget accordé : +{amount_val:,.0f} tokens.".replace(',', ' '))
    # Granting a budget was NOT audited: yet it is the action with the richest
    # incident history on this machine (the 6,7e12 tokens typo),
    # and « who raised whose quota » must have an answer in the database.
    log_audit(session.get('username'), 'budget.approve',
              f"{breq['username']} +{amount_val:.0f} tokens"
              + (f" (temporaire, retour à {grant['base_budget'] if grant else current_budget:.0f} "
                 f"le {expires_at[:16]})" if expires_at else " (permanent)"))
    _fmt = lambda n: f"{n:,.0f}".replace(',', ' ')  # noqa: E731
    if expires_at:
        return _json_ok(f"+{_fmt(amount_val)} tokens accordés à {breq['fullname']} "
                        f"(total temporaire : {_fmt(new_budget)}, retour à "
                        f"{_fmt(grant['base_budget'] if grant else current_budget)} "
                        f"le {expires_at[:16].replace('T', ' ')} UTC).")
    return _json_ok(f"+{_fmt(amount_val)} tokens accordés à {breq['fullname']} "
                    f"(nouveau total : {_fmt(new_budget)} / {grant_duration}).")


@bp.route('/admin/users/<username>/budget/set', methods=['POST'])
@admin_required
def set_user_budget(username):
    """Redefine an account's cap (EXACT amount, not an addition).

    Allows LOWERING a quota (200M → 50M) as well as raising it. Any temporary
    grant in progress is overwritten: the admin decision stands.

    0 is REFUSED. Measured on this instance: `user/update` with `max_budget: 0`
    does leave 0 in the database (LiteLLM does not interpret it as
    « unlimited »), so the account is really capped at zero tokens — a
    « je mets 0 pour lever la limite » cuts access. Blocking has the same
    effect, reversible and with a recorded reason.
    """
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    db = get_db()
    raw = request.form.get('budget', '').strip()
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        return _json_error(
            "Budget : nombre strictement positif attendu (tokens). Pour couper "
            "l'accès d'un compte, utilise le blocage du compte — réversible et "
            "tracé ; un budget de 0 le plafonne réellement à zéro token.")
    if value > 1_000_000_000_000:
        # Same guardrail as approval: beyond 1e12 tokens it is a typo that
        # makes the account unlimited de facto.
        return _json_error("Montant irréaliste (> 1e12 tokens) — vérifie la saisie.")
    grant_duration = get_setting('default_key_duration', KEY_DURATION)
    if not litellm_update_user_budget(username, value, budget_duration=grant_duration):
        return _json_error("Erreur lors de la mise à jour du budget sur LiteLLM.", 503)
    db.execute("DELETE FROM budget_grants WHERE username=?", (username,))
    # A directory account's group quota no longer overwrites this cap at login.
    db.execute("UPDATE user_sources SET budget_manual=1, budget_auto=NULL WHERE username=?",
               (username,))
    db.commit()
    log_audit(session.get('username', '?'), 'budget_set', f'{username}={value:.0f}')
    _fmt = lambda n: f"{n:,.0f}".replace(',', ' ')  # noqa: E731
    return _json_ok(f"Budget de {username} redéfini à {_fmt(value)} tokens / {grant_duration}.")


def revert_expired_grants():
    """Brings back to their base cap the accounts whose grant has expired.

    Returns the number of accounts brought back. Called by the reaper (portal
    thread, every 60 s) and once at startup: after an outage, the missed
    deadline is caught up within the minute of boot.
    """
    db = get_db()
    now = datetime.utcnow().isoformat()
    rows = db.execute("SELECT * FROM budget_grants WHERE expires_at <= ?", (now,)).fetchall()
    if not rows:
        return 0
    grant_duration = get_setting('default_key_duration', KEY_DURATION)
    brought_back = 0
    for g in rows:
        info = _litellm_user_info(g['username'])
        current = info.get('max_budget')
        if current is None or abs(current - g['current_budget']) < 1:
            # Nobody has touched the cap since the boost: we return to the base.
            if not litellm_update_user_budget(g['username'], g['base_budget'],
                                              budget_duration=grant_duration):
                # The row MUST NOT disappear: it carries the deadline AND the
                # base cap, hence the only reason to retry at the next pass.
                # Deleting it despite the failure turned a TEMPORARY grant into
                # a permanent raise, silently — one minute of unreachable
                # LiteLLM sufficed, and nobody could know anymore that a return
                # was due.
                log_audit('reaper', 'budget.grant_revert_echec',
                          f"{g['username']} : retour au plafond de base non appliqué "
                          f"(LiteLLM injoignable ?), nouvelle tentative au prochain passage")
                continue
            brought_back += 1
        # Otherwise the admin intervened in the meantime: the grant is simply
        # forgotten, we do NOT overwrite their decision.
        db.execute("DELETE FROM budget_grants WHERE id=?", (g['id'],))
    db.commit()
    return brought_back

@bp.route('/admin/budget/reject/<int:req_id>', methods=['POST'])
@admin_required
def reject_budget(req_id):
    db = get_db()
    breq = db.execute("SELECT username FROM budget_requests WHERE id=?", (req_id,)).fetchone()
    if not breq:
        return _json_error("Demande introuvable.", 404)
    db.execute(
        "UPDATE budget_requests SET status='rejected', updated_at=? WHERE id=?",
        (datetime.now().isoformat(), req_id)
    )
    db.commit()
    if breq['username']:
        add_notification(breq['username'], 'request', "Ta demande de budget a été refusée.")
    log_audit(session.get('username'), 'budget.reject', breq['username'] or '?')
    return _json_ok("Demande rejetée.")
