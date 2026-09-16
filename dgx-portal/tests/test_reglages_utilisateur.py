"""Les routes des PARAMÈTRES UTILISATEUR, et leur contrat de réponse.

Pourquoi ce fichier existe : les actions de l'onglet « Clés API » (`POST /keys`)
et celles des réglages (`/settings/avatar`, `/settings/appearance`) répondaient
`('', 204)` après un `flash(...)`. Or **aucun template ne rend les flash** —
l'interface est Next.js —, donc l'utilisateur lisait « Clé créée ! » ou « Clé
révoquée. » quoi qu'il arrive. Le cas grave : LiteLLM injoignable, la révocation
échouait et la clé restait VALIDE pendant que l'utilisateur la croyait morte.

Ces tests verrouillent le contrat honnête : un succès dit `{ok: true}`, un refus
porte un statut HTTP qui le dit (`404` clé inconnue, `409` demande déjà en
attente, `502` amont injoignable, `400` valeur invalide).

Aucun appel réseau : `create_litellm_key` / `revoke_litellm_key` sont remplacés.
"""
import time
import unittest
from unittest.mock import patch

import app as portal


class _BaseReglages(unittest.TestCase):
    CSRF = "test-csrf"

    def _client(self, username="demo"):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["fullname"] = "Compte de test"
            s["is_admin"] = False
            s["csrf"] = self.CSRF
        return c

    def _post(self, client, url, data):
        return client.post(url, data=data, headers={"X-CSRFToken": self.CSRF})

    def _db(self):
        return portal.app.app_context()


