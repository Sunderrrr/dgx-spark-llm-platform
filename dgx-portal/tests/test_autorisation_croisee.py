"""CROSS-SITE authorization (IDOR): an account never touches another's data.

Why this file exists: isolation was checked for memory
(`test_memory_api.IsolationApiTest`), and for NOTHING else. Yet the
resources carrying a client-chosen identifier are exactly those an
account may try to reach by guessing or copying it: conversation, media
job, opened session, API key.

Each route below is called by B with A's identifier, and the test requires
TWO things at once:

  1. the route does not answer « c'est fait » (no 2xx success);
  2. A's data is INTACT after the call.

The second point is the real safety net: a route can answer 404 after
already writing (check placed after the write), and a test looking only at
the HTTP code would not see it. This is the most discreet form of IDOR —
the one leaving no trace in the interface.

No network access: the inserted media jobs are fictitious, and all routes
check ownership BEFORE calling a sidecar (a foreign job thus leaves in
404 without any request leaving the container).
"""

import json
import time
import unittest

import app as portal


class AutorisationCroiseeTest(unittest.TestCase):
    CSRF = "jeton-de-test"
    A = "zz-idor-a"
    B = "zz-idor-b"

    # A's identifiers, all chosen to be guessable by B.
    CONV_A = "zz-conv-a"
    IMG_A = "aaaaaaaaaaaaaaaa"
    MUS_A = "bbbbbbbbbbbbbbbb"
    VID_A = "cccccccccccccccc"
    SID_A = "zz-sid-a"
    CLE_A = "sk-zz-cle-de-a"

    # ── Outils ───────────────────────────────────────────────────────────

    def _base(self):
        """Inserts a record keeping only the existing columns.

        The schema moves (columns added by ALTER TABLE): listing the columns
        actually present avoids a test breaking on a migration, without
        hiding a schema error on the cited columns.
        """
        with portal.app.app_context():
            db = portal.get_db()
            jeux = {
                "local_users": dict(
                    username=self.A, password_hash="x", fullname="Compte A",
                    is_admin=0, group_name=None, max_budget=None, enabled=1,
                    created_at="2026-09-17T00:00:00"),
                "conversations": dict(
                    username=self.A, client_id=self.CONV_A, title="Conversation de A",
                    model="auto-model",
                    messages=json.dumps([{"role": "user", "content": self.MARQUEUR}]),
                    updated_at="2026-09-17T00:00:00"),
                "image_jobs": dict(
                    username=self.A, prompt_id=self.IMG_A, prompt=self.MARQUEUR + "-image",
                    status="running", image_path="/tmp/zz-a-0.png",
                    image_subfolder="", image_type="png", created_at="2026-09-17T00:00:00"),
                "ocr_jobs": dict(
                    username=self.A, text=self.MARQUEUR + "-ocr",
                    image_path="/tmp/zz-a-ocr.png", created_at="2026-09-17T00:00:00"),
                "voice_jobs": dict(
                    username=self.A, text=self.MARQUEUR + "-voix",
                    audio_path="/tmp/zz-a.wav", audio_ms=1000,
                    created_at="2026-09-17T00:00:00"),
                "music_jobs": dict(
                    username=self.A, job_id=self.MUS_A, prompt=self.MARQUEUR + "-musique",
                    status="running", created_at="2026-09-17T00:00:00"),
                "video_jobs": dict(
                    username=self.A, prompt_id=self.VID_A, prompt=self.MARQUEUR + "-video",
                    status="running", created_at="2026-09-17T00:00:00"),
                "api_keys": dict(
                    username=self.A, key_alias="zz-alias-a", key_value=self.CLE_A,
                    created_at="2026-09-17T00:00:00"),
                "user_sessions": dict(
                    sid=self.SID_A, username=self.A, auth_at=time.time(),
                    expires_at=time.time() + 3600, revoked=0, created_at=time.time(),
                    ip="127.0.0.1", user_agent="test"),
            }
            self.ids = {}
            for table, valeurs in jeux.items():
                cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
                gardees = {k: v for k, v in valeurs.items() if k in cols}
                db.execute(
                    f"INSERT INTO {table} ({', '.join(gardees)}) "
                    f"VALUES ({', '.join('?' for _ in gardees)})",
                    tuple(gardees.values()))
                self.ids[table] = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            db.commit()
        return self.ids

    MARQUEUR = "SECRET-DE-A"

    def setUp(self):
        portal.app.config["TESTING"] = True
        self._efface()
        self._base()
        self.client_b = self._connecte(self.B)

    def tearDown(self):
        self._efface()

    def _efface(self):
        with portal.app.app_context():
            db = portal.get_db()
            for u in (self.A, self.B):
                for table in ("local_users", "user_sources", "api_keys", "user_sessions",
                              "conversations", "conversation_shares", "user_prefs",
                              "image_jobs", "ocr_jobs", "voice_jobs", "music_jobs",
                              "video_jobs"):
                    try:
                        db.execute(f"DELETE FROM {table} WHERE username=?", (u,))
                    except Exception:
                        pass
                try:
                    db.execute("DELETE FROM memory_edges WHERE username=?", (u,))
                    db.execute("DELETE FROM memory_nodes WHERE username=?", (u,))
                except Exception:
                    pass
            db.commit()

    def _connecte(self, username):
        """A valid Flask session, as a real login would make."""
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["fullname"] = username
            s["is_admin"] = False
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        return c

    def _valeur(self, table, colonne, cle, valeur_cle):
        """Re-reads a database value to prove it did not move."""
        with portal.app.app_context():
            db = portal.get_db()
            ligne = db.execute(
                f"SELECT {colonne} AS v FROM {table} WHERE {cle}=?", (valeur_cle,)).fetchone()
            return ligne["v"] if ligne else None

    def _existe(self, table, cle, valeur_cle):
        with portal.app.app_context():
            return portal.get_db().execute(
                f"SELECT 1 FROM {table} WHERE {cle}=?", (valeur_cle,)).fetchone() is not None

    def _refuse(self, reponse, route):
        """The route must not announce a success."""
        self.assertNotIn(
            reponse.status_code, (200, 201, 202, 204),
            f"{route} a répondu {reponse.status_code} à un compte qui n'est pas "
            f"le propriétaire : {reponse.get_data(as_text=True)[:200]}")

    # ── Conversations ────────────────────────────────────────────────────

    def test_b_ne_lit_pas_la_conversation_de_a(self):
        r = self.client_b.get(f"/api/conversations/{self.CONV_A}")
        self._refuse(r, "GET /api/conversations/<id>")
        self.assertNotIn(self.MARQUEUR, r.get_data(as_text=True))

    def test_b_ne_supprime_pas_la_conversation_de_a(self):
        """Exact contract: the answer says HOW MANY of B's rows were
        deleted (0 here), so it does not claim to have deleted A's, and
        both cases — unknown identifier, someone else's identifier — are
        indistinguishable (no existence oracle)."""
        r = self.client_b.post("/conversations",
                               data={"action": "delete", "id": self.CONV_A},
                               headers={"X-CSRFToken": self.CSRF})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        self.assertEqual(r.get_json(), {"ok": True, "deleted": 0},
                         "la suppression prétend avoir touché la conversation de A")
        self.assertTrue(self._existe("conversations", "client_id", self.CONV_A),
                        "la conversation de A a disparu")
        self.assertEqual(self._valeur("conversations", "username", "client_id", self.CONV_A), self.A)

    def test_le_compteur_de_suppression_est_reel(self):
        """Otherwise the previous test would pass with a hardcoded 0 `deleted`."""
        self.client_b.post(
            "/conversations",
            data={"action": "save", "id": "zz-conv-b", "title": "À B",
                  "messages": json.dumps([{"role": "user", "content": "à moi"}])},
            headers={"X-CSRFToken": self.CSRF})
        r = self.client_b.post("/conversations",
                               data={"action": "delete", "id": "zz-conv-b"},
                               headers={"X-CSRFToken": self.CSRF})
        self.assertEqual(r.get_json(), {"ok": True, "deleted": 1})

    def test_action_inconnue_refusee(self):
        """An unrecognized action must not answer « c'est fait »."""
        r = self.client_b.post("/conversations", data={"action": "zz-inconnue"},
                               headers={"X-CSRFToken": self.CSRF})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertFalse(r.get_json()["ok"])

    def test_b_n_ecrase_pas_la_conversation_de_a(self):
        """Same client_id: the conflict key is (username, client_id), so B writes
        at home. This test proves it — else B would overwrite A's content."""
        r = self.client_b.post(
            "/conversations",
            data={"action": "save", "id": self.CONV_A, "title": "Détournée",
                  "messages": json.dumps([{"role": "user", "content": "PWNED"}])},
            headers={"X-CSRFToken": self.CSRF})
        self.assertIn(r.status_code, (200, 204), "l'enregistrement de B doit marcher chez B")
        self.assertNotIn("PWNED", self._valeur("conversations", "messages", "client_id", self.CONV_A))
        self.assertNotIn("PWNED", self._valeur("conversations", "title", "client_id", self.CONV_A))

    def test_b_ne_partage_pas_la_conversation_de_a(self):
        r = self.client_b.post("/conversations/share", json={"client_id": self.CONV_A},
                               headers={"X-CSRFToken": self.CSRF})
        self._refuse(r, "POST /conversations/share")
        with portal.app.app_context():
            fuite = portal.get_db().execute(
                "SELECT 1 FROM conversation_shares WHERE username=?", (self.B,)).fetchone()
        self.assertIsNone(fuite, "B a créé un partage à partir de la conversation de A")

    def test_la_liste_de_b_ne_contient_pas_les_conversations_de_a(self):
        r = self.client_b.get("/api/conversations")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.CONV_A, r.get_data(as_text=True))
        self.assertNotIn(self.MARQUEUR, r.get_data(as_text=True))

    # ── Sessions ouvertes ────────────────────────────────────────────────

    def test_b_ne_revoque_pas_la_session_de_a(self):
        r = self.client_b.post("/api/account/sessions/revoke",
                               json={"id": self.SID_A},
                               headers={"X-CSRFToken": self.CSRF})
        self.assertTrue(r.status_code < 500, r.get_data(as_text=True)[:200])
        self.assertFalse(self._valeur("user_sessions", "revoked", "sid", self.SID_A),
                         "la session de A a été révoquée par B")

    def test_b_ne_voit_pas_la_session_de_a(self):
        r = self.client_b.get("/api/account/sessions")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.SID_A, r.get_data(as_text=True))

    def test_b_ne_revoque_pas_toutes_les_sessions(self):
        """The « toutes les autres » action must only reach one's own."""
        r = self.client_b.post("/api/account/sessions/revoke",
                               json={"all": True}, headers={"X-CSRFToken": self.CSRF})
        self.assertTrue(r.status_code < 500, r.get_data(as_text=True)[:200])
        self.assertFalse(self._valeur("user_sessions", "revoked", "sid", self.SID_A))

    # ── API keys ─────────────────────────────────────────────────────────

    def test_b_ne_revoque_pas_la_cle_de_a(self):
        r = self.client_b.post("/keys", data={"action": "revoke", "key": self.CLE_A},
                               headers={"X-CSRFToken": self.CSRF})
        self._refuse(r, "POST /keys (revoke)")
        self.assertTrue(self._existe("api_keys", "key_value", self.CLE_A),
                        "la clé de A a été supprimée par B")

    def test_b_ne_renomme_pas_la_cle_de_a(self):
        r = self.client_b.post("/keys",
                               data={"action": "rename", "key": self.CLE_A,
                                     "key_name": "detournee"},
                               headers={"X-CSRFToken": self.CSRF})
        self._refuse(r, "POST /keys (rename)")
        self.assertEqual(self._valeur("api_keys", "key_alias", "key_value", self.CLE_A),
                         "zz-alias-a")

    def test_la_liste_des_cles_de_b_ne_contient_pas_celle_de_a(self):
        r = self.client_b.get("/api/keys")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.CLE_A, r.get_data(as_text=True))

    # ── Media jobs: status, cancel, file ─────────────────────────────────

    def test_b_ne_voit_pas_les_jobs_de_a(self):
        for route in ("/api/image/history", "/api/ocr/history", "/api/voice/history",
                      "/api/music/history", "/api/video/history"):
            r = self.client_b.get(route)
            self.assertEqual(r.status_code, 200, route)
            self.assertNotIn(self.MARQUEUR, r.get_data(as_text=True), route)

    def test_b_n_annule_pas_les_jobs_de_a(self):
        appels = [
            ("/api/image/cancel/" + self.IMG_A, "image_jobs", "prompt_id", self.IMG_A),
            ("/api/music/cancel/" + self.MUS_A, "music_jobs", "job_id", self.MUS_A),
            ("/api/video/cancel/" + self.VID_A, "video_jobs", "prompt_id", self.VID_A),
        ]
        for route, table, cle, valeur in appels:
            r = self.client_b.post(route, headers={"X-CSRFToken": self.CSRF})
            self._refuse(r, f"POST {route}")
            self.assertEqual(self._valeur(table, "status", cle, valeur), "running",
                             f"{route} a modifié le job de A")

    def test_b_ne_lit_pas_les_statuts_de_a(self):
        for route in ("/api/image/status/" + self.IMG_A,
                      "/api/music/status/" + self.MUS_A,
                      "/api/video/status/" + self.VID_A):
            r = self.client_b.get(route)
            self._refuse(r, f"GET {route}")

    def test_b_ne_supprime_pas_les_fichiers_de_a(self):
        """The file routes serve a job's media: they must refuse before
        looking on disk, and deleting a thumbnail must not reach
        A's batch."""
        routes = [
            f"/image/file/{self.IMG_A}",
            f"/api/image/delete/{self.IMG_A}/0",
        ]
        for route in routes:
            r = (self.client_b.post(route, headers={"X-CSRFToken": self.CSRF})
                 if route.startswith("/api/") else self.client_b.get(route))
            self._refuse(r, route)
        for route in (f"/ocr/image/{self.ids['ocr_jobs']}",
                      f"/voice/audio/{self.ids['voice_jobs']}",
                      f"/music/file/{self.MUS_A}",
                      f"/video/file/{self.VID_A}"):
            r = self.client_b.get(route)
            self._refuse(r, f"GET {route}")

    def test_b_ne_supprime_pas_le_job_ocr_de_a(self):
        self.assertTrue(self._existe("ocr_jobs", "id", self.ids["ocr_jobs"]))
        r = self.client_b.get(f"/ocr/image/{self.ids['ocr_jobs']}")
        self._refuse(r, "GET /ocr/image/<id>")
        self.assertTrue(self._existe("ocr_jobs", "id", self.ids["ocr_jobs"]))


if __name__ == "__main__":
    unittest.main()
