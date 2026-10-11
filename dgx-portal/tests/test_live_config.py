"""Admin → Config is the live source of LDAP, SSO, SMTP and security settings.

Until 2026-10-11 the panel stored values that nothing read: only .env counted,
and a change needed a redeploy. These tests lock the precedence (Admin, then
.env, then the code default), that it holds in notification threads (no Flask
context), and that the SSO client follows a configuration change.
"""
import os
import threading
import unittest
from unittest.mock import patch

import app as portal
import parametres
import sso_client

CLES = ('ldap_uri', 'smtp_host', 'smtp_user', 'smtp_pass', 'oidc_metadata_url',
        'oidc_client_id', 'oidc_client_secret', 'oidc_admin_group')

METADATA = {
    'issuer': 'https://sso.example.net',
    'authorization_endpoint': 'https://sso.example.net/authorize',
    'token_endpoint': 'https://sso.example.net/token',
    'jwks_uri': 'https://sso.example.net/jwks',
}


class LiveConfigTest(unittest.TestCase):

    def setUp(self):
        self.addCleanup(self._purge)

    def _set(self, **values):
        with portal.app.app_context():
            for k, v in values.items():
                parametres.definir(k, v)

    def _purge(self):
        with portal.app.app_context():
            for k in CLES:
                parametres.definir(k, '')

    def test_admin_value_wins_over_env(self):
        with patch.dict(os.environ, {'LDAP_URI': 'ldap://from-env:389'}):
            with portal.app.app_context():
                self.assertEqual(parametres.ldap_config()['uri'], 'ldap://from-env:389')
            self._set(ldap_uri='ldap://from-admin:389')
            with portal.app.app_context():
                self.assertEqual(parametres.ldap_config()['uri'], 'ldap://from-admin:389')

    def test_code_default_when_nothing_set(self):
        with patch.dict(os.environ, {'OIDC_ADMIN_GROUP': ''}), portal.app.app_context():
            self.assertEqual(parametres.oidc_config()['admin_group'], 'admins')

    def test_smtp_password_env_name_is_the_real_one(self):
        with patch.dict(os.environ, {'SMTP_PASSWORD': 'secret'}), portal.app.app_context():
            etat = {e['nom']: e for e in parametres.etat_config()}
        self.assertTrue(etat['smtp_pass']['configure'])
        self.assertEqual(etat['smtp_pass']['valeur'], '', 'a secret never goes to the browser')

    def test_admin_value_reaches_notification_threads(self):
        self._set(smtp_host='smtp.from-admin.example')
        vu = []
        t = threading.Thread(target=lambda: vu.append(parametres.smtp_config()['host']))
        t.start()
        t.join()
        self.assertEqual(vu, ['smtp.from-admin.example'])

    def test_sso_client_follows_a_configuration_change(self):
        with patch.dict(os.environ, {'OIDC_METADATA_URL': '', 'OIDC_CLIENT_ID': '',
                                     'OIDC_CLIENT_SECRET': ''}), \
             patch.object(sso_client, '_metadata', return_value=dict(METADATA)):
            with portal.app.test_request_context():
                self.assertIsNone(sso_client.sso_client(portal.app))
            self._set(oidc_metadata_url='https://sso.example.net/.well-known/openid-configuration',
                      oidc_client_id='first', oidc_client_secret='s')
            with portal.app.test_request_context():
                self.assertEqual(sso_client.sso_client(portal.app).client_id, 'first')
            self._set(oidc_client_id='second')
            with portal.app.test_request_context():
                self.assertEqual(sso_client.sso_client(portal.app).client_id, 'second')
                self.assertTrue(portal.oidc_config()['enabled'])


if __name__ == '__main__':
    unittest.main()
