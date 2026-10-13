"""SMTP mails: maintenance button + « demander un modèle » button (media pages).

We do NOT test the sending (requires a real SMTP, never in the test image):
we test that the notification functions are guarded (no-op without SMTP
config), and the logic of the /api/model/request route (login guard, valid
category, refusal if a model of the category is already loaded, notification
emitted otherwise).
"""
import os
import time
import unittest
from unittest.mock import patch

import app as portal
from notify import notify_maintenance_email, notify_media_request_email
try:                                     # discovery runs tests/ as top level
    from settings_helper import live_settings
except ImportError:                      # single module: tests.test_x
    from tests.settings_helper import live_settings


class NotifyGuardsTest(unittest.TestCase):
    """Without SMTP config (test image), the sends are safe no-ops."""

    # The SMTP settings and the alert recipient live in Admin → Config.
    NO_SMTP = dict(smtp_host='', smtp_user='', smtp_pass='', admin_email='')

    def test_maintenance_email_noop_without_config(self):
        with live_settings(**self.NO_SMTP), portal.app.app_context():
            self.assertIs(notify_maintenance_email(True, "demo", "Demo"), False)

    def test_media_request_email_noop_without_config(self):
        with live_settings(**self.NO_SMTP), portal.app.app_context():
            self.assertIs(notify_media_request_email("image", "demo", "Demo"), False)


class MediaRequestRouteTest(unittest.TestCase):
    def setUp(self):
        portal.app.config["TESTING"] = True
        self.calls = []
        self._orig_cat = portal._media_category_running
        self._orig_notify = portal.notify_media_request_email
        portal._media_category_running = lambda cat: False
        portal.notify_media_request_email = self._record
        self._clean_cooldown()

    def tearDown(self):
        portal._media_category_running = self._orig_cat
        portal.notify_media_request_email = self._orig_notify
        self._clean_cooldown()

    def _clean_cooldown(self):
        with portal.app.app_context():
            portal.get_db().execute("DELETE FROM media_request_cooldown")
            portal.get_db().commit()

    def _record(self, category, username, fullname):
        self.calls.append((category, username, fullname))

    def _client(self, username):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"
            s["username"] = username
            s["auth_at"] = int(time.time())
        return c

    def test_requires_session(self):
        # Session with valid CSRF but NO username => login_required rejects.
        # (With no session at all, the CSRF guard would answer 400 before login_required.)
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"
        r = c.post("/api/model/request", json={"category": "image"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.calls, [])

    def test_unknown_category_refused(self):
        c = self._client("demo")
        r = c.post("/api/model/request", json={"category": "doodle"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_request_sent_if_no_model(self):
        c = self._client("demo")
        r = c.post("/api/model/request", json={"category": "video"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        # The test session carries no fullname -> empty string.
        self.assertEqual(self.calls, [("video", "demo", "")])

    def test_refused_if_model_already_loaded(self):
        portal._media_category_running = lambda cat: cat == "image"
        c = self._client("demo")
        r = c.post("/api/model/request", json={"category": "image"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.calls, [])

    def test_cooldown_refuse_repetition(self):
        # Anti-spam: two close requests on the same (user, category) →
        # the 2nd is refused (429) regardless of the frontend lock
        # (reset on navigation).
        c = self._client("demo")
        r1 = c.post("/api/model/request", json={"category": "video"},
                    headers={"X-CSRFToken": "tok"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(self.calls, [("video", "demo", "")])
        r2 = c.post("/api/model/request", json={"category": "video"},
                    headers={"X-CSRFToken": "tok"})
        self.assertEqual(r2.status_code, 429)
        self.assertEqual(len(self.calls), 1)

    def test_cooldown_independent_per_category(self):
        # A « video » request does not block an « ocr » request from the same user.
        c = self._client("demo")
        self.assertEqual(c.post("/api/model/request", json={"category": "video"},
                                headers={"X-CSRFToken": "tok"}).status_code, 200)
        self.assertEqual(c.post("/api/model/request", json={"category": "ocr"},
                                headers={"X-CSRFToken": "tok"}).status_code, 200)
        self.assertEqual(len(self.calls), 2)


class AdminEmailRouteTest(unittest.TestCase):
    """Routes /admin/email/config et /admin/email/test (bouton « test SMTP »)."""

    def setUp(self):
        portal.app.config["TESTING"] = True
        import admin_routes as ar
        self.ar = ar
        self._send = ar.send_test_email

    def tearDown(self):
        self.ar.send_test_email = self._send

    def _smtp(self, value, admin_email=''):
        """Host, user and password all set to `value`, plus the alert recipient
        (Admin → Config, read live)."""
        cm = live_settings(smtp_host=value, smtp_user=value, smtp_pass=value, admin_email=admin_email)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def _admin(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
            s["is_admin"] = True
        return c

    def test_config_returns_status_without_password(self):
        self._smtp("")
        r = self._admin().get("/admin/email/config")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["configured"])
        self.assertNotIn("SMTP_PASS", body)
        self.assertNotIn("SMTP_USER", body)

    def test_config_forbidden_for_non_admin(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"; s["username"] = "demo"
            s["auth_at"] = int(time.time())
        r = c.get("/admin/email/config")
        self.assertNotEqual(r.status_code, 200)

    def test_email_test_non_configuré_400(self):
        self._smtp("")
        r = self._admin().post("/admin/email/test", headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_configured_email_test_calls_send(self):
        self._smtp("x", admin_email="admin@example.com")
        self.ar.send_test_email = lambda: True
        r = self._admin().post("/admin/email/test", headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
