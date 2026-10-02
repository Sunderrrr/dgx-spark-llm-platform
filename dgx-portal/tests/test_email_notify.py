"""SMTP mails: maintenance button + « demander un modèle » button (media pages).

We do NOT test the sending (requires a real SMTP, never in the test image):
we test that the notification functions are guarded (no-op without SMTP
config), and the logic of the /api/model/request route (login guard, valid
category, refusal if a model of the category is already loaded, notification
emitted otherwise).
"""
import time
import unittest

import app as portal
import notify as notify_mod
from notify import notify_maintenance_email, notify_media_request_email


class NotifyGuardsTest(unittest.TestCase):
    """Without SMTP config (test image), the sends are safe no-ops."""

    def _without_smtp(self):
        orig = (notify_mod.SMTP_HOST, notify_mod.SMTP_USER,
                notify_mod.SMTP_PASS, notify_mod.ADMIN_EMAIL)
        notify_mod.SMTP_HOST = ""
        notify_mod.SMTP_USER = ""
        notify_mod.SMTP_PASS = ""
        notify_mod.ADMIN_EMAIL = ""
        return orig

    def _restore(self, orig):
        (notify_mod.SMTP_HOST, notify_mod.SMTP_USER,
         notify_mod.SMTP_PASS, notify_mod.ADMIN_EMAIL) = orig

    def test_maintenance_email_noop_sans_config(self):
        orig = self._without_smtp()
        try:
            self.assertIs(notify_maintenance_email(True, "demo", "Demo"), False)
        finally:
            self._restore(orig)

    def test_media_request_email_noop_sans_config(self):
        orig = self._without_smtp()
        try:
            self.assertIs(notify_media_request_email("image", "demo", "Demo"), False)
        finally:
            self._restore(orig)


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

    def test_requiert_session(self):
        # Session with valid CSRF but NO username => login_required rejects.
        # (With no session at all, the CSRF guard would answer 400 before login_required.)
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"
        r = c.post("/api/model/request", json={"category": "image"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.calls, [])

    def test_categorie_inconnue_refusee(self):
        c = self._client("demo")
        r = c.post("/api/model/request", json={"category": "doodle"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_demande_envoyee_si_aucun_modele(self):
        c = self._client("demo")
        r = c.post("/api/model/request", json={"category": "video"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        # The test session carries no fullname -> empty string.
        self.assertEqual(self.calls, [("video", "demo", "")])

    def test_refuse_si_modele_deja_charge(self):
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
        r1 = c.post("/api/model/request", json={"category": "music"},
                    headers={"X-CSRFToken": "tok"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(self.calls, [("music", "demo", "")])
        r2 = c.post("/api/model/request", json={"category": "music"},
                    headers={"X-CSRFToken": "tok"})
        self.assertEqual(r2.status_code, 429)
        self.assertEqual(len(self.calls), 1)

    def test_cooldown_par_categorie_independant(self):
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
        self._smtp = (ar.SMTP_HOST, ar.SMTP_USER, ar.SMTP_PASS, ar.ADMIN_EMAIL)
        self._send = ar.send_test_email

    def tearDown(self):
        (self.ar.SMTP_HOST, self.ar.SMTP_USER,
         self.ar.SMTP_PASS, self.ar.ADMIN_EMAIL) = self._smtp
        self.ar.send_test_email = self._send

    def _admin(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
            s["is_admin"] = True
        return c

    def test_config_renvoie_statut_sans_mot_de_passe(self):
        self.ar.SMTP_HOST = self.ar.SMTP_USER = self.ar.SMTP_PASS = ""
        self.ar.ADMIN_EMAIL = ""
        r = self._admin().get("/admin/email/config")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["configured"])
        self.assertNotIn("SMTP_PASS", body)
        self.assertNotIn("SMTP_USER", body)

    def test_config_non_admin_interdit(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"; s["username"] = "demo"
            s["auth_at"] = int(time.time())
        r = c.get("/admin/email/config")
        self.assertNotEqual(r.status_code, 200)

    def test_email_test_non_configuré_400(self):
        self.ar.SMTP_HOST = self.ar.SMTP_USER = self.ar.SMTP_PASS = ""
        self.ar.ADMIN_EMAIL = ""
        r = self._admin().post("/admin/email/test", headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_email_test_configuré_appelle_send(self):
        self.ar.SMTP_HOST = self.ar.SMTP_USER = self.ar.SMTP_PASS = "x"
        self.ar.ADMIN_EMAIL = "admin@example.com"
        self.ar.send_test_email = lambda: True
        r = self._admin().post("/admin/email/test", headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
