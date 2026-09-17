"""Gardes des routes média : maintenance d'abord, débit ensuite.

Écrit le 2026-09-17 en même temps que le regroupement des quatre préambules
identiques (image, musique, vidéo, voix) dans `guards.media_block_json` : ce
chemin n'avait AUCUN test, donc rien ne prouvait que le refactor conservait
l'ordre des refus ni les codes de statut. Un refus de garde ne doit jamais
laisser passer la requête (le sidecar sature le GPU partagé).
"""
import time
import unittest

import app as portal
from db import set_setting

# Les quatre routes de génération média, toutes derrière la même garde.
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
        # `set_setting` (upsert) et non un UPDATE : la ligne `maintenance_mode`
        # n'existe pas par défaut, la valeur de repli venant de `get_setting`.
        # Un UPDATE sur une ligne absente ne changeait rien et le test passait
        # à côté de la garde.
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
    """En maintenance, un compte ordinaire est refusé AVANT tout le reste."""

    def test_les_quatre_routes_media_refusees_en_maintenance(self):
        self._maintenance(True)
        c = self._client("demo")
        for url in ROUTES:
            with self.subTest(url=url):
                r = self._post(c, url, {'prompt': 'x'})
                self.assertEqual(r.status_code, 503, url)
                self.assertIn("maintenance", r.get_json()['error'].lower())

    def test_un_admin_passe_pendant_la_maintenance(self):
        """La maintenance ne doit pas enfermer l'administrateur dehors : il est
        le seul à pouvoir la désactiver depuis l'interface."""
        self._maintenance(True)
        c = self._client("root", is_admin=True)
        for url in ROUTES:
            with self.subTest(url=url):
                r = self._post(c, url)
                # La garde est franchie : l'erreur suivante (prompt manquant ou
                # modèle absent) est une erreur d'ENTRÉE, jamais un 503/429.
                self.assertNotIn(r.status_code, (503, 429), url)


class DebitTest(_BaseMedia):
    """Le plafond de débit média compte par compte, et il est atteignable."""

    def test_le_plafond_media_finit_par_refuser(self):
        # Le bucket est partagé avec le plafond de chat (même fenêtre, même
        # table) : on envoie donc des requêtes SANS prompt, rejetées en 400 par
        # la validation d'entrée — elles consomment quand même le quota, sans
        # jamais toucher au sidecar.
        c = self._client("demo")
        codes = [self._post(c, '/api/image/generate').status_code
                 for _ in range(21)]
        self.assertNotIn(429, codes[:20], codes)   # les 20 premières passent la garde
        self.assertEqual(codes[20], 429, codes)    # la 21e est refusée

    def test_le_plafond_est_par_compte(self):
        """Un compte ne doit pas pouvoir épuiser le quota d'un autre."""
        c1 = self._client("demo")
        for _ in range(21):
            self._post(c1, '/api/image/generate')
        c2 = self._client("autre")
        r = self._post(c2, '/api/image/generate')
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_la_maintenance_est_evaluee_avant_le_debit(self):
        """L'ordre est une décision : en maintenance ET au plafond, la réponse
        est le refus de maintenance (503), pas le refus de débit (429)."""
        c = self._client("demo")
        for _ in range(21):
            self._post(c, '/api/image/generate')
        self._maintenance(True)
        r = self._post(c, '/api/image/generate')
        self.assertEqual(r.status_code, 503, r.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
