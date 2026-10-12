"""The USER SETTINGS routes, and their response contract.

Why this file exists: the actions of the « Clés API » tab (`POST /keys`)
and those of the settings (`/settings/avatar`, `/settings/appearance`)
answered `('', 204)` after a `flash(...)`. Yet **no template renders the
flashes** — the UI is Next.js —, so the user read « Clé créée ! » or « Clé
révoquée. » whatever happened. The serious case: LiteLLM unreachable, the
revocation failed and the key stayed VALID while the user believed it dead.

These tests lock in the honest contract: a success says `{ok: true}`, a
refusal carries an HTTP status saying it (`404` unknown key, `409` request
already pending, `503` upstream unreachable, `400` invalid value).

No network call: `create_litellm_key` / `revoke_litellm_key` are replaced.
"""
import time
import unittest
from unittest.mock import patch

import app as portal
import settings_routes
from werkzeug.security import generate_password_hash


class _SettingsBase(unittest.TestCase):
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

    def _insert_local(self, username, password="Motdepasse1!", is_admin=0, enabled=1):
        """Creates the LOCAL account the routes read (`local_users`)."""
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM local_users WHERE username=?", (username,))
            db.execute(
                "INSERT INTO local_users (username, password_hash, fullname, is_admin, "
                "group_name, max_budget, enabled, created_at) VALUES (?,?,?,?,NULL,NULL,?,?)",
                (username, generate_password_hash(password), "Compte de test", is_admin,
                 enabled, "2026-09-14T00:00:00"))
            db.commit()

    def _insert_source(self, username, sources):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM user_sources WHERE username=?", (username,))
            db.execute(
                "INSERT INTO user_sources (username, sources, fullname, last_source, last_seen, "
                "last_is_admin) VALUES (?,?,?,?,?,NULL)",
                (username, sources, "Compte de test", sources.split(",")[0], "2026-09-14T00:00:00"))
            db.commit()

    def _cleanup(self, username):
        with self._db():
            db = portal.get_db()
            for table in ("local_users", "user_sources", "api_keys", "user_sessions",
                          "user_prefs", "login_attempts"):
                try:
                    db.execute(f"DELETE FROM {table} WHERE username=?", (username,))
                except Exception:
                    pass
            db.commit()


