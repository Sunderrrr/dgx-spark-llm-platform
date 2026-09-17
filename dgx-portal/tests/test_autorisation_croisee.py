"""Autorisation CROISÉE (IDOR) : un compte ne touche jamais les données d'un autre.

Pourquoi ce fichier existe : l'isolation était vérifiée pour la mémoire
(`test_memory_api.IsolationApiTest`), et pour RIEN d'autre. Or les ressources qui
portent un identifiant choisi par le client sont exactement celles qu'un compte
peut tenter d'atteindre en le devinant ou en le recopiant : conversation, job
média, session ouverte, clé API.

Chaque route ci-dessous est appelée par B avec l'identifiant de A, et le test
exige DEUX choses à la fois :

  1. la route ne répond pas « c'est fait » (aucun 2xx de succès) ;
  2. la donnée de A est INTACTE après l'appel.

Le second point est le vrai filet : une route peut répondre 404 après avoir déjà
écrit (contrôle placé après l'écriture), et un test qui ne regarde que le code
HTTP ne le verrait pas. C'est la forme d'IDOR la plus discrète — celle qui ne
laisse aucune trace dans l'interface.

Aucun accès réseau : les jobs média insérés sont fictifs, et toutes les routes
vérifient la propriété AVANT d'appeler un sidecar (un job étranger part donc en
404 sans qu'aucune requête ne sorte du conteneur).
"""

import json
import time
import unittest

import app as portal


class AutorisationCroiseeTest(unittest.TestCase):
    CSRF = "jeton-de-test"
    A = "zz-idor-a"
    B = "zz-idor-b"

    # Identifiants de A, tous choisis pour être devinables par B.
    CONV_A = "zz-conv-a"
    IMG_A = "aaaaaaaaaaaaaaaa"
    MUS_A = "bbbbbbbbbbbbbbbb"
    VID_A = "cccccccccccccccc"
    SID_A = "zz-sid-a"
    CLE_A = "sk-zz-cle-de-a"

    # ── Outils ───────────────────────────────────────────────────────────

    def _base(self):
        """Insère un enregistrement en ne gardant que les colonnes existantes.

        Le schéma bouge (colonnes ajoutées par ALTER TABLE) : lister les colonnes
        réellement présentes évite un test qui casse sur une migration, sans
        pour autant masquer une erreur de schéma sur les colonnes citées.
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
        """Une session Flask valide, comme le ferait un vrai login."""
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["fullname"] = username
            s["is_admin"] = False
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        return c

    def _valeur(self, table, colonne, cle, valeur_cle):
        """Relit une valeur en base pour prouver qu'elle n'a pas bougé."""
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
        """La route ne doit pas annoncer un succès."""
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
        """Contrat exact : la réponse dit COMBIEN de lignes de B ont été
        supprimées (0 ici), donc elle ne prétend pas avoir supprimé celle de A,
        et les deux cas — identifiant inconnu, identifiant d'autrui — sont
        indiscernables (aucun oracle d'existence)."""
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
        """Sinon le test précédent passerait avec un `deleted` codé en dur à 0."""
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
        """Une action non reconnue ne doit pas répondre « c'est fait »."""
        r = self.client_b.post("/conversations", data={"action": "zz-inconnue"},
                               headers={"X-CSRFToken": self.CSRF})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertFalse(r.get_json()["ok"])

    def test_b_n_ecrase_pas_la_conversation_de_a(self):
        """Même client_id : le conflit est (username, client_id), donc B écrit
        chez lui. Ce test le prouve — sinon B écraserait le contenu de A."""
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
        """L'action « toutes les autres » ne doit atteindre que les siennes."""
        r = self.client_b.post("/api/account/sessions/revoke",
                               json={"all": True}, headers={"X-CSRFToken": self.CSRF})
        self.assertTrue(r.status_code < 500, r.get_data(as_text=True)[:200])
        self.assertFalse(self._valeur("user_sessions", "revoked", "sid", self.SID_A))

    # ── Clés API ─────────────────────────────────────────────────────────

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

    # ── Jobs média : statut, annulation, fichier ─────────────────────────

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
        """Les routes de fichier servent le média d'un job : elles doivent
        refuser avant de chercher sur le disque, et la suppression d'une vignette
        ne doit pas atteindre le lot de A."""
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