class ClesApiTest(_BaseReglages):
    """`POST /keys` : création, révocation, demande de budget."""

    def _insere_cle(self, valeur="sk-test-123", alias="poste-test", user="demo"):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM api_keys WHERE key_value=?", (valeur,))
            db.execute(
                "INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) "
                "VALUES (?,?,?,?)", (user, alias, valeur, "2026-09-14T00:00:00"))
            db.commit()

    def test_creation_reussie_rend_la_cle(self):
        """La valeur repart vers l'interface : c'est le seul instant où on la voit."""
        c = self._client()
        with patch.object(portal, "create_litellm_key", return_value="sk-neuve-999"):
            r = self._post(c, "/keys", {"action": "create", "key_name": "mon-laptop"})
        self.assertEqual(r.status_code, 200)
        corps = r.get_json()
        self.assertTrue(corps["ok"])
        self.assertEqual(corps["key"], "sk-neuve-999")
        self.assertEqual(corps["key_alias"], "mon-laptop")
        with self._db():
            ligne = portal.get_db().execute(
                "SELECT key_alias FROM api_keys WHERE key_value=?",
                ("sk-neuve-999",)).fetchone()
        self.assertIsNotNone(ligne, "la clé créée doit être consignée en base")
        self.assertEqual(ligne["key_alias"], "mon-laptop")

    def test_creation_ratee_dit_502_et_ne_ment_pas(self):
        """LiteLLM muet : avant, l'interface annonçait « Clé créée ! »."""
        c = self._client()
        with patch.object(portal, "create_litellm_key", return_value=None):
            r = self._post(c, "/keys", {"action": "create", "key_name": "x"})
        self.assertEqual(r.status_code, 502)
        corps = r.get_json()
        self.assertFalse(corps["ok"])
        self.assertTrue(corps["error"])
        with self._db():
            ligne = portal.get_db().execute(
                "SELECT 1 FROM api_keys WHERE key_alias='x'").fetchone()
        self.assertIsNone(ligne, "aucune clé ne doit être consignée quand la création échoue")

    def test_creation_sans_nom_genere_un_alias(self):
        c = self._client()
        vus = {}

        def _faux(alias, username, is_admin=False):
            vus["alias"] = alias
            return "sk-auto-1"

        with patch.object(portal, "create_litellm_key", side_effect=_faux):
            r = self._post(c, "/keys", {"action": "create", "key_name": "  "})
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(vus["alias"].startswith("demo-"),
                        f"alias généré inattendu : {vus['alias']!r}")

    def test_revocation_dune_cle_dun_autre_compte_refusee(self):
        """Anti-IDOR : la clé existe, mais pas sur CE compte."""
        self._insere_cle(valeur="sk-autrui", user="autre")
        c = self._client("demo")
        with patch.object(portal, "revoke_litellm_key", return_value=True):
            r = self._post(c, "/keys", {"action": "revoke", "key": "sk-autrui"})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()["ok"])

    def test_revocation_ratee_dit_que_la_cle_est_encore_valide(self):
        """Le mensonge dangereux : « Clé révoquée. » alors qu'elle marche encore."""
        self._insere_cle(valeur="sk-vive")
        c = self._client()
        with patch.object(portal, "revoke_litellm_key", return_value=False):
            r = self._post(c, "/keys", {"action": "revoke", "key": "sk-vive"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("encore valide", r.get_json()["error"])
        with self._db():
            ligne = portal.get_db().execute(
                "SELECT 1 FROM api_keys WHERE key_value='sk-vive'").fetchone()
        self.assertIsNotNone(ligne, "une révocation ratée ne doit PAS retirer la ligne")

    def test_revocation_reussie_supprime_la_ligne(self):
        self._insere_cle(valeur="sk-amortir")
        c = self._client()
        with patch.object(portal, "revoke_litellm_key", return_value=True):
            r = self._post(c, "/keys", {"action": "revoke", "key": "sk-amortir"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with self._db():
            ligne = portal.get_db().execute(
                "SELECT 1 FROM api_keys WHERE key_value='sk-amortir'").fetchone()
        self.assertIsNone(ligne)

    def test_demande_de_budget_deja_en_attente_refusee(self):
        """Le bouton affichait « Demande envoyée ! » alors que le serveur refusait."""
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM budget_requests WHERE username='demo'")
            db.execute(
                "INSERT INTO budget_requests (username, fullname, key_alias, current_budget, "
                "reason, status, created_at) VALUES (?,?,?,?,?,?,?)",
                ("demo", "Compte de test", "(compte)", 1000, None, "pending", "2026-09-14"))
            db.commit()
        c = self._client()
        r = self._post(c, "/keys", {"action": "request_budget", "reason": "encore"})
        self.assertEqual(r.status_code, 409)
        corps = r.get_json()
        self.assertFalse(corps["ok"])
        self.assertEqual(corps["code"], "deja_en_attente",
                         "un refus stable doit porter un code traduisible")

    def test_demande_de_budget_inseree(self):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM budget_requests WHERE username='demo'")
            db.commit()
        c = self._client()
        with patch.object(portal, "notify_budget_email", return_value=True), \
             patch.object(portal, "notify_budget_discord", return_value=True):
            r = self._post(c, "/keys", {"action": "request_budget", "reason": "besoin"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with self._db():
            n = portal.get_db().execute(
                "SELECT COUNT(*) FROM budget_requests WHERE username='demo' "
                "AND status='pending'").fetchone()[0]
        self.assertEqual(n, 1)
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM budget_requests WHERE username='demo'")
            db.commit()

    def test_action_inconnue_refusee(self):
        c = self._client()
        r = self._post(c, "/keys", {"action": "explose"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_get_reste_sans_corps(self):
        """Le GET n'est pas une page : l'interface Next.js porte l'écran."""
        c = self._client()
        self.assertEqual(c.get("/keys").status_code, 204)


class AvatarTest(_BaseReglages):
    """`POST /settings/avatar` : `flash` invisible → réponse lisible."""

    def test_avatar_inconnu_refuse_en_400(self):
        c = self._client()
        r = self._post(c, "/settings/avatar", {"avatar_id": "../../etc/passwd"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_avatar_valide_enregistre(self):
        with self._db():
            avatars = portal.AVATAR_IDS
        self.assertTrue(avatars, "aucun avatar connu : le test ne prouverait rien")
        c = self._client()
        r = self._post(c, "/settings/avatar", {"avatar_id": avatars[0]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with self._db():
            ligne = portal.get_db().execute(
                "SELECT avatar_id FROM user_prefs WHERE username='demo'").fetchone()
        self.assertIsNotNone(ligne)
        self.assertEqual(ligne["avatar_id"], avatars[0])


class ApparenceTest(_BaseReglages):
    """`POST /settings/appearance` : le refus portait le statut du succès."""

    def test_theme_inconnu_refuse_en_400(self):
        c = self._client()
        r = self._post(c, "/settings/appearance", {"theme_id": "rose-fluo"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_langue_inconnue_refusee_en_400(self):
        c = self._client()
        r = self._post(c, "/settings/appearance", {"lang": "de"})
        self.assertEqual(r.status_code, 400)

    def test_theme_et_langue_valides_enregistres(self):
        with self._db():
            theme = portal.THEME_IDS[0]
        c = self._client()
        r = self._post(c, "/settings/appearance", {"theme_id": theme, "lang": "fr"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with self._db():
            ligne = portal.get_db().execute(
                "SELECT theme_id, lang FROM user_prefs WHERE username='demo'").fetchone()
        self.assertEqual(ligne["theme_id"], theme)
        self.assertEqual(ligne["lang"], "fr")


if __name__ == "__main__":
    unittest.main()
