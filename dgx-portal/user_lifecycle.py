"""Account lifecycle: everything to undo when someone leaves.

Three things happen here, and NONE was done before 2026-09-13:

1. ACCESS. Deleting a local account only did a `DELETE` in
   `local_users`. Its API keys stayed valid — LiteLLM validates them
   itself, the portal is not in the API path — and its browser session
   survived up to 12 h. A leaver thus kept access to the API *and* to
   the portal.
2. THE ENVELOPE. The LiteLLM user object (cap + cumulative spend)
   survived: an account recreated under the same name inherited the
   previous one's spend, and could thus start above its quota.
3. THE DATA. Memory, conversations, shares, preferences, passkeys and
   media jobs stayed in database, attached to a name a colleague could
   take over — memory being indexed by account name, the newcomer
   literally inherited the old one's memories.

The audit log (`audit_log`) is NOT purged: it is the trace of admin
actions, and it must outlive the account it concerns. The generated media
files (image/music/voice) are not deleted here either: the backup
script's orphan purge handles them, with its 7-day grace — deleting the
jobs is enough to orphan them.
"""
import sqlite3
import threading

from instance_config import smtp_config
from db import get_db, log_audit

# Tables attached to an account, purged at its deletion. `local_users` is
# not among them: the delete route erases it explicitly, by identifier,
# which is the only safe way to target the right row.
#
# `blocked_users` is not either, and it is a CHOICE: purging erases DATA,
# blocking decides an ACCESS. Confusing them made a purge — the normal
# gesture when a directory employee leaves, who has no local row — UNBLOCK
# the account silently: it became loggable again, with platform and GPU
# access. The route promises though to erase « without touching its access »,
# and the docs say offboarding remains blocking.
PURGED_TABLES = (
    'announcement_state',
    'api_keys',
    'budget_grants',
    'budget_requests',
    'conversation_shares',
    'conversations',
    'discord_links',
    'image_jobs',
    'inflight_requests',
    'mcp_servers',
    'media_request_cooldown',
    'memory_aliases',
    'memory_edges',
    'memory_nodes',
    'model_requests',
    'music_jobs',
    'notifications',
    'ocr_jobs',
    'pending_actions',
    'pending_webauthn',
    'previews',
    'skills',
    'support_feedback',
    'support_thread',
    'user_prefs',
    'user_security',
    'user_sessions',
    'user_sources',
    'user_directory_groups',   # directory group memberships (directory_groups.py)
    'video_jobs',
    'voice_jobs',
    'webauthn_credentials',
)

_JOBS_MEDIA = ('image_jobs', 'music_jobs', 'ocr_jobs', 'video_jobs', 'voice_jobs')


def _account(table, username, column='username'):
    try:
        return get_db().execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (username,)).fetchone()[0]
    except sqlite3.Error:
        # Table absent from an older database: nothing to count, nothing to purge.
        return 0


def count_data(username):
    """Summary of what will be lost — shown to the admin before they confirm."""
    keys = _account('api_keys', username)
    return {
        'sessions': _account('user_sessions', username),
        'keys': keys,
        'memory_facts': _account('memory_nodes', username),
        'memory_links': _account('memory_edges', username),
        'conversations': _account('conversations', username),
        'shares': _account('conversation_shares', username),
        'media_jobs': sum(_account(t, username) for t in _JOBS_MEDIA),
        'preferences': _account('user_prefs', username),
        'passkeys': _account('webauthn_credentials', username),
    }


def _revoke_keys(username, revoke_litellm_key, log):
    """Revokes the account's API keys on the LiteLLM side, then forgets their rows.

    The clear value comes from `api_keys` (the portal stores it to be able
    to revoke it: the only place it can be found back). A key that LiteLLM
    refuses to delete is reported but does not interrupt the rest: better
    an account without portal than an account with a live key, and the
    report says so.
    """
    db = get_db()
    values = [r['key_value'] for r in db.execute(
        "SELECT key_value FROM api_keys WHERE username=?", (username,)).fetchall()]
    successes = failed = 0
    for value in values:
        try:
            if revoke_litellm_key(value):
                successes += 1
            else:
                failed += 1
        except Exception as exc:                                 # noqa: BLE001
            log(f'[user_lifecycle] révocation de clé impossible : {exc}')
            failed += 1
    # The local rows disappear in every case: a key we could not revoke would
    # stay invisible and unmanageable afterwards anyway.
    db.execute("DELETE FROM api_keys WHERE username=?", (username,))
    db.commit()
    return successes, failed, len(values)


