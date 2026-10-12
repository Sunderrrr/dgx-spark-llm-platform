"""Groups brought down from LDAP and SSO, merged by name (directory_groups.py).

What is locked here: a group is ONE row whatever brought it (LDAP DN, SSO
claim, local group of that name, any case); its admin right and quota reach
its directory members at login and as soon as an admin changes it; and an
admin decision on an account (budget set by hand, grant running) is never
overwritten by a group quota.
"""
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import app as portal
import directory_groups as dg

ACCOUNT = 'ztest-annuaire-g'
OTHER = 'ztest-annuaire-h'
GROUPS = ('ztest-famille', 'ztest-labo', 'ztest-admins')


class GroupsBase(unittest.TestCase):

    def setUp(self):
        portal.app.config['TESTING'] = True
        self._purge()
        self.addCleanup(self._purge)
        with portal.app.app_context():
            db = portal.get_db()
            for u in (ACCOUNT, OTHER):
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
            for u in (ACCOUNT, OTHER):
                for t in ('user_sources', 'user_directory_groups', 'budget_grants', 'local_users'):
                    db.execute(f"DELETE FROM {t} WHERE username=?", (u,))
            for g in GROUPS:
                db.execute("DELETE FROM user_groups WHERE lower(name)=lower(?)", (g,))
            db.commit()

    def _group(self, name):
        return portal.get_db().execute(
            "SELECT * FROM user_groups WHERE lower(name)=lower(?)", (name,)).fetchone()


class FusionTest(GroupsBase):

    def test_same_ldap_and_sso_group_merged(self):
        with portal.app.app_context():
            dg.import_groups(ACCOUNT, {
                'ldap': ['cn=ztest-famille,ou=groups,dc=example,dc=org'],
                'sso': ['ZTEST-Famille', 'ztest-labo']})
            rows = portal.get_db().execute(
                "SELECT name, sources FROM user_groups WHERE lower(name) IN (?,?)",
                ('ztest-famille', 'ztest-labo')).fetchall()
            self.assertEqual(len(rows), 2, "one row per group, whatever the case")
            self.assertEqual(self._group('ztest-famille')['sources'], 'ldap,sso')
            self.assertEqual([g['name'] for g in dg.groups_of(ACCOUNT)],
                             ['ztest-famille', 'ztest-labo'])

    def test_merge_with_local_group_of_same_name(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at, sources) "
                       "VALUES ('ztest-Labo', 5000, 0, '2026-10-11', 'local')")
            db.commit()
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-labo,ou=groups,dc=x']})
            self.assertEqual(self._group('ztest-labo')['name'], 'ztest-Labo')
            self.assertEqual(self._group('ztest-labo')['sources'], 'ldap,local')

    def test_absent_source_keeps_memberships(self):
        with portal.app.app_context():
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-famille,dc=x']})
            dg.import_groups(ACCOUNT, {'sso': ['ztest-labo']})
            self.assertEqual({g['name'] for g in dg.groups_of(ACCOUNT)},
                             {'ztest-famille', 'ztest-labo'})
            dg.import_groups(ACCOUNT, {'ldap': []})       # left the LDAP group
            self.assertEqual({g['name'] for g in dg.groups_of(ACCOUNT)}, {'ztest-labo'})


class RightsAndQuotaTest(GroupsBase):

    def _cote(self, name, budget=None, admin=0):
        db = portal.get_db()
        db.execute("UPDATE user_groups SET max_budget=?, is_admin=? WHERE lower(name)=lower(?)",
                   (budget, admin, name))
        db.commit()

    def test_admin_by_group_right_or_directory_admin_group(self):
        with portal.app.app_context():
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-labo,dc=x']})
            self.assertFalse(dg.rights_from_groups(ACCOUNT)[0])
            self._cote('ztest-labo', admin=1)
            self.assertTrue(dg.rights_from_groups(ACCOUNT)[0])
            dg.import_groups(OTHER, {'sso': ['ztest-admins']})
            with mock.patch.object(dg, 'setting', return_value='ztest-admins'):
                self.assertTrue(dg.rights_from_groups(OTHER)[0])

    def test_most_generous_quota_applies(self):
        with portal.app.app_context():
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-labo,dc=x', 'cn=ztest-famille,dc=x']})
            self._cote('ztest-labo', budget=1000)
            self._cote('ztest-famille', budget=3000)
            self.assertEqual(dg.apply_group_quota(ACCOUNT), 3000)
            self.assertEqual(self.maj.call_args[0][:2], (ACCOUNT, 3000.0))
            self.maj.reset_mock()
            self.assertIsNone(dg.apply_group_quota(ACCOUNT), "already applied")
            self.maj.assert_not_called()

    def test_leaving_group_restores_default(self):
        with portal.app.app_context():
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-labo,dc=x']})
            self._cote('ztest-labo', budget=1000)
            dg.apply_group_quota(ACCOUNT)
            dg.import_groups(ACCOUNT, {'ldap': []})
            default = float(portal.get_setting('default_key_budget', portal.KEY_BUDGET))
            self.assertEqual(dg.apply_group_quota(ACCOUNT), default)

    def test_without_group_quota_nothing_touched(self):
        with portal.app.app_context():
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-labo,dc=x']})
            self.assertIsNone(dg.apply_group_quota(ACCOUNT))
            self.maj.assert_not_called()

    def test_manual_budget_and_override_not_overwritten(self):
        with portal.app.app_context():
            db = portal.get_db()
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-labo,dc=x']})
            self._cote('ztest-labo', budget=1000)
            db.execute("UPDATE user_sources SET budget_manual=1 WHERE username=?", (ACCOUNT,))
            db.commit()
            self.assertIsNone(dg.apply_group_quota(ACCOUNT))
            db.execute("UPDATE user_sources SET budget_manual=0 WHERE username=?", (ACCOUNT,))
            db.execute("INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                       (ACCOUNT, 1, 2, (datetime.utcnow() + timedelta(hours=1)).isoformat(),
                        '2026-10-11', '2026-10-11'))
            db.commit()
            self.assertIsNone(dg.apply_group_quota(ACCOUNT))
            self.maj.assert_not_called()
            # An EXPIRED grant no longer holds the group quota back.
            db.execute("UPDATE budget_grants SET expires_at=? WHERE username=?",
                       ((datetime.utcnow() - timedelta(hours=1)).isoformat(), ACCOUNT))
            db.commit()
            self.assertEqual(dg.apply_group_quota(ACCOUNT), 1000)

    def test_local_account_not_affected(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO local_users (username, password_hash, created_at) VALUES (?,?,?)",
                       (ACCOUNT, 'x', '2026-10-11'))
            db.commit()
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-labo,dc=x']})
            self._cote('ztest-labo', budget=1000)
            self.assertIsNone(dg.apply_group_quota(ACCOUNT))