class ApiKeysTest(_SettingsBase):
    """`POST /keys`: creation, revocation, budget request."""

    def _insert_key(self, value="sk-test-123", alias="poste-test", user="demo"):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM api_keys WHERE key_value=?", (value,))
            db.execute(
                "INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) "
                "VALUES (?,?,?,?)", (user, alias, value, "2026-09-14T00:00:00"))
            db.commit()

    def test_successful_creation_returns_key(self):
        """The value goes back to the UI: the only moment one sees it."""
        c = self._client()
        with patch.object(portal, "create_litellm_key", return_value="sk-neuve-999"):
            r = self._post(c, "/keys", {"action": "create", "key_name": "mon-laptop"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["key"], "sk-neuve-999")
        self.assertEqual(body["key_alias"], "mon-laptop")
        with self._db():
            line = portal.get_db().execute(
                "SELECT key_alias FROM api_keys WHERE key_value=?",
                ("sk-neuve-999",)).fetchone()
        self.assertIsNotNone(line, "la clé créée doit être consignée en base")
        self.assertEqual(line["key_alias"], "mon-laptop")

    def test_failed_creation_says_503_and_does_not_lie(self):
        """LiteLLM mute: before, the UI announced « Clé créée ! »."""
        c = self._client()
        with patch.object(portal, "create_litellm_key", return_value=None):
            r = self._post(c, "/keys", {"action": "create", "key_name": "x"})
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["error"])
        with self._db():
            line = portal.get_db().execute(
                "SELECT 1 FROM api_keys WHERE key_alias='x'").fetchone()
        self.assertIsNone(line, "no key must be recorded when creation fails")

    def test_creation_without_name_generates_alias(self):
        c = self._client()
        seen = {}

        def _fake(alias, username, is_admin=False):
            seen["alias"] = alias
            return "sk-auto-1"

        with patch.object(portal, "create_litellm_key", side_effect=_fake):
            r = self._post(c, "/keys", {"action": "create", "key_name": "  "})
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(seen["alias"].startswith("demo-"),
                        f"alias généré inattendu : {seen['alias']!r}")

    def test_revoking_other_accounts_key_refused(self):
        """Anti-IDOR: the key exists, but not on THIS account."""
        self._insert_key(value="sk-autrui", user="autre")
        c = self._client("demo")
        with patch.object(portal, "revoke_litellm_key", return_value=True):
            r = self._post(c, "/keys", {"action": "revoke", "key": "sk-autrui"})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()["ok"])

    def test_failed_revocation_says_key_still_valid(self):
        """The dangerous lie: « Clé révoquée. » while it still works."""
        self._insert_key(value="sk-vive")
        c = self._client()
        with patch.object(portal, "revoke_litellm_key", return_value=False):
            r = self._post(c, "/keys", {"action": "revoke", "key": "sk-vive"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("encore valide", r.get_json()["error"])
        with self._db():
            line = portal.get_db().execute(
                "SELECT 1 FROM api_keys WHERE key_value='sk-vive'").fetchone()
        self.assertIsNotNone(line, "a failed revocation must NOT remove the row")

    def test_successful_revocation_deletes_row(self):
        self._insert_key(value="sk-amortir")
        c = self._client()
        with patch.object(portal, "revoke_litellm_key", return_value=True):
            r = self._post(c, "/keys", {"action": "revoke", "key": "sk-amortir"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with self._db():
            line = portal.get_db().execute(
                "SELECT 1 FROM api_keys WHERE key_value='sk-amortir'").fetchone()
        self.assertIsNone(line)

    def test_budget_request_already_pending_refused(self):
        """The button displayed « Demande envoyée ! » while the server refused."""
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
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "already_pending",
                         "a stable refusal must carry a translatable code")

    def test_budget_request_inserted(self):
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

    def test_unknown_action_refused(self):
        c = self._client()
        r = self._post(c, "/keys", {"action": "explose"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_get_stays_bodiless(self):
        """The GET is not a page: the Next.js UI carries the screen."""
        c = self._client()
        self.assertEqual(c.get("/keys").status_code, 204)


class McpActivationTest(_SettingsBase):
    """« juste les active pas temps que les user n'on pas set les api key ».

    A server registered without its key sits DISABLED — and writing the
    authorization header is what switches it on. Nothing is ever activated
    behind the user's back, and nothing is disabled either (a key-less server
    like context7 is managed by the toggle).
    """

    USER = "mcp-test"

    def tearDown(self):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM mcp_servers WHERE username=?", (self.USER,))
            db.commit()

    def _insert(self, auth=None, active=0):
        with self._db():
            db = portal.get_db()
            db.execute(
                "INSERT INTO mcp_servers (username, name, url, auth_header, description, "
                "allowed_tools, enabled, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (self.USER, "github", "https://api.githubcopilot.com/mcp/", auth,
                 "Dépôts GitHub", "", active, "2026-10-05T00:00:00"))
            db.commit()
            return db.execute("SELECT id FROM mcp_servers WHERE username=?",
                              (self.USER,)).fetchone()['id']

    def _state(self, ident):
        with self._db():
            r = portal.get_db().execute(
                "SELECT enabled, auth_header FROM mcp_servers WHERE id=?", (ident,)).fetchone()
            return (r['enabled'], r['auth_header'])

    def _update(self, client, ident, **data):
        base = {"action": "update", "id": str(ident), "name": "github",
                "url": "https://api.githubcopilot.com/mcp/", "description": "Dépôts GitHub",
                "allowed_tools": ""}
        base.update(data)
        with patch.object(settings_routes, "validate_mcp_url", return_value=(True, '')), \
             patch.object(settings_routes, "MCPClient") as MC:
            MC.return_value.initialize.return_value = None
            MC.return_value.list_tools.return_value = [{"name": "list_issues"}]
            return self._post(client, "/mcp", base)

    def test_writing_key_enables_server(self):
        ident = self._insert(auth=None, active=0)
        r = self._update(self._client(self.USER), ident, auth_header="Bearer ghp_test")
        self.assertTrue(r.get_json().get('ok'), r.get_json())
        self.assertEqual(self._state(ident), (1, "Bearer ghp_test"))

    def test_without_key_server_stays_inactive(self):
        """Redisplaying the form without touching the key keeps it off: an
        incomplete configuration must never become active by accident."""
        ident = self._insert(auth=None, active=0)
        r = self._update(self._client(self.USER), ident, auth_header="")
        self.assertTrue(r.get_json().get('ok'), r.get_json())
        self.assertEqual(self._state(ident), (0, None))

    def test_already_active_server_stays_on(self):
        """A key-less server (context7…) toggled ON stays ON when edited."""
        ident = self._insert(auth=None, active=1)
        r = self._update(self._client(self.USER), ident, auth_header="")
        self.assertTrue(r.get_json().get('ok'), r.get_json())
        self.assertEqual(self._state(ident), (1, None))


class AvatarTest(_SettingsBase):
    """`POST /settings/avatar`: invisible `flash` → readable response."""

    def test_unknown_avatar_refused_400(self):
        c = self._client()
        r = self._post(c, "/settings/avatar", {"avatar_id": "../../etc/passwd"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_valid_avatar_saved(self):
        with self._db():
            avatars = portal.AVATAR_IDS
        self.assertTrue(avatars, "no known avatar: the test would prove nothing")
        c = self._client()
        r = self._post(c, "/settings/avatar", {"avatar_id": avatars[0]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with self._db():
            line = portal.get_db().execute(
                "SELECT avatar_id FROM user_prefs WHERE username='demo'").fetchone()
        self.assertIsNotNone(line)
        self.assertEqual(line["avatar_id"], avatars[0])

    def test_generated_avatar_resets_to_null(self):
        """The « avatar généré » choice (empty value) must be able to go back.

        It is `NULL` that means « no logo chosen, so an avatar created from
        the nickname »: without this way back, an account that had taken
        a brand logo could never return to the default avatar — and the
        empty value was not even accepted (400).
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
            line = portal.get_db().execute(
                "SELECT avatar_id FROM user_prefs WHERE username='demo'").fetchone()
        self.assertIsNone(line["avatar_id"])

    def test_generated_avatar_accepted_spelled_out(self):
        """`genere` is accepted as a synonym of the empty value."""
        c = self._client()
        r = self._post(c, "/settings/avatar", {"avatar_id": "genere"})
        self.assertEqual(r.status_code, 200)
        with self._db():
            line = portal.get_db().execute(
                "SELECT avatar_id FROM user_prefs WHERE username='demo'").fetchone()
        self.assertIsNone(line["avatar_id"])


class AppearanceTest(_SettingsBase):
    """`POST /settings/appearance`: the refusal carried the success status."""

    def test_unknown_theme_refused_400(self):
        c = self._client()
        r = self._post(c, "/settings/appearance", {"theme_id": "rose-fluo"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_unknown_language_refused_400(self):
        c = self._client()
        r = self._post(c, "/settings/appearance", {"lang": "de"})
        self.assertEqual(r.status_code, 400)

    def test_valid_theme_and_language_saved(self):
        with self._db():
            theme = portal.THEME_IDS[0]
        c = self._client()
        r = self._post(c, "/settings/appearance", {"theme_id": theme, "lang": "fr"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with self._db():
            line = portal.get_db().execute(
                "SELECT theme_id, lang FROM user_prefs WHERE username='demo'").fetchone()
        self.assertEqual(line["theme_id"], theme)
        self.assertEqual(line["lang"], "fr")


class KeyRenameTest(_SettingsBase):
    """`action=rename`: the alias is what lets one find their key back."""

    def _insert_key(self, value="sk-ren-1", alias="poste-test", user="demo"):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM api_keys WHERE key_value=?", (value,))
            db.execute(
                "INSERT OR REPLACE INTO api_keys (username, key_alias, key_value, created_at) "
                "VALUES (?,?,?,?)", (user, alias, value, "2026-09-14T00:00:00"))
            db.commit()

    def _alias(self, value):
        with self._db():
            line = portal.get_db().execute(
                "SELECT key_alias FROM api_keys WHERE key_value=?", (value,)).fetchone()
        return line["key_alias"] if line else None

    def test_rename_succeeds_on_both_sides(self):
        """Portal AND LiteLLM: else the two views diverge."""
        self._insert_key()
        c = self._client()
        calls = []
        with patch.object(portal, "rename_litellm_key",
                          side_effect=lambda k, a: calls.append((k, a)) or True):
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-ren-1",
                                        "key_name": "portable-nora"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self._alias("sk-ren-1"), "portable-nora")
        self.assertEqual(calls, [("sk-ren-1", "portable-nora")],
                         "l'alias doit aussi partir chez LiteLLM")

    def test_renaming_other_accounts_key_refused(self):
        self._insert_key(value="sk-autrui-ren", user="autre")
        c = self._client("demo")
        with patch.object(portal, "rename_litellm_key", return_value=True) as fake:
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-autrui-ren",
                                        "key_name": "a-moi"})
        self.assertEqual(r.status_code, 404)
        fake.assert_not_called()
        self.assertEqual(self._alias("sk-autrui-ren"), "poste-test")

    def test_unreachable_litellm_keeps_old_name(self):
        """A half-done rename would be worse than no rename."""
        self._insert_key()
        c = self._client()
        with patch.object(portal, "rename_litellm_key", return_value=False):
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-ren-1",
                                        "key_name": "portable-nora"})
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self._alias("sk-ren-1"), "poste-test",
                         "the portal must not rename if LiteLLM refused")

    def test_taken_alias_refused(self):
        self._insert_key(value="sk-a", alias="portable")
        self._insert_key(value="sk-b", alias="bureau")
        c = self._client()
        with patch.object(portal, "rename_litellm_key", return_value=True) as fake:
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-b",
                                        "key_name": "portable"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "alias_in_use")
        fake.assert_not_called()

    def test_empty_name_refused(self):
        self._insert_key()
        c = self._client()
        with patch.object(portal, "rename_litellm_key", return_value=True) as fake:
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-ren-1",
                                        "key_name": "   "})
        self.assertEqual(r.status_code, 400)
        fake.assert_not_called()
        self.assertEqual(self._alias("sk-ren-1"), "poste-test")

    def test_name_sanitized_and_bounded(self):
        """A 4000-character alias would break the list display."""
        self._insert_key()
        c = self._client()
        with patch.object(portal, "rename_litellm_key", return_value=True):
            r = self._post(c, "/keys", {"action": "rename", "key": "sk-ren-1",
                                        "key_name": "mon portable " + "x" * 100})
        self.assertEqual(r.status_code, 200)
        alias = self._alias("sk-ren-1")
        self.assertLessEqual(len(alias), 40)
        self.assertNotIn(" ", alias)
        self.assertTrue(alias.startswith("mon-portable"))


class PasswordSourceTest(_SettingsBase):
    """Who holds the password: the question deciding what we display."""

    def test_local_account(self):
        self._insert_local("zz-gestion-local")
        with self._db():
            info = portal.password_management("zz-gestion-local")
        self.assertEqual(info["management"], "portal")
        self.assertTrue(info["passkey"])
        self._cleanup("zz-gestion-local")

    def test_ldap_account(self):
        self._insert_source("zz-gestion-ldap", "ldap")
        with self._db():
            info = portal.password_management("zz-gestion-ldap")
        self.assertEqual(info["management"], "ldap-directory")
        self.assertTrue(info["passkey"], "2FA stays possible for an LDAP account")
        self._cleanup("zz-gestion-ldap")

    def test_sso_account(self):
        self._insert_source("zz-gestion-sso", "sso")
        with self._db():
            info = portal.password_management("zz-gestion-sso")
        self.assertEqual(info["management"], "sso-provider")
        self.assertFalse(info["passkey"],
                         "an SSO account has no password checkable here")
        self._cleanup("zz-gestion-sso")

    def test_account_without_known_source(self):
        """Missing data ≠ no password: we do not guess."""
        self._cleanup("zz-gestion-inconnu")
        with self._db():
            info = portal.password_management("zz-gestion-inconnu")
        self.assertEqual(info["management"], "unknown")
        self.assertFalse(info["local"])

    def test_local_and_sso_local_is_authoritative(self):
        """The sources are CUMULATIVE: announcing « SSO » would be false."""
        self._insert_local("zz-gestion-mixte")
        self._insert_source("zz-gestion-mixte", "local,sso")
        with self._db():
            info = portal.password_management("zz-gestion-mixte")
        self.assertEqual(info["management"], "portal")
        self._cleanup("zz-gestion-mixte")


class AccountSecurityTest(_SettingsBase):
    """`/api/security`: say why a passkey is not offerable."""

    def test_sso_refuses_check_without_counting_failure(self):
        """An SSO account trying its password must not lock itself out."""
        self._insert_source("zz-sec-sso", "sso")
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
        self.assertEqual(n, 0, "the failure counter shared with /login must not move")
        self._cleanup("zz-sec-sso")

    def test_payload_says_source(self):
        self._insert_local("zz-sec-local")
        self._cleanup("zz-sec-local")
        self._insert_local("zz-sec-local")
        c = self._client("zz-sec-local")
        r = c.get("/api/security")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["password_managed_by"], "portal")
        self.assertTrue(body["passkey_possible"])

    def test_whoami_carries_source(self):
        self._insert_local("zz-sec-who")
        c = self._client("zz-sec-who")
        body = c.get("/api/whoami").get_json()
        self.assertEqual(body["password_managed_by"], "portal")
        self.assertTrue(body["local_account"])
        self._cleanup("zz-sec-who")


class AccountDeletionTest(_SettingsBase):
    """`/api/account/delete`: leave on one's own, without lying about what remains."""

    def _patch_litellm(self):
        return patch.multiple(
            "litellm_client",
            delete_litellm_user=lambda u: True,
            revoke_litellm_key=lambda k: True,
        )

    def test_without_confirmation_refused(self):
        self._insert_local("zz-del-1")
        c = self._client("zz-del-1")
        r = self._json_post(c, "/api/account/delete", {"password": "Motdepasse1!"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["needs_confirm"])
        with self._db():
            self.assertIsNotNone(portal.get_db().execute(
                "SELECT 1 FROM local_users WHERE username='zz-del-1'").fetchone())
        self._cleanup("zz-del-1")

    def test_local_account_without_password_refused(self):
        self._insert_local("zz-del-2")
        c = self._client("zz-del-2")
        r = self._json_post(c, "/api/account/delete", {"confirm": "DELETE"})
        self.assertEqual(r.status_code, 400)
        self._cleanup("zz-del-2")

    def test_wrong_password_refused(self):
        self._insert_local("zz-del-3")
        c = self._client("zz-del-3")
        with patch("auth.ldap_authenticate", return_value=(False, None, None)):
            r = self._json_post(c, "/api/account/delete",
                                {"confirm": "DELETE", "password": "faux"})
        # 400: the portal answers « the confirmation is wrong », not « you are
        # not authenticated » — a 401 would log the user out.
        self.assertEqual(r.status_code, 400)
        with self._db():
            self.assertIsNotNone(portal.get_db().execute(
                "SELECT 1 FROM local_users WHERE username='zz-del-3'").fetchone())
        self._cleanup("zz-del-3")

    def test_local_account_does_not_query_directory(self):
        """The refusal of a local password must not WAIT for the directory.

        Measured on 2026-09-17 in production: `POST /api/security/register/begin`
        took **13 s** to refuse a wrong password, because the failed local
        check chained onto an LDAP bind against a mute directory — and the
        refusal is the FREQUENT case. The gunicorn worker stayed stuck all
        that time (four simultaneous typos were enough to freeze the
        portal). The test locks in the intent without depending on a
        clock: the directory must not even be called.
        """
        self._insert_local("zz-del-5")
        c = self._client("zz-del-5")
        with patch("auth.ldap_authenticate") as directory_states:
            r = self._json_post(c, "/api/account/delete",
                                {"confirm": "DELETE", "password": "faux"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(directory_states.called,
                         "an account whose password the PORTAL holds must "
                         "not query the directory")
        self._cleanup("zz-del-5")

    def test_successful_deletion_removes_access(self):
        self._insert_local("zz-del-4")
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
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["deleted"])
        with self._db():
            db = portal.get_db()
            self.assertIsNone(db.execute(
                "SELECT 1 FROM local_users WHERE username='zz-del-4'").fetchone())
            self.assertIsNone(db.execute(
                "SELECT 1 FROM api_keys WHERE username='zz-del-4'").fetchone())
        self._cleanup("zz-del-4")

    def test_last_local_admin_refused(self):
        """Cutting one's own hand would leave the portal without a local admin."""
        self._insert_local("zz-del-admin", is_admin=1)
        with self._db():
            db = portal.get_db()
            # No other active local admin: this is the targeted situation.
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
        self._cleanup("zz-del-admin")

    def test_directory_account_requires_password(self):
        """An LDAP account is not purged on the cookie alone.

        The portal KNOWS how to check an LDAP password
        (`_verify_password_locked` queries the directory): the original
        justification (« a directory account has no password the portal can
        check ») only held for SSO. Deletion takes away
        API keys and the LiteLLM envelope: it demands the same proof as for
        a local one. Here the directory is unreachable → nothing is purged.
        """
        self._insert_source("zz-del-ldap", "ldap")
        with self._db():
            db = portal.get_db()
            db.execute("INSERT OR REPLACE INTO user_prefs (username, lang) VALUES ('zz-del-ldap','fr')")
            db.commit()
        c = self._client("zz-del-ldap")
        with self._patch_litellm():
            without = self._json_post(c, "/api/account/delete", {"confirm": "DELETE"})
            fake = self._json_post(c, "/api/account/delete",
                                   {"confirm": "DELETE", "password": "MauvaisMotDePasse1!"})
        self.assertEqual(without.status_code, 400, without.get_data(as_text=True))
        self.assertEqual(fake.status_code, 400, fake.get_data(as_text=True))
        with self._db():
            self.assertIsNotNone(portal.get_db().execute(
                "SELECT 1 FROM user_prefs WHERE username='zz-del-ldap'").fetchone(),
                "a refusal must erase nothing")
        self._cleanup("zz-del-ldap")

    def test_sso_account_always_purged_without_password(self):
        """The SSO account, for its part, has no checkable password: we do not block it.

        Requiring impossible proof would make leaving the portal unreachable.
        """
        self._insert_source("zz-del-sso", "sso")
        with self._db():
            db = portal.get_db()
            db.execute("INSERT OR REPLACE INTO user_prefs (username, lang) VALUES ('zz-del-sso','fr')")
            db.commit()
        c = self._client("zz-del-sso")
        with self._patch_litellm():
            r = self._json_post(c, "/api/account/delete", {"confirm": "DELETE"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["deleted"],
                         "a directory account is not deleted by the portal")
        with self._db():
            self.assertIsNone(portal.get_db().execute(
                "SELECT 1 FROM user_prefs WHERE username='zz-del-sso'").fetchone())
        self._cleanup("zz-del-sso")


class BudgetDisplayTest(_SettingsBase):
    """`/api/settings`: the consumption is the PERIOD's, and it says so."""

    def test_payload_carries_reset_date(self):
        fake = {"exists": True, "spend": 42, "max_budget": 100,
                "budget_reset_at": "2026-09-20T00:00:00"}
        c = self._client()
        with patch("settings_routes._litellm_user_info", return_value=fake):
            r = c.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        acct = r.get_json()["account"]
        self.assertEqual(acct["budget_reset_at"], "2026-09-20T00:00:00")
        self.assertTrue(acct["budget_duration"],
                        "the envelope duration must go with the cap")

    def test_without_envelope_date_stays_absent(self):
        """No invented date when LiteLLM gives none."""
        fake = {"exists": False, "spend": 0, "max_budget": None, "budget_reset_at": ""}
        c = self._client()
        with patch("settings_routes._litellm_user_info", return_value=fake):
            r = c.get("/api/settings")
        self.assertIsNone(r.get_json()["account"]["budget_reset_at"])


if __name__ == "__main__":
    unittest.main()
