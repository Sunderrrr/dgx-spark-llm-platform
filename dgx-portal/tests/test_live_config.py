"""Admin → Config is THE source of LDAP, SSO, SMTP and security settings.

Until 2026-10-11 the panel stored values that nothing read: only .env counted.
Then the database won over .env; now .env is not read at all for them, except
ONCE, to carry an older deployment's values into the database. These tests lock
that, that the values reach notification threads (no Flask context), that a
secret is written but never read back, and that the SSO client follows a
configuration change.
"""
import os
import threading
import time
import unittest
from unittest.mock import patch

import app as portal
import parametres
import sso_client

CLES = ('ldap_uri', 'ldap_base', 'smtp_host', 'smtp_user', 'smtp_pass', 'oidc_metadata_url',
        'oidc_client_id', 'oidc_client_secret', 'oidc_admin_group', 'ldap_bind_pw')

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
                parametres.definir(k, parametres.EFFACER)

    def test_env_n_est_plus_lu(self):
        with patch.dict(os.environ, {'LDAP_URI': 'ldap://from-env:389'}), \
             portal.app.app_context():
            self.assertEqual(parametres.ldap_config()['uri'], '')
        self._set(ldap_uri='ldap://from-admin:389')
        with portal.app.app_context():
            self.assertEqual(parametres.ldap_config()['uri'], 'ldap://from-admin:389')

    def test_import_unique_depuis_un_ancien_env(self):
        env = {'LDAP_URI': 'ldap://legacy:389', 'LDAP_BIND_PW': 'pw', 'LDAP_BASE': 'dc=legacy'}
        self._set(ldap_base='dc=already-set')
        with portal.app.app_context():
            marque = portal.get_setting('cfg_env_importe', '')
            portal.get_db().execute("DELETE FROM settings WHERE key='cfg_env_importe'")
            portal.get_db().commit()
            try:
                with patch.dict(os.environ, env):
                    importes = parametres.importer_env_une_fois()
                    self.assertIn('ldap_uri', importes)
                    self.assertIn('ldap_bind_pw', importes)
                    self.assertNotIn('ldap_base', importes, "a value set in Admin is kept")
                    c = parametres.ldap_config()
                    self.assertEqual((c['uri'], c['bind_pw'], c['base']),
                                     ('ldap://legacy:389', 'pw', 'dc=already-set'))
                    # Cleared by the operator: a second start does not bring it back.
                    parametres.definir('ldap_uri', '')
                    self.assertEqual(parametres.importer_env_une_fois(), [])
                    self.assertEqual(parametres.ldap_config()['uri'], '')
                # A key that joined the panel AFTER the first import still gets
                # its one import (an instance marked with the old plain flag).
                portal.set_setting('cfg_env_importe', '1')
                with patch.dict(os.environ, {'DISCORD_CLIENT_ID': '42', 'LDAP_URI': 'ldap://again'}):
                    self.assertEqual(parametres.importer_env_une_fois(), ['discord_client_id'])
                    self.assertEqual(parametres.discord_config()['client_id'], '42')
                    self.assertEqual(parametres.importer_env_une_fois(), [])
                parametres.definir('discord_client_id', parametres.EFFACER)
            finally:
                if marque:
                    portal.set_setting('cfg_env_importe', marque)

    def test_code_default_when_nothing_set(self):
        with portal.app.app_context():
            self.assertEqual(parametres.oidc_config()['admin_group'], 'admins')

    def test_un_secret_s_ecrit_mais_ne_se_relit_pas(self):
        self._set(smtp_pass='secret')
        with portal.app.app_context():
            etat = {e['nom']: e for e in parametres.etat_config()}
            self.assertTrue(etat['smtp_pass']['configure'])
            self.assertEqual(etat['smtp_pass']['valeur'], '', 'a secret never goes to the browser')
            # An empty secret field means « unchanged » (the browser never has
            # the value to send back); only EFFACER clears it.
            parametres.definir('smtp_pass', '')
            self.assertEqual(parametres.smtp_config()['password'], 'secret')
            parametres.definir('smtp_pass', parametres.EFFACER)
            self.assertEqual(parametres.smtp_config()['password'], '')

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


