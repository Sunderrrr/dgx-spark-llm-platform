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
from werkzeug.security import generate_password_hash


class _BaseReglages(unittest.TestCase):
    CSRF = "test-csrf"

    def _client(self, username="demo", is_admin=False):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["fullname"] = "Compte de test"
            s["is_admin"] = is_admin
            s["csrf"] = self.CSRF
        return c

    def _post(self, client, url, data):
        return client.post(url, data=data, headers={"X-CSRFToken": self.CSRF})

    def _json_post(self, client, url, data):
        return client.post(url, json=data, headers={"X-CSRFToken": self.CSRF})

    def _db(self):
        return portal.app.app_context()

    def _insere_local(self, username, password="Motdepasse1!", is_admin=0, enabled=1):
        """Crée le compte LOCAL que les routes lisent (`local_users`)."""
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM local_users WHERE username=?", (username,))
            db.execute(
                "INSERT INTO local_users (username, password_hash, fullname, is_admin, "
                "group_name, max_budget, enabled, created_at) VALUES (?,?,?,?,NULL,NULL,?,?)",
                (username, generate_password_hash(password), "Compte de test", is_admin,
                 enabled, "2026-09-14T00:00:00"))
            db.commit()

    def _insere_source(self, username, sources):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM user_sources WHERE username=?", (username,))
            db.execute(
                "INSERT INTO user_sources (username, sources, fullname, last_source, last_seen, "
                "last_is_admin) VALUES (?,?,?,?,?,NULL)",
                (username, sources, "Compte de test", sources.split(",")[0], "2026-09-14T00:00:00"))
            db.commit()

    def _nettoie(self, username):
        with self._db():
            db = portal.get_db()
            for table in ("local_users", "user_sources", "api_keys", "user_sessions",
                          "user_prefs", "login_attempts"):
                try:
                    db.execute(f"DELETE FROM {table} WHERE username=?", (username,))
                except Exception:
                    pass
            db.commit()


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

    def test_avatar_genere_remet_a_null(self):
        """Le choix « avatar généré » (valeur vide) doit revenir en arrière.

        C'est `NULL` qui veut dire « aucun logo choisi, donc avatar créé à
        partir du pseudo » : sans ce retour possible, un compte qui avait pris
        un logo de marque ne pouvait plus jamais revenir à l'avatar par défaut —
        et la valeur vide n'était même pas acceptée (400).
        """
        with self._db():
            avatars = portal.AVATAR_IDS
        c = self._client()
        self.assertEqual(self._post(c, "/settings/avatar", {"avatar_id": avatars[0]}).status_code, 200)
        with self._db():
            assert portal.get_db().execute(
                "SELECT avatar_id FROM user_prefs WHERE username='demo'"
            ).fetchone()["avatar_id"] == avatars[0]
        r = self._post(c, "/settings/avatar", {"avatar_id": ""})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with self._db():
            ligne = portal.get_db().execute(
                "SELECT avatar_id FROM user_prefs WHERE username='demo'").fetchone()
        self.assertIsNone(ligne["avatar_id"])

    def test_avatar_genere_accepte_en_toutes_lettres(self):
        """`genere` est accepté comme synonyme de la valeur vide."""
        c = self._client()
        r = self._post(c, "/settings/avatar", {"avatar_id": "genere"})
        self.assertEqual(r.status_code, 200)
        with self._db():
            ligne = portal.get_db().execute(
                "SELECT avatar_id FROM user_prefs WHERE username='demo'").fetchone()
        self.assertIsNone(ligne["avatar_id"])


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


