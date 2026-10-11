"""The instance's identity comes from its deployment, never from the code.

Name: Admin → Branding (settings.platform_name), « DGX » by default. Origins:
DGX_WEB / DGX_API in .env. These tests lock that no text a user or a mail
recipient sees carries a name or a domain baked into the repository.
"""
import importlib
import os
import time
import unittest
from unittest.mock import patch

import app as portal
try:                                     # discovery runs tests/ as top level
    from reglages_test import reglages
except ImportError:                      # single module: tests.test_x
    from tests.reglages_test import reglages
import config
import db
import parametres
import support


class PlatformIdentityTest(unittest.TestCase):

    def setUp(self):
        self.addCleanup(self._reset_name)

    def _set_name(self, value):
        with portal.app.app_context():
            db.set_setting('platform_name', value)

    def _reset_contact(self):
        with portal.app.app_context():
            parametres.definir('security_contact', '')

    def _reset_name(self):
        with portal.app.app_context():
            portal.get_db().execute("DELETE FROM settings WHERE key='platform_name'")
            portal.get_db().commit()

    def test_default_name_is_dgx(self):
        self._reset_name()
        with portal.app.app_context():
            self.assertEqual(db.platform_name(), 'DGX')

    def test_name_outside_app_context_falls_back(self):
        self.assertEqual(db.platform_name(), 'DGX')

    def test_support_prompts_follow_the_branding(self):
        self._set_name('Acme AI')
        with portal.app.app_context():
            prompt = support.support_text(support.SUPPORT_SYSTEM)
            faq = support.support_text(support.SUPPORT_FAQ)
        self.assertIn('Tu es Acme AI', prompt)
        self.assertIn('Acme AI', faq)
        self.assertNotIn('__PLATFORM__', prompt + faq)

    def test_security_txt_names_this_deployment_and_never_expires(self):
        with portal.app.app_context():
            parametres.definir('security_contact', 'sec@example.net')
        self.addCleanup(self._reset_contact)
        with patch.object(portal, 'PUBLIC_WEB_URL', 'https://ai.example.net'):
            r = portal.app.test_client().get('/.well-known/security.txt')
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Contact: mailto:sec@example.net', body)
        self.assertIn('Canonical: https://ai.example.net/.well-known/security.txt', body)
        expires = body.split('Expires: ', 1)[1].split()[0]
        self.assertGreater(expires, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))

    def test_security_txt_without_contact_is_404(self):
        with reglages(security_contact='', admin_email=''):
            r = portal.app.test_client().get('/.well-known/security.txt')
        self.assertEqual(r.status_code, 404)

    def test_origins_come_from_the_environment(self):
        env = {'DGX_WEB': 'https://ai.example.net/', 'DGX_API': 'https://api.example.net'}
        with patch.dict(os.environ, env, clear=False):
            for k in ('PUBLIC_API_URL', 'WEBAUTHN_ORIGIN', 'WEBAUTHN_RP_ID'):
                os.environ.pop(k, None)
            cfg = importlib.reload(config)
            try:
                self.assertEqual(cfg.PUBLIC_WEB_URL, 'https://ai.example.net')
                # One /v1, not the « …/v1/v1 » the compose file used to produce.
                self.assertEqual(cfg.PUBLIC_API_URL, 'https://api.example.net/v1')
                self.assertEqual(cfg.WEBAUTHN_ORIGIN, 'https://ai.example.net')
                self.assertEqual(cfg.WEBAUTHN_RP_ID, 'ai.example.net')
            finally:
                importlib.reload(config)


if __name__ == '__main__':
    unittest.main()