def revoke_account_keys(username, by=None, reason=''):
    """Revokes an account's API keys and RECORDS it. Returns (ok, ko, total).

    Why this path separate from deprovisioning: blocking an account (or
    disabling it) cuts access to the PORTAL, not to the API. LiteLLM
    validates the keys itself and the portal is not in the request path, so
    a blocked account kept a fully functional `Bearer` key — the
    2026-10-02 audit established it, and the gesture SECURITY.md §2.4 names
    as the offboarding of directory accounts was thus incomplete.

    We reuse the SINGLE revocation path (`_revoquer_cles`, the same as
    `deprovisionner_compte`) so there are not two ways to revoke.
    Deliberate trade-off: « débloquer » does NOT restore the keys — the
    account gets its portal access back, the user recreates a key if needed.
    """
    from litellm_client import revoke_litellm_key

    successes, failed, total = _revoke_keys(username, revoke_litellm_key, print)
    log_audit(by or '?', 'user.keys_revoked',
              f'{username} — {successes}/{total} clé(s) révoquée(s)'
              + (f', {failed} ÉCHEC(S) À RÉVOQUER' if failed else '')
              + (f' — {reason}' if reason else ''))
    return successes, failed, total


def purge_data(username):
    """Erases all data attached to the account. Returns the counts."""
    db = get_db()
    total = 0
    for table in PURGED_TABLES:
        try:
            total += db.execute(
                f"DELETE FROM {table} WHERE username=?", (username,)).rowcount
        except sqlite3.Error:
            continue
    # Login attempts: composite keys ('ip|user' and 'user:user'). Without it,
    # an account recreated under the same name would be born already locked.
    try:
        total += db.execute(
            "DELETE FROM login_attempts WHERE key=? OR key LIKE ? ESCAPE '\\'",
            (f"user:{username}",
             f"%|{username.replace(chr(92), chr(92)*2).replace('%', chr(92)+'%').replace('_', chr(92)+'_')}")).rowcount
    except sqlite3.Error:
        pass
    db.commit()
    return total


def deprovision_account(username, by, purge=True, action='user.delete'):
    """Removes every access of the account, then (purge=True) its data.

    The ORDER matters: the keys first — LiteLLM refuses to delete a user
    still carrying some depending on the version —, then the envelope, the
    portal database last. If a step fails, we prefer leaving an account
    without keys over an account without local row but with live
    keys.
    """
    from auth import _revoke_user_sessions                    # late import: auth already imports this module
    from litellm_client import delete_litellm_user, revoke_litellm_key

    report = count_data(username)
    keys_ok, keys_failed, keys_total = _revoke_keys(username, revoke_litellm_key, print)
    report['keys_revoked'] = keys_ok
    report['keys_failed'] = keys_failed
    report['keys'] = keys_total

    sessions = _revoke_user_sessions(username)
    report['sessions'] = sessions

    report['litellm_user'] = bool(delete_litellm_user(username))
    report['lignes_purgees'] = purge_data(username) if purge else 0

    detail = (f"{username} — {keys_ok}/{keys_total} clé(s) révoquée(s)"
              + (f", {keys_failed} ÉCHEC(S)" if keys_failed else '')
              + f", {sessions} session(s), enveloppe LiteLLM "
              + ('supprimée' if report['litellm_user'] else 'NON supprimée')
              + (f", {report['lignes_purgees']} ligne(s) de données purgée(s)"
                 if purge else ', données CONSERVÉES'))
    log_audit(by, action, detail)
    return report


def _send_password_notification(username, by_admin=None):
    """The real work of the notification (address, then email).

    Split from `prevenir_mot_de_passe_change` to be testable without a
    thread: this is where the directory is queried, and it can be slow.
    """
    try:
        from auth import ldap_lookup_email
        from notify import send_user_email
        recipient = ldap_lookup_email(username)
        if not recipient:
            # Local account with no equivalent in the directory: no known
            # address, and we do not invent one.
            print(f'[user_lifecycle] pas d’adresse connue pour {username}, '
                  'notification non envoyée')
            return False
        author = ('Un administrateur a réinitialisé' if by_admin
                  else 'Ton mot de passe a été changé')
        return bool(send_user_email(
            recipient,
            'Mot de passe modifié — DGX platform',
            f"{author} le mot de passe du compte « {username} » sur la plateforme DGX.\n\n"
            "Si tu n'es pas à l'origine de ce changement, contacte un administrateur "
            "immédiatement : toutes les autres sessions du compte ont été fermées, mais "
            "l'accès à l'API doit être vérifié."))
    except Exception as exc:                                     # noqa: BLE001
        print(f'[user_lifecycle] notification de mot de passe non envoyée : {exc}')
        return False


def notify_password_changed(username, by_admin=None):
    """Warns the person concerned that their password just changed, OFF the path.

    This is what gives the self-service change its value: someone whose
    account is taken over stays blind as long as nobody tells them the
    password moved. Deliberate best-effort — without SMTP configured (or
    without a known address for a purely local account), nothing happens
    and the change still succeeds: a notification must never fail the
    action it reports.

    It must not DELAY it either. Measured on 2026-09-16: the answer to
    `POST /api/account/password` took **3,2 s**, because the address is
    looked up via an LDAP bind (`ldap_lookup_email`) and the directory does
    not answer — while the change itself was already written to database.
    The click thus stayed suspended on work that does not concern it. The
    notification now goes out in a daemon thread: it can neither fail
    loudly nor make anyone wait. Returns False only when there is no SMTP.
    """
    if not smtp_config()['host']:
        return False
    threading.Thread(target=_send_password_notification,
                     args=(username, by_admin),
                     name='notif-mot-de-passe', daemon=True).start()
    return True
