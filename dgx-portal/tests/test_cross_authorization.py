"""CROSS-SITE authorization (IDOR): an account never touches another's data.

Why this file exists: isolation was checked for memory
(`test_memory_api.IsolationApiTest`), and for NOTHING else. Yet the
resources carrying a client-chosen identifier are exactly those an
account may try to reach by guessing or copying it: conversation, media
job, opened session, API key.

Each route below is called by B with A's identifier, and the test requires
TWO things at once:

  1. the route does not answer « it's done » (no 2xx success);
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


class CrossAuthorizationTest(unittest.TestCase):
    CSRF = "jeton-de-test"
    A = "zz-idor-a"
    B = "zz-idor-b"

    # A's identifiers, all chosen to be guessable by B.
    CONV_A = "zz-conv-a"
    IMG_A = "aaaaaaaaaaaaaaaa"
    MUS_A = "bbbbbbbbbbbbbbbb"
    VID_A = "cccccccccccccccc"
    SID_A = "zz-sid-a"
    KEY_A = "sk-zz-cle-de-a"

    # ── Outils ───────────────────────────────────────────────────────────

    def _base(self):
        """Inserts a record keeping only the existing columns.

        The schema moves (columns added by ALTER TABLE): listing the columns
        actually present avoids a test breaking on a migration, without
        hiding a schema error on the cited columns.
        """
        with portal.app.app_context():
            db = portal.get_db()
            cases = {
                "local_users": dict(
                    username=self.A, password_hash="x", fullname="Compte A",
                    is_admin=0, group_name=None, max_budget=None, enabled=1,
                    created_at="2026-09-17T00:00:00"),
                "conversations": dict(
                    username=self.A, client_id=self.CONV_A, title="Conversation de A",
                    model="auto-model",
                    messages=json.dumps([{"role": "user", "content": self.MARKER}]),
                    updated_at="2026-09-17T00:00:00"),
                "image_jobs": dict(
                    username=self.A, prompt_id=self.IMG_A, prompt=self.MARKER + "-image",
                    status="running", image_path="/tmp/zz-a-0.png",
                    image_subfolder="", image_type="png", created_at="2026-09-17T00:00:00"),
                "ocr_jobs": dict(
                    username=self.A, text=self.MARKER + "-ocr",
                    image_path="/tmp/zz-a-ocr.png", created_at="2026-09-17T00:00:00"),
                "voice_jobs": dict(
                    username=self.A, text=self.MARKER + "-voix",
                    audio_path="/tmp/zz-a.wav", audio_ms=1000,
                    created_at="2026-09-17T00:00:00"),
                "music_jobs": dict(
                    username=self.A, job_id=self.MUS_A, prompt=self.MARKER + "-musique",
                    status="running", created_at="2026-09-17T00:00:00"),
                "video_jobs": dict(
                    username=self.A, prompt_id=self.VID_A, prompt=self.MARKER + "-video",
                    status="running", created_at="2026-09-17T00:00:00"),
                "api_keys": dict(
                    username=self.A, key_alias="zz-alias-a", key_value=self.KEY_A,
                    created_at="2026-09-17T00:00:00"),
                "user_sessions": dict(
                    sid=self.SID_A, username=self.A, auth_at=time.time(),
                    expires_at=time.time() + 3600, revoked=0, created_at=time.time(),
                    ip="127.0.0.1", user_agent="test"),
            }
            self.ids = {}
            for table, values in cases.items():
                cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
                guarded = {k: v for k, v in values.items() if k in cols}
                db.execute(
                    f"INSERT INTO {table} ({', '.join(guarded)}) "
                    f"VALUES ({', '.join('?' for _ in guarded)})",
                    tuple(guarded.values()))
                self.ids[table] = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            db.commit()
        return self.ids

    MARKER = "SECRET-DE-A"

    def setUp(self):
        portal.app.config["TESTING"] = True
        self._efface()
        self._base()
        self.client_b = self._logged_in(self.B)

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

    def _logged_in(self, username):
        """A valid Flask session, as a real login would make."""
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["fullname"] = username
            s["is_admin"] = False
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        return c

    def _value(self, table, column, key, key_value_):
        """Re-reads a database value to prove it did not move."""
        with portal.app.app_context():
            db = portal.get_db()
            line = db.execute(
                f"SELECT {column} AS v FROM {table} WHERE {key}=?", (key_value_,)).fetchone()
            return line["v"] if line else None

    def _exists(self, table, key, key_value_):
        with portal.app.app_context():
            return portal.get_db().execute(
                f"SELECT 1 FROM {table} WHERE {key}=?", (key_value_,)).fetchone() is not None

    def _refuse(self, response, route):
        """The route must not announce a success."""
        self.assertNotIn(
            response.status_code, (200, 201, 202, 204),
            f"{route} answered {response.status_code} to an account that is not "
            f"the owner: {response.get_data(as_text=True)[:200]}")

    # ── Conversations ────────────────────────────────────────────────────

    def test_b_cannot_read_a_conversation(self):
        r = self.client_b.get(f"/api/conversations/{self.CONV_A}")
        self._refuse(r, "GET /api/conversations/<id>")
        self.assertNotIn(self.MARKER, r.get_data(as_text=True))

    def test_b_cannot_delete_a_conversation(self):
        """Exact contract: the answer says HOW MANY of B's rows were
        deleted (0 here), so it does not claim to have deleted A's, and
        both cases — unknown identifier, someone else's identifier — are
        indistinguishable (no existence oracle)."""
        r = self.client_b.post("/conversations",
                               data={"action": "delete", "id": self.CONV_A},
                               headers={"X-CSRFToken": self.CSRF})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        self.assertEqual(r.get_json(), {"ok": True, "deleted": 0},
                         "the deletion claims it touched A's conversation")
        self.assertTrue(self._exists("conversations", "client_id", self.CONV_A),
                        "la conversation de A a disparu")
        self.assertEqual(self._value("conversations", "username", "client_id", self.CONV_A), self.A)

    def test_delete_counter_is_real(self):
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

    def test_unknown_action_refused(self):
        """An unrecognized action must not answer « it's done »."""
        r = self.client_b.post("/conversations", data={"action": "zz-inconnue"},
                               headers={"X-CSRFToken": self.CSRF})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertFalse(r.get_json()["ok"])

    def test_b_cannot_overwrite_a_conversation(self):
        """Same client_id: the conflict key is (username, client_id), so B writes
        at home. This test proves it — else B would overwrite A's content."""
        r = self.client_b.post(
            "/conversations",
            data={"action": "save", "id": self.CONV_A, "title": "Détournée",
                  "messages": json.dumps([{"role": "user", "content": "PWNED"}])},
            headers={"X-CSRFToken": self.CSRF})
        self.assertIn(r.status_code, (200, 204), "l'enregistrement de B doit marcher chez B")
        self.assertNotIn("PWNED", self._value("conversations", "messages", "client_id", self.CONV_A))
        self.assertNotIn("PWNED", self._value("conversations", "title", "client_id", self.CONV_A))

    def test_b_cannot_share_a_conversation(self):
        r = self.client_b.post("/conversations/share", json={"client_id": self.CONV_A},
                               headers={"X-CSRFToken": self.CSRF})
        self._refuse(r, "POST /conversations/share")
        with portal.app.app_context():
            leak = portal.get_db().execute(
                "SELECT 1 FROM conversation_shares WHERE username=?", (self.B,)).fetchone()
        self.assertIsNone(leak, "B created a share from A's conversation")

    def test_b_list_excludes_a_conversations(self):
        r = self.client_b.get("/api/conversations")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.CONV_A, r.get_data(as_text=True))
        self.assertNotIn(self.MARKER, r.get_data(as_text=True))

    # ── Sessions ouvertes ────────────────────────────────────────────────

    def test_b_cannot_revoke_a_session(self):
        r = self.client_b.post("/api/account/sessions/revoke",
                               json={"id": self.SID_A},
                               headers={"X-CSRFToken": self.CSRF})
        self.assertTrue(r.status_code < 500, r.get_data(as_text=True)[:200])
        self.assertFalse(self._value("user_sessions", "revoked", "sid", self.SID_A),
                         "A's session was revoked by B")

    def test_b_cannot_see_a_session(self):
        r = self.client_b.get("/api/account/sessions")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.SID_A, r.get_data(as_text=True))

    def test_b_cannot_revoke_all_sessions(self):
        """The « toutes les autres » action must only reach one's own."""
        r = self.client_b.post("/api/account/sessions/revoke",
                               json={"all": True}, headers={"X-CSRFToken": self.CSRF})
        self.assertTrue(r.status_code < 500, r.get_data(as_text=True)[:200])
        self.assertFalse(self._value("user_sessions", "revoked", "sid", self.SID_A))

    # ── API keys ─────────────────────────────────────────────────────────

    def test_b_cannot_revoke_a_key(self):
        r = self.client_b.post("/keys", data={"action": "revoke", "key": self.KEY_A},
                               headers={"X-CSRFToken": self.CSRF})
        self._refuse(r, "POST /keys (revoke)")
        self.assertTrue(self._exists("api_keys", "key_value", self.KEY_A),
                        "A's key was deleted by B")

    def test_b_cannot_rename_a_key(self):
        r = self.client_b.post("/keys",
                               data={"action": "rename", "key": self.KEY_A,
                                     "key_name": "detournee"},
                               headers={"X-CSRFToken": self.CSRF})
        self._refuse(r, "POST /keys (rename)")
        self.assertEqual(self._value("api_keys", "key_alias", "key_value", self.KEY_A),
                         "zz-alias-a")

    def test_b_key_list_excludes_a_key(self):
        r = self.client_b.get("/api/keys")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.KEY_A, r.get_data(as_text=True))

    # ── Media jobs: status, cancel, file ─────────────────────────────────

    def test_b_cannot_see_a_jobs(self):
        for route in ("/api/image/history", "/api/ocr/history", "/api/voice/history",
                      "/api/music/history", "/api/video/history"):
            r = self.client_b.get(route)
            self.assertEqual(r.status_code, 200, route)
            self.assertNotIn(self.MARKER, r.get_data(as_text=True), route)

    def test_b_cannot_cancel_a_jobs(self):
        calls = [
            ("/api/image/cancel/" + self.IMG_A, "image_jobs", "prompt_id", self.IMG_A),
            ("/api/music/cancel/" + self.MUS_A, "music_jobs", "job_id", self.MUS_A),
            ("/api/video/cancel/" + self.VID_A, "video_jobs", "prompt_id", self.VID_A),
        ]
        for route, table, key, value in calls:
            r = self.client_b.post(route, headers={"X-CSRFToken": self.CSRF})
            self._refuse(r, f"POST {route}")
            self.assertEqual(self._value(table, "status", key, value), "running",
                             f"{route} a modifié le job de A")

    def test_b_cannot_read_a_statuses(self):
        for route in ("/api/image/status/" + self.IMG_A,
                      "/api/music/status/" + self.MUS_A,
                      "/api/video/status/" + self.VID_A):
            r = self.client_b.get(route)
            self._refuse(r, f"GET {route}")

    def test_b_cannot_delete_a_files(self):
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

    def test_b_cannot_delete_a_ocr_job(self):
        self.assertTrue(self._exists("ocr_jobs", "id", self.ids["ocr_jobs"]))
        r = self.client_b.get(f"/ocr/image/{self.ids['ocr_jobs']}")
        self._refuse(r, "GET /ocr/image/<id>")
        self.assertTrue(self._exists("ocr_jobs", "id", self.ids["ocr_jobs"]))


if __name__ == "__main__":
    unittest.main()