class RoutesTest(GroupsBase):

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

    def test_editing_group_updates_directory_members(self):
        with portal.app.app_context():
            dg.import_groups(ACCOUNT, {'sso': ['ztest-labo']})
        r = self._admin().post('/admin/groups/create',
                               data={'name': 'ZTEST-labo', 'max_budget': '7000', 'is_admin': '1'},
                               headers={'X-CSRFToken': 'tok'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with portal.app.app_context():
            rs = portal.get_db().execute("SELECT last_is_admin, budget_auto FROM user_sources "
                                         "WHERE username=?", (ACCOUNT,)).fetchone()
            self.assertEqual(rs['last_is_admin'], 1)
            self.assertEqual(rs['budget_auto'], 7000)
            self.assertEqual(self._group('ztest-labo')['name'], 'ztest-labo',
                             "edited in place, not duplicated under another case")

    def test_list_carries_directory_groups_sources_and_members(self):
        with portal.app.app_context():
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-famille,dc=x'], 'sso': ['ztest-famille']})
            dg.import_groups(OTHER, {'sso': ['ztest-famille']})
        with mock.patch('admin_routes.directory_states_for', return_value=None):
            body = self._admin().get('/api/admin/users').get_json()
        users = {u['username']: u for u in body['users']}
        self.assertEqual(users[ACCOUNT]['directory_groups'], ['ztest-famille'])
        g = {x['name']: x for x in body['groups']}['ztest-famille']
        self.assertEqual(g['sources'], ['ldap', 'sso'])
        self.assertEqual(g['members'], 2)


class SyncTest(GroupsBase):
    """The background directory read brings LDAP groups down between logins."""

    def test_add_and_remove_follow_directory(self):
        with portal.app.app_context():
            db = portal.get_db()
            dg.import_groups(OTHER, {'ldap': ['cn=ztest-admins,dc=x']})
            db.execute("UPDATE user_groups SET is_admin=1 WHERE name='ztest-admins'")
            db.commit()
            dg.sync_ldap_groups({ACCOUNT: ['ztest-admins', 'ztest-labo']})
            self.assertEqual({g['name'] for g in dg.groups_of(ACCOUNT)}, {'ztest-admins', 'ztest-labo'})
            self.assertEqual(db.execute("SELECT last_is_admin FROM user_sources WHERE username=?",
                                        (ACCOUNT,)).fetchone()[0], 1)
            # Removed from the admin group in the directory: demoted at once.
            dg.sync_ldap_groups({ACCOUNT: ['ztest-labo']})
            self.assertEqual(db.execute("SELECT last_is_admin FROM user_sources WHERE username=?",
                                        (ACCOUNT,)).fetchone()[0], 0)

    def test_account_absent_from_read_untouched(self):
        with portal.app.app_context():
            dg.import_groups(ACCOUNT, {'ldap': ['cn=ztest-labo,dc=x']})
            dg.sync_ldap_groups({})
            self.assertEqual([g['name'] for g in dg.groups_of(ACCOUNT)], ['ztest-labo'])


class LoginTest(GroupsBase):

    def test_ldap_login_imports_groups_and_admin_right(self):
        with portal.app.app_context():
            dg.import_groups(OTHER, {'ldap': ['cn=ztest-labo,dc=x']})
            db = portal.get_db()
            db.execute("UPDATE user_groups SET is_admin=1 WHERE name='ztest-labo'")
            db.commit()
        anon = portal.app.test_client()
        with anon.session_transaction() as s:
            s['csrf'] = 'tok'
        with mock.patch.object(portal, 'ldap_authenticate',
                               return_value=(True, False, 'Zed', ['cn=ztest-labo,ou=g,dc=x'])), \
             mock.patch.object(portal, '_webauthn_enabled', return_value=False):
            r = anon.post('/login', data={'username': ACCOUNT, 'password': 'pw'},
                          headers={'X-CSRFToken': 'tok', 'Cf-Connecting-Ip': '82.67.54.182'})
        self.assertIn(r.status_code, (200, 302), r.get_data(as_text=True)[:200])
        with anon.session_transaction() as s:
            self.assertTrue(s.get('is_admin'), "the group's admin right applies at login")
        with portal.app.app_context():
            self.assertEqual([g['name'] for g in dg.groups_of(ACCOUNT)], ['ztest-labo'])


if __name__ == '__main__':
    unittest.main()
