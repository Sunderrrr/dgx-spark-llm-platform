"""WebAuthn / passkeys — 2e facteur (non-TOTP).

On ne re-teste PAS la cryptographie (c'est la lib `webauthn`, testée en amont).
On teste notre branchement : que la réponse d'enregistrement générée par
`navigator.credentials.create` est acceptée et stockée, que le login avec 2FA
active (mot de passe valide) ne pose PAS la session mais renvoie un défi, que
l'assertion de `navigator.credentials.get` termine le login, et que la
suppression/désactivation exige une re-vérification par mot de passe.

Un `FakeAuthenticator` produit des réponses (P-256) que la lib accepte — on
vérifie ainsi le flux complet côté serveur sans matériel réel.
"""
import base64
import hashlib
import json
import secrets
import time
import unittest

import app as portal
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import encode_cbor
from werkzeug.security import generate_password_hash

RP_ID = "dgx.cronos.website"
ORIGIN = "https://dgx.cronos.website"


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


class FakeAuthenticator:
    """Authenticator logiciel de test (P-256) pour enregistrer puis asserter."""

    def __init__(self, rp_id=RP_ID, origin=ORIGIN):
        self.rp_id = rp_id
        self.origin = origin
        self.sk = ec.generate_private_key(ec.SECP256R1())
        n = self.sk.public_key().public_numbers()
        self.cose = encode_cbor({1: 2, 3: -7, -1: 1,
                                 -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})
        self.cred_id = b"fake-cred-" + secrets.token_bytes(8)

    def _client_data(self, typ, challenge_b64):
        return json.dumps(
            {"type": typ, "challenge": challenge_b64, "origin": self.origin, "crossOrigin": False},
            separators=(",", ":")).encode()

    def register(self, challenge_b64):
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        auth_data = (rp_hash + bytes([0x41]) + b"\x00\x00\x00\x00"
                     + b"\x00" * 16 + len(self.cred_id).to_bytes(2, "big")
                     + self.cred_id + self.cose)
        att_obj = encode_cbor({"fmt": "none", "authData": auth_data})
        cd = self._client_data("webauthn.create", challenge_b64)
        return {"id": _b64url(self.cred_id), "rawId": _b64url(self.cred_id),
                "type": "public-key",
                "response": {"clientDataJSON": _b64url(cd),
                             "attestationObject": _b64url(att_obj),
                             "transports": ["usb"]},
                "clientExtensionResults": {}}

    def authenticate(self, challenge_b64, counter=1):
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        acd = self._client_data("webauthn.get", challenge_b64)
        adata = rp_hash + bytes([0x01]) + counter.to_bytes(4, "big")
        sig = self.sk.sign(adata + hashlib.sha256(acd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": _b64url(self.cred_id), "rawId": _b64url(self.cred_id),
                "type": "public-key",
                "response": {"clientDataJSON": _b64url(acd),
                             "authenticatorData": _b64url(adata),
                             "signature": _b64url(sig), "userHandle": None},
                "clientExtensionResults": {}}


class WebAuthnTestCase(unittest.TestCase):
    def setUp(self):
        portal.app.config["TESTING"] = True
        self._clean()
        self._mkuser("eve", "pw")

    def tearDown(self):
        self._clean()

    def _clean(self):
        with portal.app.app_context():
            db = portal.get_db()
            for t in ("user_security", "webauthn_credentials", "pending_webauthn",
                      "user_sessions", "user_sources", "local_users"):
                db.execute(f"DELETE FROM {t}")
            db.commit()

    def _mkuser(self, username, password, is_admin=0, fullname=None):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO local_users "
                "(username, password_hash, fullname, is_admin, group_name, max_budget, enabled, created_at) "
                "VALUES (?,?,?,?,NULL,NULL,1,?)",
                (username, generate_password_hash(password), fullname or username, is_admin, time.time()))
            db.commit()

    def _client(self, username=None):
        """Client de test ; si username, ouvre une session (username+auth_at)."""
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"
            if username:
                s["username"] = username
                s["auth_at"] = int(time.time())
        return c

    def _register(self, c, username, label="Ma clé"):
        """Enregistre une passkey via l'API (retourne (fake, nonce de login))."""
        begin = c.post("/api/security/register/begin", headers={"X-CSRFToken": "tok"})
        self.assertEqual(begin.status_code, 200, begin.get_data(as_text=True))
        pk = begin.get_json()["publicKey"]
        fake = FakeAuthenticator()
        reg = fake.register(pk["challenge"])
        fin = c.post("/api/security/register/finish",
                     json={"nonce": begin.get_json()["nonce"], "credential": reg,
                           "label": label},
                     headers={"X-CSRFToken": "tok"})
        self.assertEqual(fin.status_code, 200, fin.get_data(as_text=True))
        self.assertTrue(fin.get_json()["ok"])
        return fake

    def test_routes_enregistrees(self):
        rules = {r.rule for r in portal.app.url_map.iter_rules()}
        for r in ("/api/security", "/api/security/register/begin",
                  "/api/security/register/finish", "/api/security/remove",
                  "/api/security/toggle", "/api/security/verify-login"):
            self.assertIn(r, rules, r)

    def test_api_security_requiert_session(self):
        c = portal.app.test_client()
        self.assertEqual(c.get("/api/security").status_code, 401)

    def test_etat_initial_desactive(self):
        c = self._client("eve")
        data = c.get("/api/security").get_json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["credentials"], [])

    def test_enregistrement_active_la_2fa(self):
        c = self._client("eve")
        self._register(c, "eve")
        data = c.get("/api/security").get_json()
        self.assertTrue(data["enabled"])
        self.assertEqual(len(data["credentials"]), 1)
        self.assertEqual(data["credentials"][0]["label"], "Ma clé")

    def test_register_finish_sans_nonce_refuse(self):
        c = self._client("eve")
        r = c.post("/api/security/register/finish", json={"credential": {}},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 400)

    def test_login_2fa_ne_pose_pas_session(self):
        c = self._client("eve")
        self._register(c, "eve")
        lc = self._client()
        r = lc.post("/login", data={"username": "eve", "password": "pw"},
                    headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertTrue(j["webauthn_required"])
        self.assertIn("nonce", j)
        with lc.session_transaction() as s:
            self.assertNotIn("username", s)

    def test_login_sans_2fa_redirige(self):
        # eve sans 2FA : login normal.
        c = self._client("eve")
        lc = self._client()
        r = lc.post("/login", data={"username": "eve", "password": "pw"},
                    headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 302)
        with lc.session_transaction() as s:
            self.assertEqual(s["username"], "eve")

    def test_verify_login_complete_la_session(self):
        c = self._client("eve")
        fake = self._register(c, "eve")
        lc = self._client()
        j = lc.post("/login", data={"username": "eve", "password": "pw"},
                    headers={"X-CSRFToken": "tok"}).get_json()
        self.assertTrue(j["webauthn_required"])
        r = lc.post("/api/security/verify-login",
                    json={"nonce": j["nonce"], "credential": fake.authenticate(j["publicKey"]["challenge"])},
                    headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        with lc.session_transaction() as s:
            self.assertEqual(s["username"], "eve")

    def test_verify_login_cle_inconnue_refuse(self):
        c = self._client("eve")
        self._register(c, "eve")
        lc = self._client()
        j = lc.post("/login", data={"username": "eve", "password": "pw"},
                    headers={"X-CSRFToken": "tok"}).get_json()
        other = FakeAuthenticator()
        r = lc.post("/api/security/verify-login",
                    json={"nonce": j["nonce"], "credential": other.authenticate(j["publicKey"]["challenge"])},
                    headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 401)

    def test_remove_requiert_mot_de_passe(self):
        c = self._client("eve")
        self._register(c, "eve")
        cred_id = c.get("/api/security").get_json()["credentials"][0]["credential_id"]
        # Sans mot de passe → 400.
        r = c.post("/api/security/remove", json={"credential_id": cred_id},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 400)
        # Mauvais mot de passe → 401.
        r = c.post("/api/security/remove", json={"credential_id": cred_id, "password": "bad"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 401)
        # Bon mot de passe → supprimé + désactivé (dernière clé).
        r = c.post("/api/security/remove", json={"credential_id": cred_id, "password": "pw"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = c.get("/api/security").get_json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["credentials"], [])

    def test_toggle_requiert_cle_et_mot_de_passe(self):
        c = self._client("eve")
        # Activer sans clé → 400.
        r = c.post("/api/security/toggle", json={"enabled": True, "password": "pw"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 400)
        self._register(c, "eve")
        # Bien désactiver / réactiver avec mot de passe correct.
        r = c.post("/api/security/toggle", json={"enabled": False, "password": "pw"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(c.get("/api/security").get_json()["enabled"])
        r = c.post("/api/security/toggle", json={"enabled": True, "password": "pw"},
                   headers={"X-CSRFToken": "tok"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(c.get("/api/security").get_json()["enabled"])

    def test_pending_defi_expire(self):
        from webauthn_routes import _pending_get, _pending_insert
        with portal.app.app_context():
            db = portal.get_db()
            _pending_insert("nonce1", "eve", "login", b"challenge")
            db.execute("UPDATE pending_webauthn SET expires_at=?", (time.time() - 10,))
            db.commit()
            self.assertIsNone(_pending_get("nonce1", "login"))