class RenommageCleTest(_BaseReglages):
    """`action=rename` : l'alias est ce qui permet de retrouver sa clé."""

    def _insere_cle(self, valeur="sk-ren-1", alias="poste-test", user="demo"):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM api_keys WHERE key_value=?", (valeur,))
            db.execute(
                "INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) "
                "VALUES (?,?,?,?)", (user, alias, valeur, "2026-09-14T00:00:00"))
            db.commit()

    def _alias(self, valeur):
        with self._db():
            ligne = portal.get_db().execute(
                "SELECT key_alias FROM api_keys WHERE key_value=?", (valeur,)).fetchone()
        return ligne["key_alias"] if ligne else None

    def test_renommage_reussi_des_deux_cotes(self):
        """Portail ET LiteLLM : sinon les deux vues divergent."""
        self._insere_cle()
        c = self._client()
        appels = []
        with patch.object(portal, "renommer_cle_litellm",
                          side_effect=lambda k, a: appels.append((k, a)) or True):
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-ren-1",
                                        "key_name": "portable-nora"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self._alias("sk-ren-1"), "portable-nora")
        self.assertEqual(appels, [("sk-ren-1", "portable-nora")],
                         "l'alias doit aussi partir chez LiteLLM")

    def test_renommage_dune_cle_dun_autre_compte_refuse(self):
        self._insere_cle(valeur="sk-autrui-ren", user="autre")
        c = self._client("demo")
        with patch.object(portal, "renommer_cle_litellm", return_value=True) as faux:
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-autrui-ren",
                                        "key_name": "a-moi"})
        self.assertEqual(r.status_code, 404)
        faux.assert_not_called()
        self.assertEqual(self._alias("sk-autrui-ren"), "poste-test")

    def test_litellm_injoignable_garde_l_ancien_nom(self):
        """Un renommage à moitié fait serait pire que pas de renommage."""
        self._insere_cle()
        c = self._client()
        with patch.object(portal, "renommer_cle_litellm", return_value=False):
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-ren-1",
                                        "key_name": "portable-nora"})
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self._alias("sk-ren-1"), "poste-test",
                         "le portail ne doit pas renommer si LiteLLM a refusé")

    def test_alias_deja_pris_refuse(self):
        self._insere_cle(valeur="sk-a", alias="portable")
        self._insere_cle(valeur="sk-b", alias="bureau")
        c = self._client()
        with patch.object(portal, "renommer_cle_litellm", return_value=True) as faux:
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-b",
                                        "key_name": "portable"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "alias_deja_utilise")
        faux.assert_not_called()

    def test_nom_vide_refuse(self):
        self._insere_cle()
        c = self._client()
        with patch.object(portal, "renommer_cle_litellm", return_value=True) as faux:
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-ren-1",
                                        "key_name": "   "})
        self.assertEqual(r.status_code, 400)
        faux.assert_not_called()
        self.assertEqual(self._alias("sk-ren-1"), "poste-test")

    def test_nom_assaini_et_borne(self):
        """Un alias de 4000 caractères casserait l'affichage de la liste."""
        self._insere_cle()
        c = self._client()
        with patch.object(portal, "renommer_cle_litellm", return_value=True):
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-ren-1",
                                        "key_name": "mon portable " + "x" * 100})
        self.assertEqual(r.status_code, 200)
        alias = self._alias("sk-ren-1")
        self.assertLessEqual(len(alias), 40)
        self.assertNotIn(" ", alias)
        self.assertTrue(alias.startswith("mon-portable"))


