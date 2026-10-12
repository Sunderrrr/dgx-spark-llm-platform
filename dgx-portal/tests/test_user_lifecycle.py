"""Account lifecycle: access cut, data purged, sessions visible.

Written on 2026-09-13, along with the fix it locks in. Before, deleting
an account only did a DELETE in `local_users`: its API keys stayed valid
(LiteLLM validates them itself, the portal is not in the API path), its
browser session survived up to 12 h, and its personal data stayed
attached to a name a colleague could take over — memory being indexed
per account, a recreated account inherited the previous one's
memories.

Two design rules deserve to be locked in here, because they are
counter-intuitive:

1. An account the portal has NO trace of must not be considered deleted
   (`test_compte_non_local_sans_ligne_reste_valide`): we never infer a
   revocation from missing data. The first version of this fix did, and
   logged everyone out at once.
2. The role is re-read at every request for a LOCAL account (the portal
   owns it), but not reinvented for a directory account, whose role comes
   from the last recorded login.
"""
import secrets
import os
import time
import unittest
from unittest import mock
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

import app as portal
try:                                     # discovery runs tests/ as top level
    from settings_helper import live_settings
except ImportError:                      # single module: tests.test_x
    from tests.settings_helper import live_settings
import litellm_client
import user_lifecycle

TARGET_USER = 'ztest-cible'
ADMIN = 'ztest-admin'
DIRECTORY_USER = 'ztest-annuaire'
MDP = 'MotDePasse123'


