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
import proprietaire

OWNER = 'ztest-owner'
AUTRE_ADMIN = 'ztest-autre-admin'
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
            self._annuaire = [r[0] for r in db.execute(
                "SELECT username FROM user_sources WHERE last_is_admin=1").fetchall()]
            self._groupes = [r[0] for r in db.execute(
                "SELECT name FROM user_groups WHERE is_admin=1").fetchall()]
            db.execute("UPDATE local_users SET enabled=0")
            db.execute("UPDATE user_sources SET last_is_admin=0 WHERE last_is_admin=1")
            db.execute("UPDATE user_groups SET is_admin=0 WHERE is_admin=1")
            db.execute("DELETE FROM settings WHERE key IN (?,?)",
                       (proprietaire.CLE_PROPRIETAIRE, proprietaire.CLE_JETON))
            db.commit()
        self.addCleanup(self._restaure)

    def _restaure(self):
        with portal.app.app_context():
            db = portal.get_db()
            for u in (OWNER, AUTRE_ADMIN):
                for t in ('local_users', 'user_sources', 'user_sessions', 'audit_log',
                          'login_attempts'):
                    col = 'key' if t == 'login_attempts' else 'username'
                    db.execute(f"DELETE FROM {t} WHERE {col} LIKE ?", (f'%{u}%',))
            db.execute("DELETE FROM login_attempts WHERE key LIKE '%|setup'")
            db.execute("DELETE FROM settings WHERE key IN (?,?)",
                       (proprietaire.CLE_PROPRIETAIRE, proprietaire.CLE_JETON))
            for i in self._admins:
                db.execute("UPDATE local_users SET enabled=1 WHERE id=?", (i,))
            for u in self._annuaire:
                db.execute("UPDATE user_sources SET last_is_admin=1 WHERE username=?", (u,))
            for g in self._groupes:
                db.execute("UPDATE user_groups SET is_admin=1 WHERE name=?", (g,))
            db.commit()

    def _client(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['csrf'] = 'tok'
        return c

    def _setup(self, c, **corps):
        return c.post('/api/setup', json=corps,
                      headers={'X-CSRFToken': 'tok', 'Cf-Connecting-Ip': '82.67.54.190'})

    def _jeton(self):
        with portal.app.app_context():
            return proprietaire.jeton_setup()


class SetupTest(BaseSetup):

    def test_instance_neuve_propose_la_creation(self):
        r = portal.app.test_client().get('/api/config')
        self.assertTrue(r.get_json()['setup_required'])
        self.assertTrue(self._jeton())

    def test_jeton_obligatoire(self):
        self._jeton()
        r = self._setup(self._client(), token='faux', username=OWNER, password=MDP)
        self.assertEqual(r.status_code, 403)
        with portal.app.app_context():
            self.assertEqual(proprietaire.proprietaire(), '')

    def test_mot_de_passe_soumis_a_la_politique(self):
        r = self._setup(self._client(), token=self._jeton(), username=OWNER, password='12345678')
        self.assertEqual(r.status_code, 400)

    def test_creation_du_proprietaire_puis_setup_ferme(self):
        jeton = self._jeton()
        c = self._client()
        r = self._setup(c, token=jeton, username=OWNER, fullname='Owner', password=MDP)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertEqual(s['username'], OWNER)
            self.assertTrue(s['is_admin'])
        with portal.app.app_context():
            self.assertEqual(proprietaire.proprietaire(), OWNER)
            self.assertFalse(proprietaire.setup_requis())
            self.assertIsNone(proprietaire.jeton_setup(), "the token is single-use")
        self.assertFalse(portal.app.test_client().get('/api/config').get_json()['setup_required'])
        r = self._setup(self._client(), token=jeton, username='ztest-second', password=MDP)
        self.assertEqual(r.status_code, 409)

    def test_un_admin_existant_ferme_le_setup(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO local_users (username, password_hash, is_admin, enabled, "
                       "created_at) VALUES (?, 'x', 1, 1, ?)", (AUTRE_ADMIN, time.time()))
            db.commit()
            self.assertFalse(proprietaire.setup_requis())
            self.assertIsNone(proprietaire.jeton_setup())


class ProtectionTest(BaseSetup):

    def setUp(self):
        super().setUp()
        self.assertEqual(self._setup(self._client(), token=self._jeton(), username=OWNER,
                                     password=MDP).status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO local_users (username, password_hash, is_admin, enabled, "
                       "created_at) VALUES (?, 'x', 1, 1, ?)", (AUTRE_ADMIN, time.time()))
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

    def test_un_autre_admin_ne_peut_pas_toucher_au_proprietaire(self):
        autre = self._admin(AUTRE_ADMIN)
        for url, data in ((f'/admin/users/update/{self.uid}', {'is_admin': '0'}),
                          (f'/admin/users/update/{self.uid}', {'enabled': '0'}),
                          (f'/admin/users/delete/{self.uid}', {'confirm': 'DELETE'}),
                          (f'/admin/users/{OWNER}/block', {'reason': 'x'})):
            r = self._post(autre, url, data)
            self.assertEqual(r.status_code, 403, url)
        with portal.app.app_context():
            row = portal.get_db().execute("SELECT is_admin, enabled FROM local_users WHERE id=?",
                                          (self.uid,)).fetchone()
            self.assertEqual((row['is_admin'], row['enabled']), (1, 1))

    def test_le_proprietaire_se_modifie_lui_meme(self):
        r = self._post(self._admin(OWNER), f'/admin/users/update/{self.uid}', {'fullname': 'Moi'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_la_liste_signale_le_proprietaire(self):
        from unittest import mock
        with mock.patch('admin_routes.etats_annuaire', return_value=None):
            users = {u['username']: u for u in
                     self._admin(OWNER).get('/api/admin/users').get_json()['users']}
        self.assertTrue(users[OWNER]['owner'])
        self.assertFalse(users[AUTRE_ADMIN]['owner'])


if __name__ == '__main__':
    unittest.main()


class ProprietaireAnnuaireTest(BaseSetup):
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
                       "created_at) VALUES (?, 'x', 1, 1, ?)", (AUTRE_ADMIN, time.time()))
            db.commit()
            portal.set_setting(proprietaire.CLE_PROPRIETAIRE, self.SSO)

        def _menage():
            with portal.app.app_context():
                db = portal.get_db()
                db.execute("DELETE FROM user_sources WHERE username=?", (self.SSO,))
                db.commit()
        self.addCleanup(_menage)

    def test_toujours_admin_meme_retire_du_groupe(self):
        import auth
        with portal.app.app_context():
            self.assertEqual(auth.etat_compte(self.SSO), (True, True, None))
            self.assertTrue(auth.is_admin_username(self.SSO))

    def test_les_autres_admins_ne_peuvent_ni_purger_ni_couper_ni_plafonner(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s.update(csrf='tok', username=AUTRE_ADMIN, is_admin=True, auth_at=int(time.time()))
        for url, data in ((f'/admin/users/{self.SSO}/purge', {'confirm': 'DELETE'}),
                          (f'/admin/users/{self.SSO}/revoke-sessions', {}),
                          (f'/admin/users/{self.SSO}/budget/set', {'budget': '10'}),
                          (f'/admin/users/{self.SSO}/block', {'reason': 'x'})):
            r = c.post(url, data=data, headers={'X-CSRFToken': 'tok'})
            self.assertEqual(r.status_code, 403, url)
