"""The admin role of an SSO/LDAP account never touches the directory per request.

Measured on 2026-10-11: one LDAP bind takes 3.6 to 5 s on the directory side.
The role used to be re-checked live (60 s cache per gunicorn worker), so the
first home page load after login stayed half-empty for 5 to 10 s. The role now
comes from the last login (SSO token group / directory entry), stored in
user_sources.last_is_admin. These tests lock that: no LDAP on the request
path, one source for sessions and API keys, the admin group is configurable.
"""
import os
import time
import unittest
from unittest.mock import patch

import app as portal
try:                                     # discovery runs tests/ as top level
    from settings_helper import live_settings
except ImportError:                      # single module: tests.test_x
    from tests.settings_helper import live_settings
import auth

USER = 'ztest-sso-role'


def _no_ldap(*args, **kwargs):
    raise AssertionError('the directory was queried on the request path')


class AdminRoleTest(unittest.TestCase):

    def setUp(self):
        self.ldap = patch.object(auth, 'Connection', side_effect=_no_ldap)
        self.ldap.start()
        self.addCleanup(self.ldap.stop)
        self.addCleanup(self._purge)

    def _record(self, is_admin):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT OR REPLACE INTO user_sources (username, sources, last_source, "
                       "last_is_admin) VALUES (?, 'sso', 'sso', ?)", (USER, int(is_admin)))
            db.commit()

    def _purge(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM user_sources WHERE username=?", (USER,))
            db.execute("DELETE FROM blocked_users WHERE username=?", (USER,))
            db.commit()

    def test_session_role_comes_from_last_login_without_ldap(self):
        self._record(True)
        with live_settings(ldap_bind_dn='cn=svc', ldap_bind_pw='x'), portal.app.app_context():
            start = time.perf_counter()
            self.assertEqual(auth.account_state(USER), (True, True, None))
            self.assertLess(time.perf_counter() - start, 0.2)

    def test_unknown_account_has_no_opinion(self):
        with portal.app.app_context():
            self.assertEqual(auth.account_state(USER), (True, None, None))

    def test_api_key_and_session_share_one_source(self):
        self._record(True)
        with portal.app.app_context():
            self.assertTrue(auth.is_admin_username(USER))
        self._record(False)
        with portal.app.app_context():
            self.assertFalse(auth.is_admin_username(USER))
            self.assertEqual(auth.account_state(USER)[1], False)

    def test_blocked_account_is_never_admin(self):
        self._record(True)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO blocked_users (username, reason, blocked_by, blocked_at) "
                       "VALUES (?, 'test', 'test', '2026-10-11')", (USER,))
            db.commit()
            self.assertFalse(auth.is_admin_username(USER))

    def test_admin_group_is_configurable(self):
        with live_settings(oidc_admin_group='dgx-admins'), portal.app.app_context():
            self.assertTrue(auth._is_admin_group('cn=dgx-admins,ou=groups,dc=example,dc=org'))
            self.assertFalse(auth._is_admin_group('cn=dgx-admins-ro,ou=groups,dc=example,dc=org'))
            self.assertFalse(auth._is_admin_group('cn=admins,ou=groups,dc=example,dc=org'))
        with patch('auth.setting', return_value=''):
            self.assertFalse(auth._is_admin_group('cn=,ou=groups'))


if __name__ == '__main__':
    unittest.main()
