"""Mails SMTP : bouton maintenance + bouton « demander un modèle » (pages média).

On ne teste PAS l'envoi (nécessite un SMTP réel, jamais dans l'image de test) :
on teste que les fonctions de notification sont gardées (no-op sans config
SMTP), et la logique de la route /api/model/request (garde login, catégorie
valide, refus si un modèle de la catégorie est déjà chargé, notification émise
sinon).
"""
import time
import unittest

import app as portal
import notify as notify_mod
from notify import notify_maintenance_email, notify_media_request_email


class NotifyGuardsTest(unittest.TestCase):
    """Sans config SMTP (image de test), les envois sont des no-op sûrs."""

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

    def tearDown(self):
        portal._media_category_running = self._orig_cat
        portal.notify_media_request_email = self._orig_notify

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
        # Session avec CSRF valide mais SANS username => login_required rejette.
        # (Sans session du tout, la garde CSRF répondrait 400 avant login_required.)
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
        # La session de test ne porte pas fullname -> chaîne vide.
        self.assertEqual(self.calls, [("video", "demo", "")])

    def test_refuse_si_modele_deja_charge(self):
        portal._media_category_running = lambda cat: cat == "image"
        c = self._client("demo")
        r = c.post("/api/model/request", json={"category": "image"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.calls, [])
