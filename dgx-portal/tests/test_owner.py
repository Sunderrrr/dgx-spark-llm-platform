"""The owner account created from the web on a fresh instance (proprietaire.py).

A fresh instance has no account: the login page offers to create the OWNER.
The page is public, so the setup token (server log) is what stops the first
visitor from owning the platform. Locked here: the token is required, setup
closes once anything administers the instance, and the owner is out of reach
of the other admins.
"""
import time
import unittest

import app as portal
import owner

OWNER = 'ztest-owner'
OTHER_ADMIN = 'ztest-autre-admin'
MDP = 'Une phrase de passe solide 42'


class BaseSetup(unittest.TestCase):
    """Puts the shared test database in the « fresh instance » state for the
    test, and restores it."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        with portal.app.app_context():
            db = portal.get_db()
            self._admins = [r[0] for r in db.execute(
                "SELECT id FROM local_users WHERE enabled=1").fetchall()]
            self._directory = [r[0] for r in db.execute(
                "SELECT username FROM user_sources WHERE last_is_admin=1").fetchall()]
            self._groups = [r[0] for r in db.execute(
                "SELECT name FROM user_groups WHERE is_admin=1").fetchall()]
            db.execute("UPDATE local_users SET enabled=0")
            db.execute("UPDATE user_sources SET last_is_admin=0 WHERE last_is_admin=1")
            db.execute("UPDATE user_groups SET is_admin=0 WHERE is_admin=1")
            db.execute("DELETE FROM settings WHERE key IN (?,?)",
                       (owner.OWNER_KEY, owner.TOKEN_KEY))
            db.commit()
        self.addCleanup(self._restore)

    def _restore(self):
        with portal.app.app_context():
            db = portal.get_db()
            for u in (OWNER, OTHER_ADMIN):
                for t in ('local_users', 'user_sources', 'user_sessions', 'audit_log',
                          'login_attempts'):
                    col = 'key' if t == 'login_attempts' else 'username'
                    db.execute(f"DELETE FROM {t} WHERE {col} LIKE ?", (f'%{u}%',))
            db.execute("DELETE FROM login_attempts WHERE key LIKE '%|setup'")
            db.execute("DELETE FROM settings WHERE key IN (?,?)",
                       (owner.OWNER_KEY, owner.TOKEN_KEY))
            for i in self._admins:
                db.execute("UPDATE local_users SET enabled=1 WHERE id=?", (i,))
            for u in self._directory:
                db.execute("UPDATE user_sources SET last_is_admin=1 WHERE username=?", (u,))
            for g in self._groups:
                db.execute("UPDATE user_groups SET is_admin=1 WHERE name=?", (g,))
            db.commit()

    def _client(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['csrf'] = 'tok'
        return c

    def _setup(self, c, **body):
        return c.post('/api/setup', json=body,
                      headers={'X-CSRFToken': 'tok', 'Cf-Connecting-Ip': '82.67.54.190'})

    def _token(self):
        with portal.app.app_context():
            return owner.setup_token()


class SetupTest(BaseSetup):

    def test_fresh_instance_offers_creation(self):
        r = portal.app.test_client().get('/api/config')
        self.assertTrue(r.get_json()['setup_required'])
        self.assertTrue(self._token())

    def test_token_required(self):
        self._token()
        r = self._setup(self._client(), token='faux', username=OWNER, password=MDP)
        self.assertEqual(r.status_code, 403)
        with portal.app.app_context():
            self.assertEqual(owner.owner(), '')

    def test_password_subject_to_policy(self):
        r = self._setup(self._client(), token=self._token(), username=OWNER, password='12345678')
        self.assertEqual(r.status_code, 400)

    def test_owner_creation_then_setup_closed(self):
        token = self._token()
        c = self._client()
        r = self._setup(c, token=token, username=OWNER, fullname='Owner', password=MDP)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertEqual(s['username'], OWNER)
            self.assertTrue(s['is_admin'])
        with portal.app.app_context():
            self.assertEqual(owner.owner(), OWNER)
            self.assertFalse(owner.setup_required())
            self.assertIsNone(owner.setup_token(), "the token is single-use")
        self.assertFalse(portal.app.test_client().get('/api/config').get_json()['setup_required'])
        r = self._setup(self._client(), token=token, username='ztest-second', password=MDP)
        self.assertEqual(r.status_code, 409)

    def test_existing_admin_closes_setup(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO local_users (username, password_hash, is_admin, enabled, "
                       "created_at) VALUES (?, 'x', 1, 1, ?)", (OTHER_ADMIN, time.time()))
            db.commit()
            self.assertFalse(owner.setup_required())
            self.assertIsNone(owner.setup_token())


class ProtectionTest(BaseSetup):

    def setUp(self):
        super().setUp()
        self.assertEqual(self._setup(self._client(), token=self._token(), username=OWNER,
                                     password=MDP).status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO local_users (username, password_hash, is_admin, enabled, "
                       "created_at) VALUES (?, 'x', 1, 1, ?)", (OTHER_ADMIN, time.time()))
            db.commit()
            self.uid = db.execute("SELECT id FROM local_users WHERE username=?",
                                  (OWNER,)).fetchone()[0]

    def _admin(self, username):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s.update(csrf='tok', username=username, is_admin=True, auth_at=int(time.time()))
        return c

    def _post(self, c, url, data=None):
        return c.post(url, data=data or {}, headers={'X-CSRFToken': 'tok'})

    def test_other_admin_cannot_touch_owner(self):
        other = self._admin(OTHER_ADMIN)
        for url, data in ((f'/admin/users/update/{self.uid}', {'is_admin': '0'}),
                          (f'/admin/users/update/{self.uid}', {'enabled': '0'}),
                          (f'/admin/users/delete/{self.uid}', {'confirm': 'DELETE'}),
                          (f'/admin/users/{OWNER}/block', {'reason': 'x'})):
            r = self._post(other, url, data)
            self.assertEqual(r.status_code, 403, url)
        with portal.app.app_context():
            row = portal.get_db().execute("SELECT is_admin, enabled FROM local_users WHERE id=?",
                                          (self.uid,)).fetchone()
            self.assertEqual((row['is_admin'], row['enabled']), (1, 1))

    def test_owner_edits_self(self):
        r = self._post(self._admin(OWNER), f'/admin/users/update/{self.uid}', {'fullname': 'Moi'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_list_flags_owner(self):
        from unittest import mock
        with mock.patch('admin_routes.directory_states_for', return_value=None):
            users = {u['username']: u for u in
                     self._admin(OWNER).get('/api/admin/users').get_json()['users']}
        self.assertTrue(users[OWNER]['owner'])
        self.assertFalse(users[OTHER_ADMIN]['owner'])


if __name__ == '__main__':
    unittest.main()


class DirectoryOwnerTest(BaseSetup):
    """The owner can be a directory (SSO/LDAP) account: no local row."""

    SSO = 'ztest-owner-sso'

    def setUp(self):
        super().setUp()
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT OR REPLACE INTO user_sources (username, sources, fullname, "
                       "last_source, last_seen, last_is_admin) VALUES (?,?,?,?,?,0)",
                       (self.SSO, 'sso', self.SSO, 'sso', '2026-10-11'))
            db.execute("INSERT INTO local_users (username, password_hash, is_admin, enabled, "
                       "created_at) VALUES (?, 'x', 1, 1, ?)", (OTHER_ADMIN, time.time()))
            db.commit()
            portal.set_setting(owner.OWNER_KEY, self.SSO)

        def _menage():
            with portal.app.app_context():
                db = portal.get_db()
                db.execute("DELETE FROM user_sources WHERE username=?", (self.SSO,))
                db.commit()
        self.addCleanup(_menage)

    def test_always_admin_even_removed_from_group(self):
        import auth
        with portal.app.app_context():
            self.assertEqual(auth.account_state(self.SSO), (True, True, None))
            self.assertTrue(auth.is_admin_username(self.SSO))

    def test_other_admins_cannot_purge_cut_or_cap(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s.update(csrf='tok', username=OTHER_ADMIN, is_admin=True, auth_at=int(time.time()))
        for url, data in ((f'/admin/users/{self.SSO}/purge', {'confirm': 'DELETE'}),
                          (f'/admin/users/{self.SSO}/revoke-sessions', {}),
                          (f'/admin/users/{self.SSO}/budget/set', {'budget': '10'}),
                          (f'/admin/users/{self.SSO}/block', {'reason': 'x'})):
            r = c.post(url, data=data, headers={'X-CSRFToken': 'tok'})
            self.assertEqual(r.status_code, 403, url)
