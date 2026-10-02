"""Guards of the media routes: maintenance first, rate second.

Written on 2026-09-17 along with the grouping of the four identical
preambles (image, music, video, voice) into `guards.media_block_json`: this
path had NO test, so nothing proved the refactor kept the refusal order nor
the status codes. A guard refusal must never let the request through (the
sidecar saturates the shared GPU).
"""
import time
import unittest

import app as portal
from db import set_setting

# The four media generation routes, all behind the same guard.
ROUTES = ('/api/image/generate', '/api/music/generate',
          '/api/video/generate', '/api/voice/generate')


class _BaseMedia(unittest.TestCase):
    CSRF = "test-csrf"

    def setUp(self):
        with self._db():
            db = portal.get_db()
            portal.get_db().execute(
                "DELETE FROM login_attempts WHERE key LIKE 'rl-media|%'")
            portal.get_db().commit()
        # `set_setting` (upsert) and not an UPDATE: the `maintenance_mode` row
        # does not exist by default, the fallback value coming from `get_setting`.
        # An UPDATE on an absent row changed nothing and the test passed
        # beside the guard.
        with self._db():
            set_setting('maintenance_mode', '0')

    def tearDown(self):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM login_attempts WHERE key LIKE 'rl-media|%'")
            db.commit()
        with self._db():
            set_setting('maintenance_mode', '0')

    def _db(self):
        return portal.app.app_context()

    def _client(self, username="demo", is_admin=False):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["fullname"] = "Compte de test"
            s["is_admin"] = is_admin
            s["csrf"] = self.CSRF
        return c

    def _post(self, client, url, data=None):
        return client.post(url, data=data or {},
                           headers={"X-CSRFToken": self.CSRF})

    def _maintenance(self, actif):
        with self._db():
            set_setting('maintenance_mode', '1' if actif else '0')


class MaintenanceTest(_BaseMedia):
    """In maintenance, an ordinary account is refused BEFORE everything else."""

    def test_les_quatre_routes_media_refusees_en_maintenance(self):
        self._maintenance(True)
        c = self._client("demo")
        for url in ROUTES:
            with self.subTest(url=url):
                r = self._post(c, url, {'prompt': 'x'})
                self.assertEqual(r.status_code, 503, url)
                self.assertIn("maintenance", r.get_json()['error'].lower())

    def test_un_admin_passe_pendant_la_maintenance(self):
        """Maintenance must not lock the administrator out: they are the only one
        able to deactivate it from the UI."""
        self._maintenance(True)
        c = self._client("root", is_admin=True)
        for url in ROUTES:
            with self.subTest(url=url):
                r = self._post(c, url)
                # The guard is crossed: the next error (missing prompt or absent
                # model) is an INPUT error, never a 503/429.
                self.assertNotIn(r.status_code, (503, 429), url)


class DebitTest(_BaseMedia):
    """The media rate cap is per account, and it is reachable."""

    def test_le_plafond_media_finit_par_refuser(self):
        # The bucket is shared with the chat cap (same window, same table): we
        # thus send requests WITHOUT prompt, rejected with 400 by the input
        # validation — they still consume the quota, without ever touching
        # the sidecar.
        c = self._client("demo")
        codes = [self._post(c, '/api/image/generate').status_code
                 for _ in range(21)]
        self.assertNotIn(429, codes[:20], codes)   # the first 20 cross the guard
        self.assertEqual(codes[20], 429, codes)    # the 21st is refused

    def test_le_plafond_est_par_compte(self):
        """An account must not be able to exhaust another's quota."""
        c1 = self._client("demo")
        for _ in range(21):
            self._post(c1, '/api/image/generate')
        c2 = self._client("autre")
        r = self._post(c2, '/api/image/generate')
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_la_maintenance_est_evaluee_avant_le_debit(self):
        """The order is a decision: in maintenance AND at the cap, the answer is
        the maintenance refusal (503), not the rate refusal (429)."""
        c = self._client("demo")
        for _ in range(21):
            self._post(c, '/api/image/generate')
        self._maintenance(True)
        r = self._post(c, '/api/image/generate')
        self.assertEqual(r.status_code, 503, r.get_data(as_text=True))


class DicteeTest(_BaseMedia):
    """Dictation has its OWN budget, set on its real pace (~1 req/s).

    Before 2026-09-22 it shared `rl-media` (20/min): a user speaking 20 s
    received « Trop de requêtes. Réessaie dans 36 s. » and their dictation
    stopped being written. These tests lock in both halves of the fix —
    the new cap suffices for a long dictation, and it no longer eats the
    media routes' quota.
    """

    def setUp(self):
        super().setUp()
        self._vider('rl-asr')

    def tearDown(self):
        self._vider('rl-asr')
        super().tearDown()

    def _vider(self, bucket):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM login_attempts WHERE key LIKE ?", (f"{bucket}|%",))
            db.commit()

    def test_une_longue_dictee_ne_declenche_pas_de_429(self):
        """61 transcriptions = 60 s of speech + the final pass: the media cap
        (20/min) refused the 21st, this one must accept them all."""
        c = self._client("demo")
        codes = [self._post(c, '/api/transcribe').status_code for _ in range(61)]
        self.assertNotIn(429, codes, codes)
        # The refusal comes from the input validation (no audio provided), so the
        # guard was indeed crossed: this is what we want to prove.
        self.assertTrue(all(code == 400 for code in codes), codes)

    def test_le_plafond_de_dictee_finit_par_refuser(self):
        from guards import ASR_RATE_MAX
        c = self._client("demo")
        codes = [self._post(c, '/api/transcribe').status_code
                 for _ in range(ASR_RATE_MAX + 1)]
        self.assertNotIn(429, codes[:ASR_RATE_MAX], codes)
        self.assertEqual(codes[ASR_RATE_MAX], 429, codes)
        self.assertIn("Trop de requêtes",
                      self._post(c, '/api/transcribe').get_json()['error'])

    def test_la_dictee_ne_consomme_pas_le_quota_media(self):
        """The two budgets are separate: dictating must not prevent generating an
        image (and vice versa)."""
        c = self._client("demo")
        for _ in range(25):
            self._post(c, '/api/transcribe')
        # The media quota is intact: the route crosses the guard and fails on its
        # input (400), never in 429.
        self.assertEqual(self._post(c, '/api/image/generate').status_code, 400)


if __name__ == '__main__':
    unittest.main()