class DiscordConfigTest(unittest.TestCase):
    """Discord is set from Admin → Config too, read live."""

    def tearDown(self):
        with portal.app.app_context():
            for k in ('discord_webhook_url', 'discord_client_id', 'discord_client_secret'):
                parametres.definir(k, parametres.EFFACER)

    def test_liaison_active_avec_id_et_secret(self):
        with portal.app.app_context():
            self.assertFalse(parametres.discord_config()['link_enabled'])
            parametres.definir('discord_client_id', '123')
            parametres.definir('discord_client_secret', 's')
            self.assertTrue(parametres.discord_config()['link_enabled'])

    def test_le_webhook_vient_de_l_admin(self):
        # notify.notify_discord reads discord_config()['webhook'] (its sending
        # is covered by test_hf_search.NotifyDiscordTest).
        with portal.app.app_context():
            parametres.definir('discord_webhook_url', 'https://discord.invalid/hook')
            self.assertEqual(parametres.discord_config()['webhook'], 'https://discord.invalid/hook')
            etat = {e['nom']: e for e in parametres.etat_config()}
            self.assertEqual(etat['discord_webhook_url']['valeur'], '', "a webhook is a secret")


class ConfigTestRouteTest(unittest.TestCase):
    """Admin → Config « Tester »: each section answers {ok, message}, on the
    saved values, and says what is missing instead of failing blindly."""

    ADMIN = 'ztest-cfg-admin'

    def setUp(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM local_users WHERE username=?", (self.ADMIN,))
            db.execute("INSERT INTO local_users (username, password_hash, is_admin, enabled, "
                       "created_at) VALUES (?, 'x', 1, 1, '2026-10-12')", (self.ADMIN,))
            db.commit()
            self.avant = {k: portal.get_db().execute(
                "SELECT value FROM settings WHERE key=?", ('cfg_' + k,)).fetchone()
                for k in ('ldap_uri', 'oidc_metadata_url', 'smtp_host', 'discord_webhook_url')}
            for k in self.avant:
                parametres.definir(k, parametres.EFFACER)
        self.c = portal.app.test_client()
        with self.c.session_transaction() as s:
            s.update(csrf='tok', username=self.ADMIN, is_admin=True, auth_at=int(time.time()))

    def tearDown(self):
        with portal.app.app_context():
            for k, row in self.avant.items():
                parametres.definir(k, row['value'] if row else parametres.EFFACER)
            db = portal.get_db()
            db.execute("DELETE FROM local_users WHERE username=?", (self.ADMIN,))
            db.execute("DELETE FROM audit_log WHERE username=?", (self.ADMIN,))
            db.commit()

    def _test(self, partie):
        return self.c.post(f'/admin/config/test/{partie}', headers={'X-CSRFToken': 'tok'})

    def test_sections_non_configurees_le_disent(self):
        for partie, mot in (('ldap', 'URI'), ('sso', 'URL'), ('smtp', 'requis'),
                            ('discord', 'webhook')):
            r = self._test(partie)
            self.assertEqual(r.status_code, 200, partie)
            body = r.get_json()
            self.assertFalse(body['ok'], partie)
            self.assertIn(mot, body['message'], partie)
        self.assertEqual(self._test('autre').status_code, 404)

    def test_sso_lit_les_metadonnees(self):
        import sso_client
        with portal.app.app_context():
            parametres.definir('oidc_metadata_url', 'https://idp.example/.well-known/openid-configuration')
        md = {'issuer': 'https://idp.example', 'authorization_endpoint': 'a', 'token_endpoint': 't'}
        with patch.object(sso_client, '_metadata', return_value=md):
            body = self._test('sso').get_json()
        self.assertIn('https://idp.example', body['message'])

    def test_discord_publie_sur_le_webhook(self):
        import admin_routes
        with portal.app.app_context():
            parametres.definir('discord_webhook_url', 'https://discord.invalid/hook')
        with patch.object(admin_routes, 'notify_discord_test', return_value=True):
            self.assertTrue(self._test('discord').get_json()['ok'])

    def test_les_valeurs_par_defaut_et_les_suggestions(self):
        body = self.c.get('/admin/config').get_json()
        lignes = {l['nom']: l for l in body['config']}
        self.assertEqual(lignes['ldap_login_attr']['defaut'], 'cn')
        self.assertEqual(lignes['smtp_pass']['defaut'], '')
        self.assertTrue(body['suggestions']['oidc_redirect_uri'].endswith('/api/oauth2-redirect'))
