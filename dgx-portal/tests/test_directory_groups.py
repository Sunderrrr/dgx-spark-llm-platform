"""Groups brought down from LDAP and SSO, merged by name (directory_groups.py).

What is locked here: a group is ONE row whatever brought it (LDAP DN, SSO
claim, local group of that name, any case); its admin right and quota reach
its directory members at login and as soon as an admin changes it; and an
admin decision on an account (budget set by hand, grant running) is never
overwritten by a group quota.
"""
import time
import unittest
from unittest import mock

import app as portal
import directory_groups as dg

COMPTE = 'ztest-annuaire-g'
AUTRE = 'ztest-annuaire-h'
GROUPES = ('ztest-famille', 'ztest-labo', 'ztest-admins')


class BaseGroupes(unittest.TestCase):

    def setUp(self):
        portal.app.config['TESTING'] = True
        self._purge()
        self.addCleanup(self._purge)
        with portal.app.app_context():
            db = portal.get_db()
            for u in (COMPTE, AUTRE):
                db.execute("INSERT INTO user_sources (username, sources, fullname, last_source, "
                           "last_seen, last_is_admin) VALUES (?,?,?,?,?,?)",
                           (u, 'ldap', u, 'ldap', '2026-10-11T10:00:00', 0))
            db.commit()
        p = mock.patch.object(dg, 'litellm_update_user_budget', return_value=True)
        self.maj = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(dg, '_ensure_litellm_user')
        p.start()
        self.addCleanup(p.stop)

    def _purge(self):
        with portal.app.app_context():
            db = portal.get_db()
            for u in (COMPTE, AUTRE):
                for t in ('user_sources', 'user_directory_groups', 'budget_grants', 'local_users'):
                    db.execute(f"DELETE FROM {t} WHERE username=?", (u,))
            for g in GROUPES:
                db.execute("DELETE FROM user_groups WHERE lower(name)=lower(?)", (g,))
            db.commit()

    def _groupe(self, nom):
        return portal.get_db().execute(
            "SELECT * FROM user_groups WHERE lower(name)=lower(?)", (nom,)).fetchone()


class FusionTest(BaseGroupes):

    def test_meme_groupe_ldap_et_sso_fusionne(self):
        with portal.app.app_context():
            dg.importer_groupes(COMPTE, {
                'ldap': ['cn=ztest-famille,ou=groups,dc=example,dc=org'],
                'sso': ['ZTEST-Famille', 'ztest-labo']})
            rows = portal.get_db().execute(
                "SELECT name, sources FROM user_groups WHERE lower(name) IN (?,?)",
                ('ztest-famille', 'ztest-labo')).fetchall()
            self.assertEqual(len(rows), 2, "one row per group, whatever the case")
            self.assertEqual(self._groupe('ztest-famille')['sources'], 'ldap,sso')
            self.assertEqual([g['name'] for g in dg.groupes_de(COMPTE)],
                             ['ztest-famille', 'ztest-labo'])

    def test_fusion_avec_un_groupe_local_du_meme_nom(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at, sources) "
                       "VALUES ('ztest-Labo', 5000, 0, '2026-10-11', 'local')")
            db.commit()
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-labo,ou=groups,dc=x']})
            self.assertEqual(self._groupe('ztest-labo')['name'], 'ztest-Labo')
            self.assertEqual(self._groupe('ztest-labo')['sources'], 'ldap,local')

    def test_une_source_absente_garde_ses_appartenances(self):
        with portal.app.app_context():
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-famille,dc=x']})
            dg.importer_groupes(COMPTE, {'sso': ['ztest-labo']})
            self.assertEqual({g['name'] for g in dg.groupes_de(COMPTE)},
                             {'ztest-famille', 'ztest-labo'})
            dg.importer_groupes(COMPTE, {'ldap': []})       # left the LDAP group
            self.assertEqual({g['name'] for g in dg.groupes_de(COMPTE)}, {'ztest-labo'})