class SourceMotDePasseTest(_BaseReglages):
    """Qui détient le mot de passe : la question qui décide de ce qu'on affiche."""

    def test_compte_local(self):
        self._insere_local("zz-gestion-local")
        with self._db():
            info = portal.gestion_mot_de_passe("zz-gestion-local")
        self.assertEqual(info["gestion"], "portail")
        self.assertTrue(info["passkey"])
        self._nettoie("zz-gestion-local")

    def test_compte_ldap(self):
        self._insere_source("zz-gestion-ldap", "ldap")
        with self._db():
            info = portal.gestion_mot_de_passe("zz-gestion-ldap")
        self.assertEqual(info["gestion"], "annuaire-ldap")
        self.assertTrue(info["passkey"], "la 2FA reste possible pour un compte LDAP")
        self._nettoie("zz-gestion-ldap")

    def test_compte_sso(self):
        self._insere_source("zz-gestion-sso", "sso")
        with self._db():
            info = portal.gestion_mot_de_passe("zz-gestion-sso")
        self.assertEqual(info["gestion"], "fournisseur-sso")
        self.assertFalse(info["passkey"],
                         "un compte SSO n'a pas de mot de passe vérifiable ici")
        self._nettoie("zz-gestion-sso")

    def test_compte_sans_source_connue(self):
        """Donnée absente ≠ absence de mot de passe : on ne devine pas."""
        self._nettoie("zz-gestion-inconnu")
        with self._db():
            info = portal.gestion_mot_de_passe("zz-gestion-inconnu")
        self.assertEqual(info["gestion"], "inconnu")
        self.assertFalse(info["local"])

    def test_local_et_sso_le_local_fait_autorite(self):
        """Les sources sont CUMULATIVES : annoncer « SSO » serait faux."""
        self._insere_local("zz-gestion-mixte")
        self._insere_source("zz-gestion-mixte", "local,sso")
        with self._db():
            info = portal.gestion_mot_de_passe("zz-gestion-mixte")
        self.assertEqual(info["gestion"], "portail")
        self._nettoie("zz-gestion-mixte")