class AccountsBase(unittest.TestCase):
    """Common base: two local accounts (a plain one, an admin) and a strict
    cleanup, so these tests leave nothing behind."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self._clean()
        self._mkuser(TARGET_USER, MDP, is_admin=0)
        self._mkuser(ADMIN, MDP, is_admin=1)

    def tearDown(self):
        self._clean()

    def _tables(self):
        return ('user_sessions', 'user_sources', 'blocked_users', 'api_keys',
                'memory_nodes', 'memory_edges', 'conversations', 'conversation_shares',
                'user_prefs', 'webauthn_credentials', 'user_security', 'pending_webauthn',
                'audit_log')

    def _clean(self):
        with portal.app.app_context():
            db = portal.get_db()
            for t in self._tables():
                db.execute(f"DELETE FROM {t} WHERE username IN (?,?,?)",
                           (TARGET_USER, ADMIN, DIRECTORY_USER))
            db.execute("DELETE FROM login_attempts WHERE key LIKE ?", ('%ztest%',))
            db.execute("DELETE FROM local_users WHERE username IN (?,?,?)",
                       (TARGET_USER, ADMIN, DIRECTORY_USER))
            db.commit()

    def _mkuser(self, username, password, is_admin=0):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO local_users "
                "(username, password_hash, fullname, is_admin, group_name, max_budget, "
                "enabled, created_at) VALUES (?,?,?,?,NULL,NULL,1,?)",
                (username, generate_password_hash(password), username, is_admin,
                 time.time()))
            db.commit()

    def _uid(self, username):
        with portal.app.app_context():
            return portal.get_db().execute(
                "SELECT id FROM local_users WHERE username=?", (username,)).fetchone()['id']

    def _client(self, username, is_admin=False, sid=None, ip='82.67.54.181',
                user_agent='Mozilla/5.0 (X11; Linux) Test/1.0'):
        """Client with an opened session and, if asked, its server row."""
        if sid is not None:
            with portal.app.app_context():
                db = portal.get_db()
                db.execute(
                    "INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                    "created_at, ip, user_agent) VALUES (?,?,?,?,0,?,?,?)",
                    (sid, username, time.time(), time.time() + 3600, time.time(), ip, user_agent))
                db.commit()
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['csrf'] = 'tok'
            s['username'] = username
            s['auth_at'] = int(time.time())
            s['is_admin'] = is_admin
            if sid:
                s['sid'] = sid
        return c

    def _post(self, client, url, data=None, json=None):
        # werkzeug refuses data AND json together: we send only one.
        kwargs = {'headers': {'X-CSRFToken': 'tok'}}
        if json is not None:
            kwargs['json'] = json
        else:
            kwargs['data'] = data or {}
        return client.post(url, **kwargs)


class RevalidationTest(AccountsBase):
    """The account state is re-read at every request, not frozen in the cookie."""

    def test_disabling_cuts_current_session(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(TARGET_USER, sid=sid)
        self.assertEqual(c.get('/api/whoami').status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("UPDATE local_users SET enabled=0 WHERE username=?", (TARGET_USER,))
            db.commit()
        # Without the re-read, this session stayed valid up to 12 h.
        self.assertEqual(c.get('/api/whoami').status_code, 401)

    def test_demotion_removes_admin_right(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(ADMIN, is_admin=True, sid=sid)
        self.assertEqual(c.get('/api/admin/users').status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("UPDATE local_users SET is_admin=0 WHERE username=?", (ADMIN,))
            db.commit()
        self.assertEqual(c.get('/api/admin/users').status_code, 403)
        self.assertFalse(c.get('/api/whoami').get_json()['is_admin'])

    def test_blocking_cuts_session(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(TARGET_USER, sid=sid)
        self.assertEqual(c.get('/api/whoami').status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO blocked_users (username, reason, blocked_by, blocked_at) "
                       "VALUES (?,?,?,?)", (TARGET_USER, 'test', ADMIN, '2026-09-13T10:00:00'))
            db.commit()
        self.assertEqual(c.get('/api/whoami').status_code, 401)

    def test_non_local_account_without_row_stays_valid(self):
        """An account with no local row NOR recorded source keeps its session.

        This is the « we infer nothing from missing data » rule: the first
        version of the fix treated such accounts as deleted and logged
        everyone out at once."""
        c = self._client('ztest-inconnu', is_admin=True)
        self.assertEqual(c.get('/api/whoami').status_code, 200)

    def test_recorded_directory_role_is_authoritative(self):
        """For an LDAP/SSO account, the last recorded role is re-read."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_sources (username, sources, fullname, last_source, "
                       "last_seen, last_is_admin) VALUES (?,?,?,?,?,?)",
                       (DIRECTORY_USER, 'sso', DIRECTORY_USER, 'sso', '2026-09-13T10:00:00', 1))
            db.commit()
        c = self._client(DIRECTORY_USER, is_admin=True)
        self.assertEqual(c.get('/api/admin/users').status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("UPDATE user_sources SET last_is_admin=0 WHERE username=?", (DIRECTORY_USER,))
            db.commit()
        # The rights were removed on the directory side: the session follows.
        self.assertEqual(c.get('/api/admin/users').status_code, 403)


class BlockingTest(AccountsBase):
    """Blocking refuses access whatever the authentication source."""

    def test_block_cuts_open_sessions(self):
        sid = secrets.token_urlsafe(32)
        self._client(TARGET_USER, sid=sid)
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{TARGET_USER}/block', json={'reason': 'départ'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['revoked_sessions'], 1)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT revoked FROM user_sessions WHERE sid=?", (sid,)).fetchone()
        self.assertEqual(row['revoked'], 1)

    def test_blocked_account_cannot_log_in(self):
        admin = self._client(ADMIN, is_admin=True)
        self._post(admin, f'/admin/users/{TARGET_USER}/block', json={'reason': 'départ'})
        anon = portal.app.test_client()
        with anon.session_transaction() as s:
            s['csrf'] = 'tok'
        r = anon.post('/login', data={'username': TARGET_USER, 'password': MDP},
                      headers={'X-CSRFToken': 'tok', 'Cf-Connecting-Ip': '82.67.54.181'})
        # 403 and not « identifiants incorrects »: the password is right,
        # it is the account that is refused.
        self.assertEqual(r.status_code, 403)

    def test_unblock_restores_access(self):
        admin = self._client(ADMIN, is_admin=True)
        self._post(admin, f'/admin/users/{TARGET_USER}/block', json={'reason': 'test'})
        self.assertEqual(self._post(admin, f'/admin/users/{TARGET_USER}/unblock').status_code, 200)
        anon = portal.app.test_client()
        with anon.session_transaction() as s:
            s['csrf'] = 'tok'
        r = anon.post('/login', data={'username': TARGET_USER, 'password': MDP},
                      headers={'X-CSRFToken': 'tok', 'Cf-Connecting-Ip': '82.67.54.181'})
        self.assertEqual(r.status_code, 302)

    def test_directory_account_blockable(self):
        """The case that had NO lever before: an LDAP/SSO account without a
        local row, that one could only log out — it came back."""
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{DIRECTORY_USER}/block', json={'reason': 'parti'})
        self.assertEqual(r.status_code, 200)
        c = self._client(DIRECTORY_USER, is_admin=False)
        self.assertEqual(c.get('/api/whoami').status_code, 401)

    def test_self_block_refused(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{ADMIN}/block', json={'reason': 'error'})
        self.assertEqual(r.status_code, 400)

    def test_last_local_admin_protected(self):
        other = self._client(DIRECTORY_USER, is_admin=True)
        r = self._post(other, f'/admin/users/{ADMIN}/block', json={'reason': 'test'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('Dernier administrateur', r.get_json()['error'])


class SuppressionTest(AccountsBase):
    """Deleting an account means removing an access, not erasing a row."""

    def _data(self, username):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO api_keys (username, key_alias, key_value, created_at) "
                       "VALUES (?,?,?,?)", (username, 'test', 'sk-test-123', '2026-09-13'))
            db.execute("INSERT INTO memory_nodes (username, name, name_norm, kind, created_at) "
                       "VALUES (?,?,?,?,?)", (username, 'vLLM', 'vllm', 'subject', '2026-09-13'))
            db.execute("INSERT INTO memory_edges (username, src_id, relation, fact, created_at) "
                       "VALUES (?,?,?,?,?)", (username, 1, 'concerne', 'un fait', '2026-09-13'))
            db.execute("INSERT INTO conversations (username, client_id, title, model, messages, "
                       "updated_at) VALUES (?,?,?,?,?,?)",
                       (username, 'c1', 't', 'm', '[]', '2026-09-13'))
            db.execute("INSERT INTO user_prefs (username, theme_id) VALUES (?,?)",
                       (username, 'dark'))
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (secrets.token_urlsafe(32), username, time.time(),
                        time.time() + 3600, time.time()))
            db.commit()

    def test_without_confirmation_refused(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/delete/{self._uid(TARGET_USER)}')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()['needs_confirm'])
        # The summary is provided so the UI can say what will be lost.
        self.assertIn('conversations', r.get_json()['data'])
        self.assertIsNotNone(self._uid(TARGET_USER))

    def test_self_delete_refused(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/delete/{self._uid(ADMIN)}',
                       json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('propre compte', r.get_json()['error'])

    def test_last_local_admin_protected(self):
        other = self._client(DIRECTORY_USER, is_admin=True)
        r = self._post(other, f'/admin/users/delete/{self._uid(ADMIN)}',
                       json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('Dernier administrateur', r.get_json()['error'])

    def test_deletion_purges_access_and_data(self):
        self._data(TARGET_USER)
        admin = self._client(ADMIN, is_admin=True)
        with patch.object(litellm_client, 'revoke_litellm_key', return_value=True), \
             patch.object(litellm_client, 'delete_litellm_user', return_value=True):
            r = self._post(admin, f'/admin/users/delete/{self._uid(TARGET_USER)}',
                           json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 200)
        purged = r.get_json()['purged']
        self.assertEqual(purged['keys_revoked'], 1)
        self.assertEqual(purged['keys_failed'], 0)
        self.assertTrue(purged['litellm_user'])
        with portal.app.app_context():
            db = portal.get_db()
            for table in ('local_users', 'api_keys', 'memory_nodes', 'memory_edges',
                          'conversations', 'user_prefs'):
                n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE username=?",
                               (TARGET_USER,)).fetchone()[0]
                self.assertEqual(n, 0, f'{table} devrait être purgée')
            sessions = db.execute(
                "SELECT COUNT(*) FROM user_sessions WHERE username=? AND revoked=0",
                (TARGET_USER,)).fetchone()[0]
            self.assertEqual(sessions, 0)
            trace = db.execute(
                "SELECT detail FROM audit_log WHERE action='user.delete' ORDER BY id DESC"
            ).fetchone()
        self.assertIn(TARGET_USER, trace['detail'])

    def test_revocation_failure_reported(self):
        """LiteLLM unreachable: the deletion succeeds, but the admin is warned
        that keys may survive. The silent failure was exactly the
        problem."""
        self._data(TARGET_USER)
        admin = self._client(ADMIN, is_admin=True)
        with patch.object(litellm_client, 'revoke_litellm_key', return_value=False), \
             patch.object(litellm_client, 'delete_litellm_user', return_value=False):
            r = self._post(admin, f'/admin/users/delete/{self._uid(TARGET_USER)}',
                           json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['purged']['keys_failed'], 1)
        self.assertIn('LiteLLM', body.get('warning', ''))
        self.assertIn('NON supprimée', self._last_trace())

    def _last_trace(self):
        with portal.app.app_context():
            return portal.get_db().execute(
                "SELECT detail FROM audit_log WHERE action='user.delete' ORDER BY id DESC"
            ).fetchone()['detail']


class DirectoryAccountPurgeTest(AccountsBase):
    """A directory account has no local row: its deletion is impossible by
    construction, but its data must not remain."""

    def _data(self, username):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO memory_nodes (username, name, name_norm, kind, created_at) "
                       "VALUES (?,?,?,?,?)", (username, 'subject', 'subject', 'subject', '2026-09-13'))
            db.execute("INSERT INTO conversations (username, client_id, title, model, messages, "
                       "updated_at) VALUES (?,?,?,?,?,?)",
                       (username, 'c1', 't', 'm', '[]', '2026-09-13'))
            db.commit()

    def test_purge_erases_data_without_touching_access(self):
        self._data(DIRECTORY_USER)
        admin = self._client(ADMIN, is_admin=True)
        # We BLOCK first: it is the ACCESS state that must survive the purge.
        # Without this prior block, the test missed the defect — `blocked_users`
        # was among the purged tables, so the normal gesture when a directory
        # employee leaves (block then purge) made the account loggable again,
        # silently and without saying so in the audit.
        self.assertEqual(
            self._post(admin, f'/admin/users/{DIRECTORY_USER}/block',
                       json={'reason': 'parti'}).status_code, 200)
        r = self._post(admin, f'/admin/users/{DIRECTORY_USER}/purge', json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['purged']['memory_facts'], 1)
        with portal.app.app_context():
            db = portal.get_db()
            remaining = db.execute("SELECT COUNT(*) FROM memory_nodes WHERE username=?",
                               (DIRECTORY_USER,)).fetchone()[0]
            trace = db.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0]
        self.assertEqual(remaining, 0)
        # The audit distinguishes a purge from a deletion: it is not the same
        # gesture, and a directory account can come back.
        self.assertEqual(trace, 'user.purge')
        with portal.app.app_context():
            # Purging is not unblocking: the purge only erases DATA, blocking
            # remains the offboarding lever.
            self.assertTrue(portal.is_blocked(DIRECTORY_USER))

    def test_without_confirmation_refused(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{DIRECTORY_USER}/purge')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()['needs_confirm'])

    def test_local_account_refused_on_this_route(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{TARGET_USER}/purge', json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('suppression', r.get_json()['error'])

    def test_own_account_purge_refused(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{ADMIN}/purge', json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 400)


class DetailTest(AccountsBase):
    """The detail view must inform without ever leaking a key."""

    def test_detail_never_returns_key_value(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO api_keys (username, key_alias, key_value, created_at) "
                       "VALUES (?,?,?,?)", (TARGET_USER, 'captures', 'sk-secret-a-ne-pas-sortir',
                                            '2026-09-13'))
            db.commit()
        admin = self._client(ADMIN, is_admin=True)
        r = admin.get(f'/admin/users/{TARGET_USER}/detail')
        self.assertEqual(r.status_code, 200)
        raw_text = r.get_data(as_text=True)
        self.assertNotIn('sk-secret-a-ne-pas-sortir', raw_text)
        body = r.get_json()
        self.assertEqual(body['keys'][0]['alias'], 'captures')
        self.assertTrue(body['local'])

    def test_directory_account_detail(self):
        admin = self._client(ADMIN, is_admin=True)
        r = admin.get(f'/admin/users/{DIRECTORY_USER}/detail')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['local'])

    def test_detail_carries_activity_and_directory_state(self):
        """The admin sees the same 6-month activity as the account's own
        settings, and, for a directory account, its state in the directory."""
        import admin_routes
        series_ = {'days': [{'date': '2026-10-10', 'tokens': 42}], 'total': 42, 'prompt': 40,
                 'completion': 2, 'peak': 42, 'peak_day': '2026-10-10', 'active_days': 1, 'avg': 42}
        admin = self._client(ADMIN, is_admin=True)
        with mock.patch.object(admin_routes, '_account_activity', return_value=series_) as act, \
             mock.patch.object(admin_routes, 'directory_states_for',
                               return_value={DIRECTORY_USER: 'disabled'}):
            body = admin.get(f'/admin/users/{DIRECTORY_USER}/detail').get_json()
            local = admin.get(f'/admin/users/{TARGET_USER}/detail').get_json()
        act.assert_any_call(DIRECTORY_USER)
        self.assertEqual(body['activity']['active_days'], 1)
        self.assertEqual(body['directory_status'], 'disabled')
        self.assertEqual(set(body['media']), {'image', 'music', 'video', 'voice', 'ocr'})
        self.assertEqual(body['media']['image']['count'], 0)
        self.assertEqual(body['model_requests'], [])
        self.assertIsNone(local['directory_status'], "a local account's state is `enabled`")

    def test_list_carries_directory_state_of_non_local_accounts(self):
        import admin_routes
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT OR REPLACE INTO user_sources (username, sources, fullname, last_source, "
                       "last_seen, last_is_admin) VALUES (?,?,?,?,?,?)",
                       (DIRECTORY_USER, 'ldap', DIRECTORY_USER, 'ldap', '2026-10-11T10:00:00', 0))
            db.commit()
        admin = self._client(ADMIN, is_admin=True)
        with mock.patch.object(admin_routes, 'directory_states_for',
                               side_effect=lambda names: {n: 'active' for n in names}) as states:
            users = {u['username']: u for u in admin.get('/api/admin/users').get_json()['users']}
        requests_ = set(states.call_args[0][0])
        self.assertIn(DIRECTORY_USER, requests_)
        self.assertNotIn(TARGET_USER, requests_, "a local account is not looked up")
        self.assertEqual(users[DIRECTORY_USER]['directory_status'], 'active')
        self.assertIsNone(users[TARGET_USER]['directory_status'])

    def test_account_list_exposes_block_state(self):
        admin = self._client(ADMIN, is_admin=True)
        self._post(admin, f'/admin/users/{TARGET_USER}/block', json={'reason': 'départ'})
        users = {u['username']: u for u in admin.get('/api/admin/users').get_json()['users']}
        self.assertTrue(users[TARGET_USER]['blocked'])
        self.assertEqual(users[TARGET_USER]['block_reason'], 'départ')
        self.assertFalse(users[ADMIN]['blocked'])

    def test_account_list_carries_avatar(self):
        """The admin list carries the avatar, like the rest of the UI.

        The admin screen shows the pp there: chosen brand logo, or
        generated avatar when `avatar_id` is null. Without this field the
        admin saw initials where the person sees their pp — unguessable.
        """
        admin = self._client(ADMIN, is_admin=True)
        users = {u['username']: u for u in admin.get('/api/admin/users').get_json()['users']}
        self.assertIn('avatar_id', users[TARGET_USER])
        self.assertIsNone(users[TARGET_USER]['avatar_id'], "aucun logo choisi : avatar généré")

        with portal.app.app_context():
            db = portal.get_db()
            logo = portal.AVATAR_IDS[0]
            db.execute("INSERT INTO user_prefs (username, avatar_id) VALUES (?,?) "
                       "ON CONFLICT(username) DO UPDATE SET avatar_id=excluded.avatar_id",
                       (TARGET_USER, logo))
            db.commit()
        users = {u['username']: u for u in admin.get('/api/admin/users').get_json()['users']}
        self.assertEqual(users[TARGET_USER]['avatar_id'], logo)


class AutoServiceTest(AccountsBase):
    """What the user can do for themselves."""

    def test_list_my_sessions_without_full_sid(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(TARGET_USER, sid=sid)
        r = c.get('/api/account/sessions')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['local_account'])
        self.assertEqual(len(body['sessions']), 1)
        s = body['sessions'][0]
        # Short fingerprint only: the sid is the cookie's secret.
        self.assertEqual(len(s['id']), 12)
        self.assertNotIn(sid, r.get_data(as_text=True))
        self.assertTrue(s['current'])
        self.assertEqual(s['ip'], '82.67.54.181')

    def test_revoke_another_session(self):
        sid = secrets.token_urlsafe(32)
        other = secrets.token_urlsafe(32)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (other, TARGET_USER, time.time() - 60, time.time() + 3600, time.time()))
            db.commit()
        c = self._client(TARGET_USER, sid=sid)
        r = self._post(c, '/api/account/sessions/revoke', json={'id': other[:12]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['current'])
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT revoked FROM user_sessions WHERE sid=?", (other,)).fetchone()
        self.assertEqual(row['revoked'], 1)

    def test_revoke_current_session(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(TARGET_USER, sid=sid)
        r = self._post(c, '/api/account/sessions/revoke', json={'id': sid[:12]})
        self.assertTrue(r.get_json()['current'])
        self.assertEqual(c.get('/api/whoami').status_code, 401)

    def test_cannot_revoke_someone_elses_session(self):
        sid_other = secrets.token_urlsafe(32)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (sid_other, ADMIN, time.time(), time.time() + 3600, time.time()))
            db.commit()
        c = self._client(TARGET_USER, sid=secrets.token_urlsafe(32))
        r = self._post(c, '/api/account/sessions/revoke', json={'id': sid_other[:12]})
        self.assertEqual(r.status_code, 404)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT revoked FROM user_sessions WHERE sid=?", (sid_other,)).fetchone()
        self.assertEqual(row['revoked'], 0)

    def test_revoke_all_others(self):
        sid = secrets.token_urlsafe(32)
        with portal.app.app_context():
            db = portal.get_db()
            for _ in range(2):
                db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, "
                           "revoked, created_at) VALUES (?,?,?,?,0,?)",
                           (secrets.token_urlsafe(32), TARGET_USER, time.time(),
                            time.time() + 3600, time.time()))
            db.commit()
        c = self._client(TARGET_USER, sid=sid)
        r = self._post(c, '/api/account/sessions/revoke', json={'all': True})
        self.assertEqual(r.get_json()['revoked'], 2)
        self.assertEqual(c.get('/api/whoami').status_code, 200)

    def test_directory_account_cannot_change_password(self):
        c = self._client(DIRECTORY_USER)
        r = self._post(c, '/api/account/password',
                       json={'current': 'x', 'new': 'NouveauMotDePasse1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('annuaire', r.get_json()['error'])

    def test_wrong_current_password(self):
        c = self._client(TARGET_USER)
        r = self._post(c, '/api/account/password',
                       json={'current': 'faux', 'new': 'NouveauMotDePasse1'})
        self.assertEqual(r.status_code, 400)

    def test_password_change_closes_other_sessions(self):
        sid = secrets.token_urlsafe(32)
        other = secrets.token_urlsafe(32)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (other, TARGET_USER, time.time(), time.time() + 3600, time.time()))
            db.commit()
        c = self._client(TARGET_USER, sid=sid)
        r = self._post(c, '/api/account/password',
                       json={'current': MDP, 'new': 'NouveauMotDePasse1'})
        self.assertEqual(r.status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            row = db.execute("SELECT password_hash FROM local_users WHERE username=?",
                             (TARGET_USER,)).fetchone()
            self.assertTrue(check_password_hash(row['password_hash'], 'NouveauMotDePasse1'))
            other_row = db.execute("SELECT revoked FROM user_sessions WHERE sid=?",
                                   (other,)).fetchone()
            current = db.execute("SELECT revoked FROM user_sessions WHERE sid=?",
                                  (sid,)).fetchone()
        self.assertEqual(other_row['revoked'], 1)
        # The current session survives: being logged out by one's own
        # action discourages changing one's password.
        self.assertEqual(current['revoked'], 0)
        self.assertEqual(c.get('/api/whoami').status_code, 200)

    def test_refuses_identical_password(self):
        c = self._client(TARGET_USER)
        r = self._post(c, '/api/account/password', json={'current': MDP, 'new': MDP})
        self.assertEqual(r.status_code, 400)


class SessionOriginTest(AccountsBase):
    """A session opened before the IP/user-agent columns were added has none:
    its bearer must be able to fill them in by opening their list, else they
    cannot recognize their own sessions (a dash says nothing)."""

    def test_session_without_origin_completed_on_read(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(TARGET_USER, sid=sid, ip=None, user_agent=None)
        self.assertEqual(c.get('/api/account/sessions').status_code, 200)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT ip, user_agent FROM user_sessions WHERE sid=?", (sid,)).fetchone()
        self.assertTrue(row['ip'])
        self.assertTrue(row['user_agent'])

    def test_recorded_origin_never_overwritten(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(TARGET_USER, sid=sid, ip='203.0.113.9', user_agent='Ancien/1.0')
        self.assertEqual(c.get('/api/account/sessions').status_code, 200)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT ip, user_agent FROM user_sessions WHERE sid=?", (sid,)).fetchone()
        # This is exactly the trace we want to keep: a stolen cookie must not
        # be able to rewrite its own origin by showing up.
        self.assertEqual((row['ip'], row['user_agent']), ('203.0.113.9', 'Ancien/1.0'))

    def test_admin_view_does_not_touch_others_session(self):
        someone_else = secrets.token_urlsafe(32)
        self._client(TARGET_USER, sid=someone_else, ip=None, user_agent=None)
        admin = self._client(ADMIN, is_admin=True)
        self.assertEqual(admin.get(f'/admin/users/{TARGET_USER}/detail').status_code, 200)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT ip FROM user_sessions WHERE sid=?", (someone_else,)).fetchone()
        # Writing the admin's IP there would make the row say « this session
        # comes from the admin » — a lie in the only view used to spot an unknown
        # session.
        self.assertIsNone(row['ip'])


class PasswordPolicyTest(unittest.TestCase):
    """Password rules, tested directly: they serve both the creation by an
    admin and the self-service change."""

    def _err(self, pw, user=None):
        from local_users import password_policy_error
        return password_policy_error(pw, user)

    def test_too_short(self):
        self.assertIsNotNone(self._err('court'))

    def test_common_password_refused(self):
        self.assertIsNotNone(self._err('password123'))

    def test_username_in_password_refused(self):
        self.assertIsNotNone(self._err('ztest-cible2026', 'ztest-cible'))

    def test_repetitive_refused(self):
        self.assertIsNotNone(self._err('aaaaaaaaaaaa'))

    def test_correct_password_accepted(self):
        self.assertIsNone(self._err('ChevalPileOrage42', 'ztest-cible'))


class QuotaTest(AccountsBase):
    """A quota that could not be written must no longer go unnoticed."""

    def test_quota_failure_reported_on_creation(self):
        admin = self._client(ADMIN, is_admin=True)
        with patch('admin_routes._sync_local_user_budget', return_value=False):
            r = self._post(admin, '/admin/users/create',
                           data={'username': 'ztest-nouveau', 'password': 'MotDePasse123'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('warning', r.get_json())
        with portal.app.app_context():
            portal.get_db().execute("DELETE FROM local_users WHERE username='ztest-nouveau'")
            portal.get_db().commit()

    def test_group_deletion_resyncs_members(self):
        seen = []
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) "
                       "VALUES (?,?,?,?)", ('ztest-groupe', 5000, 0, '2026-09-13'))
            db.execute("UPDATE local_users SET group_name='ztest-groupe' WHERE username=?",
                       (TARGET_USER,))
            db.commit()
        admin = self._client(ADMIN, is_admin=True)

        def fake_sync(username, row):
            seen.append(username)
            return True

        with patch('admin_routes._sync_local_user_budget', side_effect=fake_sync):
            r = self._post(admin, '/admin/groups/delete/ztest-groupe')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['members'], 1)
        # Without this propagation, the member kept the old envelope: a ghost
        # quota, more generous than the default.
        self.assertEqual(seen, [TARGET_USER])
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT group_name FROM local_users WHERE username=?", (TARGET_USER,)).fetchone()
        self.assertIsNone(row['group_name'])

    def test_member_with_own_cap_not_resynced(self):
        seen = []
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) "
                       "VALUES (?,?,?,?)", ('ztest-groupe', 5000, 0, '2026-09-13'))
            db.execute("UPDATE local_users SET group_name='ztest-groupe', max_budget=777 "
                       "WHERE username=?", (TARGET_USER,))
            db.commit()
        admin = self._client(ADMIN, is_admin=True)
        with patch('admin_routes._sync_local_user_budget',
                   side_effect=lambda u, r: seen.append(u) or True):
            self._post(admin, '/admin/groups/delete/ztest-groupe')
        # Their personal cap did not depend on the group: nothing to propagate.
        self.assertEqual(seen, [])


class PasswordNotificationTest(unittest.TestCase):
    """The password change notification is a bonus, never a condition: it
    must break nothing when the email is not configured."""

    def test_without_smtp_nothing_sent_nothing_raised(self):
        with patch.dict(os.environ, {'SMTP_HOST': ''}):
            self.assertFalse(user_lifecycle.notify_password_changed(TARGET_USER))

    def test_unreachable_directory_does_not_raise(self):
        with patch('auth.ldap_lookup_email', side_effect=RuntimeError('annuaire HS')):
            self.assertFalse(
                user_lifecycle._send_password_notification(TARGET_USER))

    def test_no_known_address_nothing_sent(self):
        with patch('auth.ldap_lookup_email', return_value=None), \
             patch('notify.send_user_email') as sending:
            self.assertFalse(
                user_lifecycle._send_password_notification(TARGET_USER))
        self.assertFalse(sending.called)

    def test_sent_to_right_recipient_with_distinct_message(self):
        with patch('auth.ldap_lookup_email', return_value='quelqu-un@example.com'), \
             patch('notify.send_user_email', return_value=True) as sending:
            self.assertTrue(
                user_lifecycle._send_password_notification(TARGET_USER))
            self.assertTrue(user_lifecycle._send_password_notification(
                TARGET_USER, by_admin=ADMIN))
        self.assertEqual(sending.call_args_list[0][0][0], 'quelqu-un@example.com')
        # The message says WHO changed the password: this is the whole value
        # of the notification for the person receiving it.
        self.assertIn("Ton mot de passe", sending.call_args_list[0][0][2])
        self.assertIn("Un administrateur", sending.call_args_list[1][0][2])

    def test_response_does_not_wait_for_directory(self):
        """Defect measured on 2026-09-16: 3,2 s of response because of the LDAP bind.

        The password change was already written to database, but the click
        stayed suspended until the (mute) directory saw fit to expire.
        The notification is now entrusted to a thread: we check here that
        the call returns immediately, even when the address lookup is slow,
        then we wait for the thread to verify the email still goes out — a
        latency fix that would lose the notification would be worse than
        the evil.
        """
        import time as _time
        seen = []

        def _lent(_user):
            _time.sleep(1.5)
            return 'quelqu-un@example.com'

        with live_settings(smtp_host='smtp.example'), \
             patch('auth.ldap_lookup_email', side_effect=_lent), \
             patch('notify.send_user_email',
                   side_effect=lambda *a, **k: seen.append(a) or True):
            t0 = _time.time()
            self.assertTrue(user_lifecycle.notify_password_changed(TARGET_USER))
            elapsed = _time.time() - t0
            self.assertLess(elapsed, 0.5,
                            f"la réponse a attendu l'annuaire ({elapsed:.2f} s)")
            for _ in range(60):                       # up to ~3 s
                if seen:
                    break
                _time.sleep(0.05)
        self.assertTrue(seen, "the notification must eventually go out despite the thread")


class HygieneSweepTest(AccountsBase):
    """The startup sweep must only remove what is DEAD.

    The risk of this kind of cleanup is making a still valid session
    disappear, which would log everyone out — hence a test checking both
    sides: stale rows go, live ones stay.
    """

    def test_stale_rows_go_live_rows_stay(self):
        with portal.app.app_context():
            db = portal.get_db()
            sid_live = secrets.token_urlsafe(32)
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (sid_live, TARGET_USER, time.time(), time.time() + 3600, time.time()))
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,1,?)",
                       (secrets.token_urlsafe(32), TARGET_USER, time.time() - 40 * 86400,
                        time.time() - 39 * 86400, time.time() - 40 * 86400))
            db.execute("INSERT INTO login_attempts (key, fails, first_at, locked_until) "
                       "VALUES (?,?,?,?)", ('82.0.0.1|ztest-vieux', 6,
                                            time.time() - 30 * 86400, 0))
            db.execute("INSERT INTO pending_webauthn (nonce, username, kind, challenge, "
                       "created_at, expires_at) VALUES (?,?,?,?,?,?)",
                       ('ztest-nonce', TARGET_USER, 'login', b'x', time.time() - 600, time.time() - 300))
            db.commit()
            portal.init_db()
            db = portal.get_db()
            remaining = {r['sid'] for r in db.execute(
                "SELECT sid FROM user_sessions WHERE username=?", (TARGET_USER,)).fetchall()}
            old = db.execute("SELECT COUNT(*) FROM login_attempts WHERE key LIKE ?",
                               ('%ztest-vieux',)).fetchone()[0]
            nonce = db.execute("SELECT COUNT(*) FROM pending_webauthn WHERE nonce=?",
                               ('ztest-nonce',)).fetchone()[0]
        self.assertEqual(remaining, {sid_live})
        self.assertEqual(old, 0)
        self.assertEqual(nonce, 0)


class PurgeInventoryTest(AccountsBase):
    """The inventory of purged tables must stay honest: any table carrying a
    `username` and absent from the list must be a deliberate choice."""

    # `audit_log` (and its `audit_log_archive`, the rows it evicts — see
    # db.log_audit, 2026-10-02) is the trace of admin actions, `local_users` is
    # erased explicitly by identifier — and `blocked_users` is an ACCESS
    # decision, not personal data: purging it would UNBLOCK the account (see the
    # comment on `PURGED_TABLES`). It is the only offboarding lever for a
    # directory account, so it survives the purge.
    DELIBERATELY_NOT_PURGED = {'audit_log', 'audit_log_archive',
                                  'local_users', 'blocked_users'}

    def test_all_user_tables_covered(self):
        with portal.app.app_context():
            db = portal.get_db()
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            with_username = set()
            for t in tables:
                cols = {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
                if 'username' in cols:
                    with_username.add(t)
        missing = with_username - set(user_lifecycle.PURGED_TABLES) \
            - self.DELIBERATELY_NOT_PURGED
        self.assertEqual(
            missing, set(),
            f"tables carrying a username and not purged: {sorted(missing)} — "
            "add them to PURGED_TABLES or declare them here with the reason")


if __name__ == '__main__':
    unittest.main()