class DroitsEtQuotaTest(BaseGroupes):

    def _cote(self, nom, budget=None, admin=0):
        db = portal.get_db()
        db.execute("UPDATE user_groups SET max_budget=?, is_admin=? WHERE lower(name)=lower(?)",
                   (budget, admin, nom))
        db.commit()

    def test_admin_par_droit_de_groupe_ou_groupe_admin_de_l_annuaire(self):
        with portal.app.app_context():
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-labo,dc=x']})
            self.assertFalse(dg.droits_par_groupes(COMPTE)[0])
            self._cote('ztest-labo', admin=1)
            self.assertTrue(dg.droits_par_groupes(COMPTE)[0])
            dg.importer_groupes(AUTRE, {'sso': ['ztest-admins']})
            with mock.patch.object(dg, 'parametre', return_value='ztest-admins'):
                self.assertTrue(dg.droits_par_groupes(AUTRE)[0])

    def test_le_quota_le_plus_genereux_s_applique(self):
        with portal.app.app_context():
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-labo,dc=x', 'cn=ztest-famille,dc=x']})
            self._cote('ztest-labo', budget=1000)
            self._cote('ztest-famille', budget=3000)
            self.assertEqual(dg.appliquer_quota_groupes(COMPTE), 3000)
            self.assertEqual(self.maj.call_args[0][:2], (COMPTE, 3000.0))
            self.maj.reset_mock()
            self.assertIsNone(dg.appliquer_quota_groupes(COMPTE), "already applied")
            self.maj.assert_not_called()

    def test_quitter_le_groupe_rend_le_defaut(self):
        with portal.app.app_context():
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-labo,dc=x']})
            self._cote('ztest-labo', budget=1000)
            dg.appliquer_quota_groupes(COMPTE)
            dg.importer_groupes(COMPTE, {'ldap': []})
            defaut = float(portal.get_setting('default_key_budget', portal.KEY_BUDGET))
            self.assertEqual(dg.appliquer_quota_groupes(COMPTE), defaut)

    def test_sans_quota_de_groupe_rien_n_est_touche(self):
        with portal.app.app_context():
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-labo,dc=x']})
            self.assertIsNone(dg.appliquer_quota_groupes(COMPTE))
            self.maj.assert_not_called()

    def test_budget_manuel_et_derogation_ne_sont_pas_ecrases(self):
        with portal.app.app_context():
            db = portal.get_db()
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-labo,dc=x']})
            self._cote('ztest-labo', budget=1000)
            db.execute("UPDATE user_sources SET budget_manual=1 WHERE username=?", (COMPTE,))
            db.commit()
            self.assertIsNone(dg.appliquer_quota_groupes(COMPTE))
            db.execute("UPDATE user_sources SET budget_manual=0 WHERE username=?", (COMPTE,))
            db.execute("INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                       (COMPTE, 1, 2, time.time() + 3600, '2026-10-11', '2026-10-11'))
            db.commit()
            self.assertIsNone(dg.appliquer_quota_groupes(COMPTE))
            self.maj.assert_not_called()

    def test_un_compte_local_n_est_pas_concerne(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO local_users (username, password_hash, created_at) VALUES (?,?,?)",
                       (COMPTE, 'x', '2026-10-11'))
            db.commit()
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-labo,dc=x']})
            self._cote('ztest-labo', budget=1000)
            self.assertIsNone(dg.appliquer_quota_groupes(COMPTE))


class RoutesTest(BaseGroupes):

    ADMIN = 'ztest-admin-g'

    def setUp(self):
        super().setUp()
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM local_users WHERE username=?", (self.ADMIN,))
            db.execute("INSERT INTO local_users (username, password_hash, fullname, is_admin, "
                       "enabled, created_at) VALUES (?,?,?,1,1,?)",
                       (self.ADMIN, 'x', self.ADMIN, time.time()))
            db.commit()

        def _menage():
            with portal.app.app_context():
                db = portal.get_db()
                for t in ('local_users', 'audit_log', 'user_sources'):
                    db.execute(f"DELETE FROM {t} WHERE username=?", (self.ADMIN,))
                db.commit()
        self.addCleanup(_menage)

    def _admin(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s.update(username=self.ADMIN, is_admin=True, auth_at=int(time.time()), csrf='tok')
        return c

    def test_modifier_un_groupe_met_a_jour_ses_membres_de_l_annuaire(self):
        with portal.app.app_context():
            dg.importer_groupes(COMPTE, {'sso': ['ztest-labo']})
        r = self._admin().post('/admin/groups/create',
                               data={'name': 'ZTEST-labo', 'max_budget': '7000', 'is_admin': '1'},
                               headers={'X-CSRFToken': 'tok'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with portal.app.app_context():
            rs = portal.get_db().execute("SELECT last_is_admin, budget_auto FROM user_sources "
                                         "WHERE username=?", (COMPTE,)).fetchone()
            self.assertEqual(rs['last_is_admin'], 1)
            self.assertEqual(rs['budget_auto'], 7000)
            self.assertEqual(self._groupe('ztest-labo')['name'], 'ztest-labo',
                             "edited in place, not duplicated under another case")

    def test_liste_porte_groupes_annuaire_sources_et_membres(self):
        with portal.app.app_context():
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-famille,dc=x'], 'sso': ['ztest-famille']})
            dg.importer_groupes(AUTRE, {'sso': ['ztest-famille']})
        with mock.patch('admin_routes.etats_annuaire', return_value=None):
            body = self._admin().get('/api/admin/users').get_json()
        users = {u['username']: u for u in body['users']}
        self.assertEqual(users[COMPTE]['directory_groups'], ['ztest-famille'])
        g = {x['name']: x for x in body['groups']}['ztest-famille']
        self.assertEqual(g['sources'], ['ldap', 'sso'])
        self.assertEqual(g['members'], 2)


class SynchronisationTest(BaseGroupes):
    """The background directory read brings LDAP groups down between logins."""

    def test_ajout_et_retrait_suivent_l_annuaire(self):
        with portal.app.app_context():
            db = portal.get_db()
            dg.importer_groupes(AUTRE, {'ldap': ['cn=ztest-admins,dc=x']})
            db.execute("UPDATE user_groups SET is_admin=1 WHERE name='ztest-admins'")
            db.commit()
            dg.synchroniser_groupes_ldap({COMPTE: ['ztest-admins', 'ztest-labo']})
            self.assertEqual({g['name'] for g in dg.groupes_de(COMPTE)}, {'ztest-admins', 'ztest-labo'})
            self.assertEqual(db.execute("SELECT last_is_admin FROM user_sources WHERE username=?",
                                        (COMPTE,)).fetchone()[0], 1)
            # Removed from the admin group in the directory: demoted at once.
            dg.synchroniser_groupes_ldap({COMPTE: ['ztest-labo']})
            self.assertEqual(db.execute("SELECT last_is_admin FROM user_sources WHERE username=?",
                                        (COMPTE,)).fetchone()[0], 0)

    def test_un_compte_absent_de_la_lecture_n_est_pas_touche(self):
        with portal.app.app_context():
            dg.importer_groupes(COMPTE, {'ldap': ['cn=ztest-labo,dc=x']})
            dg.synchroniser_groupes_ldap({})
            self.assertEqual([g['name'] for g in dg.groupes_de(COMPTE)], ['ztest-labo'])


class ConnexionTest(BaseGroupes):

    def test_connexion_ldap_importe_les_groupes_et_le_droit_admin(self):
        with portal.app.app_context():
            dg.importer_groupes(AUTRE, {'ldap': ['cn=ztest-labo,dc=x']})
            db = portal.get_db()
            db.execute("UPDATE user_groups SET is_admin=1 WHERE name='ztest-labo'")
            db.commit()
        anon = portal.app.test_client()
        with anon.session_transaction() as s:
            s['csrf'] = 'tok'
        with mock.patch.object(portal, 'ldap_authenticate',
                               return_value=(True, False, 'Zed', ['cn=ztest-labo,ou=g,dc=x'])), \
             mock.patch.object(portal, '_webauthn_enabled', return_value=False):
            r = anon.post('/login', data={'username': COMPTE, 'password': 'pw'},
                          headers={'X-CSRFToken': 'tok', 'Cf-Connecting-Ip': '82.67.54.182'})
        self.assertIn(r.status_code, (200, 302), r.get_data(as_text=True)[:200])
        with anon.session_transaction() as s:
            self.assertTrue(s.get('is_admin'), "the group's admin right applies at login")
        with portal.app.app_context():
            self.assertEqual([g['name'] for g in dg.groupes_de(COMPTE)], ['ztest-labo'])


if __name__ == '__main__':
    unittest.main()