class SecuriteCompteTest(_BaseReglages):
    """`/api/security` : dire pourquoi une passkey n'est pas proposable."""

    def test_sso_refuse_la_verification_sans_compter_d_echec(self):
        """Un compte SSO qui essaie son mot de passe ne doit pas se verrouiller."""
        self._insere_source("zz-sec-sso", "sso")
        c = self._client("zz-sec-sso")
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM login_attempts WHERE key=?", ("user:zz-sec-sso",))
            db.commit()
        r = self._json_post(c, "/api/security/toggle", {"enabled": True, "password": "peu-importe"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("SSO", r.get_json()["error"])
        with self._db():
            n = portal.get_db().execute(
                "SELECT COUNT(*) FROM login_attempts WHERE key=?", ("user:zz-sec-sso",)).fetchone()[0]
        self.assertEqual(n, 0, "le compteur d'échecs partagé avec /login ne doit pas bouger")
        self._nettoie("zz-sec-sso")

    def test_le_payload_dit_la_source(self):
        self._insere_local("zz-sec-local")
        self._nettoie("zz-sec-local")
        self._insere_local("zz-sec-local")
        c = self._client("zz-sec-local")
        r = c.get("/api/security")
        self.assertEqual(r.status_code, 200)
        corps = r.get_json()
        self.assertEqual(corps["password_managed_by"], "portail")
        self.assertTrue(corps["passkey_possible"])

    def test_whoami_porte_la_source(self):
        self._insere_local("zz-sec-who")
        c = self._client("zz-sec-who")
        corps = c.get("/api/whoami").get_json()
        self.assertEqual(corps["password_managed_by"], "portail")
        self.assertTrue(corps["local_account"])
        self._nettoie("zz-sec-who")


class SuppressionCompteTest(_BaseReglages):
    """`/api/account/delete` : partir soi-même, sans mentir sur ce qui reste."""

    def _patch_litellm(self):
        return patch.multiple(
            "litellm_client",
            delete_litellm_user=lambda u: True,
            revoke_litellm_key=lambda k: True,
        )

    def test_sans_confirmation_refuse(self):
        self._insere_local("zz-del-1")
        c = self._client("zz-del-1")
        r = self._json_post(c, "/api/account/delete", {"password": "Motdepasse1!"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["needs_confirm"])
        with self._db():
            self.assertIsNotNone(portal.get_db().execute(
                "SELECT 1 FROM local_users WHERE username='zz-del-1'").fetchone())
        self._nettoie("zz-del-1")

    def test_compte_local_sans_mot_de_passe_refuse(self):
        self._insere_local("zz-del-2")
        c = self._client("zz-del-2")
        r = self._json_post(c, "/api/account/delete", {"confirm": "DELETE"})
        self.assertEqual(r.status_code, 400)
        self._nettoie("zz-del-2")

    def test_mot_de_passe_incorrect_refuse(self):
        self._insere_local("zz-del-3")
        c = self._client("zz-del-3")
        with patch("auth.ldap_authenticate", return_value=(False, None, None)):
            r = self._json_post(c, "/api/account/delete",
                                {"confirm": "DELETE", "password": "faux"})
        # 400 : le portail répond « la confirmation est fausse », pas « tu n'es
        # pas authentifié » — un 401 ferait déconnecter l'utilisateur.
        self.assertEqual(r.status_code, 400)
        with self._db():
            self.assertIsNotNone(portal.get_db().execute(
                "SELECT 1 FROM local_users WHERE username='zz-del-3'").fetchone())
        self._nettoie("zz-del-3")

    def test_un_compte_local_n_interroge_pas_l_annuaire(self):
        """Le refus d'un mot de passe local ne doit pas ATTENDRE l'annuaire.

        Mesuré le 2026-09-17 en production : `POST /api/security/register/begin`
        mettait **13 s** à refuser un mot de passe faux, parce que la
        vérification locale échouée enchaînait sur un bind LDAP sur un annuaire
        muet — et le refus est le cas FRÉQUENT. Le worker gunicorn restait
        immobilisé tout ce temps (quatre fautes de frappe simultanées suffisaient
        à figer le portail). Le test verrouille l'intention sans dépendre d'une
        horloge : l'annuaire ne doit même pas être appelé.
        """
        self._insere_local("zz-del-5")
        c = self._client("zz-del-5")
        with patch("auth.ldap_authenticate") as annuaire:
            r = self._json_post(c, "/api/account/delete",
                                {"confirm": "DELETE", "password": "faux"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(annuaire.called,
                         "un compte dont le PORTAIL detient le mot de passe ne doit "
                         "pas interroger l'annuaire")
        self._nettoie("zz-del-5")

    def test_suppression_reussie_retire_l_acces(self):
        self._insere_local("zz-del-4")
        with self._db():
            db = portal.get_db()
            db.execute("INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, "
                       "created_at) VALUES ('zz-del-4','cle','sk-del-4','2026-09-14')")
            db.commit()
        c = self._client("zz-del-4")
        with self._patch_litellm():
            r = self._json_post(c, "/api/account/delete",
                                {"confirm": "DELETE", "password": "Motdepasse1!"})
        self.assertEqual(r.status_code, 200)
        corps = r.get_json()
        self.assertTrue(corps["ok"])
        self.assertTrue(corps["deleted"])
        with self._db():
            db = portal.get_db()
            self.assertIsNone(db.execute(
                "SELECT 1 FROM local_users WHERE username='zz-del-4'").fetchone())
            self.assertIsNone(db.execute(
                "SELECT 1 FROM api_keys WHERE username='zz-del-4'").fetchone())
        self._nettoie("zz-del-4")

    def test_dernier_admin_local_refuse(self):
        """Se couper la main laisserait le portail sans administrateur local."""
        self._insere_local("zz-del-admin", is_admin=1)
        with self._db():
            db = portal.get_db()
            # Aucun autre admin local actif : c'est la situation visée.
            db.execute("UPDATE local_users SET is_admin=0 WHERE username<>'zz-del-admin'")
            db.commit()
        c = self._client("zz-del-admin", is_admin=True)
        with self._patch_litellm():
            r = self._json_post(c, "/api/account/delete",
                                {"confirm": "DELETE", "password": "Motdepasse1!"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("administrateur", r.get_json()["error"])
        with self._db():
            self.assertIsNotNone(portal.get_db().execute(
                "SELECT 1 FROM local_users WHERE username='zz-del-admin'").fetchone())
        self._nettoie("zz-del-admin")

    def test_compte_d_annuaire_exige_le_mot_de_passe(self):
        """Un compte LDAP ne se purge pas sur le seul cookie.

        Le portail SAIT vérifier un mot de passe LDAP (`_verify_password_locked`
        interroge l'annuaire) : la justification d'origine (« un compte
        d'annuaire n'a aucun mot de passe que le portail puisse vérifier ») ne
        valait que pour le SSO. La suppression emporte clés API et enveloppe
        LiteLLM : elle exige donc la même preuve qu'en local. Ici l'annuaire est
        injoignable depuis les tests → refus, et RIEN n'est purgé.
        """
        self._insere_source("zz-del-ldap", "ldap")
        with self._db():
            db = portal.get_db()
            db.execute("INSERT OR REPLACE INTO user_prefs (username, lang) VALUES ('zz-del-ldap','fr')")
            db.commit()
        c = self._client("zz-del-ldap")
        with self._patch_litellm():
            sans = self._json_post(c, "/api/account/delete", {"confirm": "DELETE"})
            faux = self._json_post(c, "/api/account/delete",
                                   {"confirm": "DELETE", "password": "MauvaisMotDePasse1!"})
        self.assertEqual(sans.status_code, 400, sans.get_data(as_text=True))
        self.assertEqual(faux.status_code, 400, faux.get_data(as_text=True))
        with self._db():
            self.assertIsNotNone(portal.get_db().execute(
                "SELECT 1 FROM user_prefs WHERE username='zz-del-ldap'").fetchone(),
                "un refus ne doit rien effacer")
        self._nettoie("zz-del-ldap")

    def test_compte_sso_purge_toujours_sans_mot_de_passe(self):
        """Le compte SSO, lui, n'a aucun mot de passe vérifiable : on ne le bloque pas.

        Exiger une preuve impossible rendrait la sortie du portail inatteignable.
        """
        self._insere_source("zz-del-sso", "sso")
        with self._db():
            db = portal.get_db()
            db.execute("INSERT OR REPLACE INTO user_prefs (username, lang) VALUES ('zz-del-sso','fr')")
            db.commit()
        c = self._client("zz-del-sso")
        with self._patch_litellm():
            r = self._json_post(c, "/api/account/delete", {"confirm": "DELETE"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        corps = r.get_json()
        self.assertTrue(corps["ok"])
        self.assertFalse(corps["deleted"],
                         "un compte d'annuaire n'est pas supprimé par le portail")
        with self._db():
            self.assertIsNone(portal.get_db().execute(
                "SELECT 1 FROM user_prefs WHERE username='zz-del-sso'").fetchone())
        self._nettoie("zz-del-sso")


class BudgetAffichageTest(_BaseReglages):
    """`/api/settings` : la consommation est celle de la PÉRIODE, et elle le dit."""

    def test_le_payload_porte_la_date_de_remise_a_zero(self):
        faux = {"exists": True, "spend": 42, "max_budget": 100,
                "budget_reset_at": "2026-09-20T00:00:00"}
        c = self._client()
        with patch("settings_routes._litellm_user_info", return_value=faux):
            r = c.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        acct = r.get_json()["account"]
        self.assertEqual(acct["budget_reset_at"], "2026-09-20T00:00:00")
        self.assertTrue(acct["budget_duration"],
                        "la durée de l'enveloppe doit accompagner le plafond")

    def test_sans_enveloppe_la_date_reste_absente(self):
        """Pas de date inventée quand LiteLLM n'en donne pas."""
        faux = {"exists": False, "spend": 0, "max_budget": None, "budget_reset_at": ""}
        c = self._client()
        with patch("settings_routes._litellm_user_info", return_value=faux):
            r = c.get("/api/settings")
        self.assertIsNone(r.get_json()["account"]["budget_reset_at"])


if __name__ == "__main__":
    unittest.main()
