"""Admin actions: what the UI asserts must be true.

Written on 2026-09-13, after an audit of the Admin part. Three classes of
defects are locked in there, all verified by reading the code before the fix:

1. **Lying by redirect.** About twenty routes answered
   `flash(...)` + `redirect(...)`. The flash is rendered by NO template (the
   UI is Next.js, `get_flashed_messages` exists nowhere in the repo) and the
   HTML body of the redirect made `res.json()` fail on the client, whose
   `catch` concluded « action done ». A refused model stop, a budget
   not applied on LiteLLM or a model registration failure thus displayed as
   successes. The single contract is `{ok, error}` + honest status, and the
   doubt (`incertain`) is distinguished from the refusal.

2. **Missing guardrails.** Editing an account had neither the « last
   admin » guard nor the self-protection that deletion and blocking have:
   one POST was enough to cut one's own hand, and the account-state
   revalidation at every request makes the access loss immediate. The
   twisted case — a group whose TWO members hold their admin rights from it
   — is covered explicitly, because `_dernier_admin_local` cannot see it.

3. **Quota leaks.** An expired grant was deleted even when the fallback to
   the base cap had failed (a temporary raise silently became permanent),
   and the quota resynchronization was unconditional — so a simple rename
   erased an ongoing grant.
"""
import os
import time
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import admin_routes
import app as portal
try:                                     # discovery runs tests/ as top level
    from settings_helper import live_settings
except ImportError:                      # single module: tests.test_x
    from tests.settings_helper import live_settings
import litellm_client
import local_users
import sidecars

ADMIN1 = 'ztest-adm1'
ADMIN2 = 'ztest-adm2'
SIMPLE = 'ztest-simple'
DIRECTORY_USER = 'ztest-annuaire'      # admin WITHOUT local row (LDAP/SSO case)
MDP = 'MotDePasse123'


class BaseAdmin(unittest.TestCase):
    """Fixture: two local admins, a plain account, a directory admin."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self._clean()
        for name, adm in ((ADMIN1, 1), (ADMIN2, 1), (SIMPLE, 0)):
            self._mkuser(name, adm)

    def tearDown(self):
        self._clean()

    def _mkuser(self, username, is_admin=0):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO local_users (username, password_hash, fullname, is_admin, "
                "group_name, max_budget, enabled, created_at) VALUES (?,?,?,?,NULL,NULL,1,?)",
                (username, generate_password_hash(MDP), username, is_admin, time.time()))
            db.commit()

    def _clean(self):
        with portal.app.app_context():
            db = portal.get_db()
            for t in ('audit_log', 'user_sessions', 'budget_grants', 'budget_requests'):
                for col in ('username',):
                    try:
                        db.execute(f"DELETE FROM {t} WHERE {col} LIKE 'ztest-%'")
                    except Exception:                            # noqa: BLE001
                        pass
            db.execute("DELETE FROM user_groups WHERE name LIKE 'ztest-%'")
            db.execute("DELETE FROM local_users WHERE username LIKE 'ztest-%'")
            db.execute("DELETE FROM model_configs WHERE name LIKE 'ztest-%'")
            db.execute("DELETE FROM announcements WHERE a LIKE 'ztest-%'")
            db.commit()

    def _uid(self, username):
        with portal.app.app_context():
            return portal.get_db().execute(
                "SELECT id FROM local_users WHERE username=?", (username,)).fetchone()['id']

    def _client(self, username, is_admin=True):
        """Admin session. No `sid`: an identifier with no row in `user_sessions` is
        refused by the guard (this is what makes a session revocable), and
        these tests are not about revocation."""
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['csrf'] = 'tok'
            s['username'] = username
            s['auth_at'] = int(time.time())
            s['is_admin'] = is_admin
        return c

    def _post(self, client, url, data=None):
        return client.post(url, data=data or {}, headers={'X-CSRFToken': 'tok'})

    def _audit(self, action):
        with portal.app.app_context():
            return portal.get_db().execute(
                "SELECT * FROM audit_log WHERE action=? ORDER BY id DESC", (action,)).fetchall()


class JsonContractTest(BaseAdmin):
    """A refusal must be reported as a refusal, never as a success."""

    def test_refused_stop_is_reported_as_a_failure(self):
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'runner_stop',
                          return_value=(False, 'runner injoignable (ConnectionError)', False)), \
             patch.object(admin_routes, 'notify_infra_alert_email'):
            r = self._post(c, '/admin/model/stop')
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertFalse(body['ok'])
        self.assertIn('injoignable', body['error'])

    def test_uncertain_stop_is_reported_as_doubt(self):
        """The runner did not answer: we do not claim the stop failed."""
        c = self._client(ADMIN1)
        reason = "Le runner n'a pas répondu dans le délai imparti (45 s) : l'arrêt est peut-être en cours."
        with patch.object(admin_routes, 'runner_stop', return_value=(False, reason, True)):
            r = self._post(c, '/admin/model/stop')
        self.assertEqual(r.status_code, 202)
        body = r.get_json()
        self.assertFalse(body['ok'])
        self.assertTrue(body['uncertain'])
        self.assertIn('peut-être en cours', body['error'])

    def test_refused_launch_alerts_infra(self):
        c = self._client(ADMIN1)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                       "VALUES ('ztest-modele', 'org/modele', '--ctx-size 4096', 'vllm', ?)",
                       (time.time(),))
            db.commit()
        with patch.object(admin_routes, 'runner_launch',
                          return_value=(False, 'flag not allowed: --x', False)), \
             patch.object(admin_routes, 'notify_infra_alert_email') as alert:
            r = self._post(c, '/admin/model/launch', {'model_name': 'ztest-modele'})
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.get_json()['ok'])
        alert.assert_called_once()

    def test_uncertain_launch_does_not_alert(self):
        """A DOUBT must not trigger an alert: it would invite clicking again,
        thus killing a model being loaded."""
        c = self._client(ADMIN1)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                       "VALUES ('ztest-modele', 'org/modele', '--ctx-size 4096', 'vllm', ?)",
                       (time.time(),))
            db.commit()
        with patch.object(admin_routes, 'runner_launch',
                          return_value=(False, 'délai dépassé (150 s)', True)), \
             patch.object(admin_routes, 'notify_infra_alert_email') as alert:
            r = self._post(c, '/admin/model/launch', {'model_name': 'ztest-modele'})
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.get_json()['uncertain'])
        alert.assert_not_called()

    def test_successful_launch_does_not_announce_model_at_spawn(self):
        """Accepting is not serving: the announcement goes out when the model
        answers, i.e. from _suivre_lancement, not from the route."""
        c = self._client(ADMIN1)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                       "VALUES ('ztest-modele', 'org/modele', '--ctx-size 4096', 'vllm', ?)",
                       (time.time(),))
            db.commit()
        with patch.object(admin_routes, 'runner_launch', return_value=(True, '', False)), \
             patch.object(admin_routes, 'add_announcement') as announcement:
            r = self._post(c, '/admin/model/launch', {'model_name': 'ztest-modele'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        announcement.assert_not_called()

    def test_sidecar_stop_reports_runner_reason(self):
        """« Échec du démarrage ocr. » did not say WHY: unreachable runner,
        missing container and transient error were identical."""
        class FakeResponse:
            ok = False
            status_code = 500
            text = ''

            @staticmethod
            def json():
                return {'detail': 'conteneur absent (docker start refusé)'}

        c = self._client(ADMIN1)
        with patch.object(sidecars.requests, 'post', return_value=FakeResponse()):
            r = self._post(c, '/admin/ocr/stop')
        self.assertEqual(r.status_code, 503)
        self.assertIn('conteneur absent', r.get_json()['error'])

    def test_global_settings_are_audited(self):
        c = self._client(ADMIN1)
        r = self._post(c, '/admin/settings',
                       {'default_key_budget': '150000000', 'default_key_duration': '7d'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        self.assertTrue(self._audit('settings.budget'),
                        "the global cap applies to every account without an override: "
                        "it must leave a trace")

    def test_maintenance_audited_and_warning_surfaced(self):
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'notify_maintenance_email', return_value=False), \
             live_settings(smtp_host='smtp.example', smtp_user='u', smtp_pass='p',
                      admin_email='a@example'):
            r = self._post(c, '/admin/maintenance/toggle')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertIn('warning', body)
        # We restore the original state: maintenance is global.
        self._post(c, '/admin/maintenance/toggle')
        self.assertTrue(self._audit('maintenance'))


class LastAdminGuardTest(BaseAdmin):
    """The portal must not be left without a local administrator."""

    def test_self_demotion_refused(self):
        c = self._client(ADMIN1)
        r = self._post(c, f'/admin/users/update/{self._uid(ADMIN1)}', {'is_admin': '0'})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.get_json()['ok'])
        with portal.app.app_context():
            still = portal.get_db().execute(
                "SELECT is_admin FROM local_users WHERE username=?", (ADMIN1,)).fetchone()
        self.assertEqual(still['is_admin'], 1, "the admin demoted themselves")

    def test_self_disable_refused(self):
        c = self._client(ADMIN1)
        r = self._post(c, f'/admin/users/update/{self._uid(ADMIN1)}', {'enabled': '0'})
        self.assertEqual(r.status_code, 409)

    def test_last_local_admin_protected(self):
        """Seen by a DIRECTORY admin (no local row), disabling the last local
        admin would leave the portal without local administration."""
        c = self._client(DIRECTORY_USER)
        r = self._post(c, f'/admin/users/update/{self._uid(ADMIN2)}', {'enabled': '0'})
        self.assertEqual(r.status_code, 200)          # ADMIN1 reste admin local
        r = self._post(c, f'/admin/users/update/{self._uid(ADMIN1)}', {'enabled': '0'})
        self.assertEqual(r.status_code, 409)
        self.assertIn('dernier administrateur local', r.get_json()['error'])

    def test_plain_account_demotable_without_guard(self):
        """The guard must not get in the way of the normal case."""
        c = self._client(ADMIN1)
        r = self._post(c, f'/admin/users/update/{self._uid(SIMPLE)}', {'enabled': '0'})
        self.assertEqual(r.status_code, 200)

    def test_admin_group_with_two_members_cannot_vanish(self):
        """The case `_dernier_admin_local` cannot see: each of the two members
        sees the other as admin, while removing both leaves
        none."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) "
                       "VALUES ('ztest-groupe', NULL, 1, ?)", (time.time(),))
            # BOTH local admins hold their rights from the group.
            db.execute("UPDATE local_users SET is_admin=0, group_name='ztest-groupe' "
                       "WHERE username IN (?,?)", (ADMIN1, ADMIN2))
            db.commit()
        c = self._client(DIRECTORY_USER)
        r = self._post(c, '/admin/groups/delete/ztest-groupe')
        self.assertEqual(r.status_code, 409)
        self.assertIn('administrateur local', r.get_json()['error'])
        with portal.app.app_context():
            encore = portal.get_db().execute(
                "SELECT COUNT(*) AS n FROM user_groups WHERE name='ztest-groupe'").fetchone()['n']
        self.assertEqual(encore, 1)

    def test_removing_group_admin_right_is_guarded(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) "
                       "VALUES ('ztest-groupe', NULL, 1, ?)", (time.time(),))
            db.execute("UPDATE local_users SET is_admin=0, group_name='ztest-groupe' "
                       "WHERE username IN (?,?)", (ADMIN1, ADMIN2))
            db.commit()
        c = self._client(DIRECTORY_USER)
        r = self._post(c, '/admin/groups/create',
                       {'name': 'ztest-groupe', 'is_admin': '0'})
        self.assertEqual(r.status_code, 409)


class QuotasTest(BaseAdmin):
    """A quota must neither be lost, nor become permanent by accident."""

    def test_budget_zero_refuse(self):
        """Measured: LiteLLM keeps max_budget=0, so the account is really capped
        at zero — « 0 pour lever la limite » cuts access."""
        c = self._client(ADMIN1)
        r = self._post(c, f'/admin/users/{SIMPLE}/budget/set', {'budget': '0'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('blocage', r.get_json()['error'])

    def test_grant_kept_if_rollback_fails(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (SIMPLE, 200000000, 250000000, '2020-01-01T00:00:00', '2020-01-01', '2020-01-01'))
            db.commit()
        with patch.object(admin_routes, '_litellm_user_info',
                          return_value={'max_budget': 250000000}), \
             patch.object(admin_routes, 'litellm_update_user_budget', return_value=False):
            with portal.app.app_context():
                brought_back = admin_routes.revert_expired_grants()
        self.assertEqual(brought_back, 0)
        with portal.app.app_context():
            remaining = portal.get_db().execute(
                "SELECT COUNT(*) AS n FROM budget_grants WHERE username=?", (SIMPLE,)).fetchone()['n']
        self.assertEqual(remaining, 1, "the row carried the expiry: deleting it made "
                                   "the raise permanent")
        self.assertTrue(self._audit('budget.grant_revert_echec'))

    def test_resync_sends_duration(self):
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT * FROM local_users WHERE username=?", (SIMPLE,)).fetchone()
        with patch.object(local_users, '_ensure_litellm_user'), \
             patch.object(local_users, 'litellm_update_user_budget', return_value=True) as maj:
            with portal.app.app_context():
                local_users._sync_local_user_budget(SIMPLE, row)
        self.assertTrue(maj.call_args.kwargs.get('budget_duration'),
                        "an update without a duration leaves an account without a reset window")


class ModelLifecycleTest(BaseAdmin):
    """Removing a model must not destroy the means to relaunch it."""

    def _model(self, name='ztest-modele'):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                       "VALUES (?, 'org/modele', '--ctx-size 4096', 'vllm', ?)",
                       (name, time.time()))
            db.commit()
            return db.execute("SELECT id FROM model_configs WHERE name=?", (name,)).fetchone()['id']

    def test_deleting_served_model_refused(self):
        """Its weights are read by the engine: we ask to stop it first, and
        nothing is touched — neither the catalog, nor the files."""
        mid = self._model()
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=['ztest-modele']), \
             patch.object(admin_routes, 'runner_delete_files') as erase:
            r = self._post(c, f'/admin/model/delete/{mid}', {'confirm': '1'})
        self.assertEqual(r.status_code, 409)
        erase.assert_not_called()
        with portal.app.app_context():
            self.assertIsNotNone(portal.get_db().execute(
                "SELECT 1 FROM model_configs WHERE id=?", (mid,)).fetchone())

    def test_deletion_erases_files(self):
        mid = self._model('ztest-modele3')
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=[]), \
             patch.object(admin_routes, '_unregister_litellm_model', return_value=True), \
             patch.object(admin_routes, 'runner_delete_files',
                          return_value=(True, 3 * 2**30, '')) as erase:
            r = self._post(c, f'/admin/model/delete/{mid}')
        self.assertEqual(r.status_code, 200)
        erase.assert_called_once_with('org/modele')
        self.assertIn('3.0 Gio', r.get_json()['message'])
        self.assertNotIn('warning', r.get_json())

    def test_shared_files_kept(self):
        """Another entry points to the same weights: we do not erase them."""
        mid = self._model('ztest-modele4')
        self._model('ztest-modele5')
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=[]), \
             patch.object(admin_routes, '_unregister_litellm_model', return_value=True), \
             patch.object(admin_routes, 'runner_delete_files') as erase:
            r = self._post(c, f'/admin/model/delete/{mid}')
        self.assertEqual(r.status_code, 200)
        erase.assert_not_called()
        self.assertIn('ztest-modele5', r.get_json()['warning'])

    def test_erase_failure_reported(self):
        mid = self._model('ztest-modele6')
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=[]), \
             patch.object(admin_routes, '_unregister_litellm_model', return_value=True), \
             patch.object(admin_routes, 'runner_delete_files',
                          return_value=(False, 0, 'runner injoignable')):
            r = self._post(c, f'/admin/model/delete/{mid}')
        self.assertEqual(r.status_code, 200)
        self.assertIn('runner injoignable', r.get_json()['warning'])

    def test_deletion_reports_deregistration_failure(self):
        mid = self._model('ztest-modele2')
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=[]), \
             patch.object(admin_routes, '_unregister_litellm_model', return_value=False), \
             patch.object(admin_routes, 'runner_delete_files', return_value=(True, 0, '')):
            r = self._post(c, f'/admin/model/delete/{mid}')
        self.assertEqual(r.status_code, 200)
        self.assertIn('warning', r.get_json())

    def test_voice_relaunch_subject_to_memory_guard(self):
        """The only one of the four media relaunches without it: an OOM there
        kills the served chat model (the biggest RSS on unified memory)."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO voice_configs (name, repo_id, added_at) VALUES "
                       "('ztest-voix', 'ResembleAI/chatterbox', ?)", (time.time(),))
            db.commit()
        c = self._client(ADMIN1)
        with patch.object(admin_routes, '_mem_guard', return_value='Mémoire insuffisante'), \
             patch.object(admin_routes, '_voice_launch') as lance:
            r = self._post(c, '/admin/voice/catalog/launch', {'voice_name': 'ztest-voix'})
        self.assertEqual(r.status_code, 507)
        lance.assert_not_called()

    def test_request_not_approved_if_routing_fails(self):
        """The requester received « ton modèle est disponible » before even
        knowing whether the LiteLLM registration succeeded."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO model_requests (username, fullname, model_id, status, created_at) "
                       "VALUES (?, ?, 'org/nouveau', 'pending', ?)", (SIMPLE, SIMPLE, time.time()))
            db.commit()
            rid = db.execute("SELECT id FROM model_requests WHERE username=?",
                             (SIMPLE,)).fetchone()['id']
        c = self._client(ADMIN1)
        with patch.object(admin_routes, '_register_litellm_model', return_value=False), \
             patch.object(admin_routes, 'send_user_email') as mail:
            r = self._post(c, f'/admin/update/{rid}', {'status': 'done'})
        self.assertEqual(r.status_code, 503)
        mail.assert_not_called()
        with portal.app.app_context():
            status_value = portal.get_db().execute(
                "SELECT status FROM model_requests WHERE id=?", (rid,)).fetchone()['status']
            portal.get_db().execute("DELETE FROM model_requests WHERE id=?", (rid,))
            portal.get_db().execute("DELETE FROM model_configs WHERE hf_model_id='org/nouveau'")
            portal.get_db().commit()
        self.assertEqual(status_value, 'pending', "the request must not be marked processed")

    def test_litellm_restored_after_creation_failure(self):
        """`_litellm_upsert` deletes the entry before recreating it: if the
        creation fails, a model that was answering must be put back in place."""
        entry = {'model_name': 'ztest-modele', 'litellm_params': {'model': 'openai/ztest-modele'},
                  'model_info': {'id': 'abc', 'db_model': True, 'max_input_tokens': 4096}}
        calls = []

        class FakeResponse:
            def __init__(self, code):
                self.status_code = code
                self.ok = code < 300

        def fake_post(url, **kwargs):
            calls.append(url)
            if url.endswith('/model/delete'):
                return FakeResponse(200)
            if len([u for u in calls if u.endswith('/model/new')]) == 1:
                return FakeResponse(500)                  # the creation fails
            return FakeResponse(200)                      # the restoration succeeds

        with patch.object(litellm_client, 'LITELLM_KEY', 'clef'), \
             patch.object(litellm_client, '_litellm_model_entry', return_value=('abc', entry)), \
             patch.object(litellm_client.requests, 'post', side_effect=fake_post):
            ok = litellm_client._litellm_upsert('ztest-modele', 'ztest-modele', 4096, 1024)
        self.assertFalse(ok)
        self.assertEqual(len([u for u in calls if u.endswith('/model/new')]), 2,
                         "l'entrée précédente n'a pas été restaurée")


class PlatformStateTest(BaseAdmin):
    """The diagnosis must no longer require a shell on the host."""

    def test_platform_state_exposes_expected_keys(self):
        c = self._client(ADMIN1)
        r = c.get('/admin/platform')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        for key in ('disk', 'backup', 'monitor', 'model', 'portal', 'maintenance', 'checked_at'):
            self.assertIn(key, d)
        self.assertIsInstance(d['disk']['free_gb'], float)
        # The mounted paths do not exist in the test container: the state must
        # say « non lisible », never pretend all is well.
        self.assertFalse(d['backup']['readable'])
        self.assertFalse(d['monitor']['readable'])

    def test_memory_directory_and_live_sessions(self):
        import admin_routes
        c = self._client(ADMIN1)
        with portal.app.app_context():
            db = portal.get_db()
            now = time.time()
            for sid, revoked in (('ztest-sess-vive', 0), ('ztest-sess-revoquee', 1)):
                db.execute("INSERT OR REPLACE INTO user_sessions (sid, username, auth_at, expires_at, "
                           "revoked, created_at) VALUES (?,?,?,?,?,?)",
                           (sid, ADMIN1, now, now + 3600, revoked, now))
            db.commit()
            live = db.execute("SELECT COUNT(*) FROM user_sessions WHERE revoked=0 "
                                  "AND expires_at > ?", (now,)).fetchone()[0]
        try:
            with patch.object(admin_routes, 'directory_states_for',
                              side_effect=lambda names: {n: 'absent' for n in names}):
                d = c.get('/admin/platform').get_json()
        finally:
            with portal.app.app_context():
                portal.get_db().execute("DELETE FROM user_sessions WHERE sid LIKE 'ztest-sess-%'")
                portal.get_db().commit()
        # Revoked sessions are not « active » (« 247 » for 229 real ones).
        self.assertEqual(d['portal']['active_sessions'], live)
        self.assertGreaterEqual(d['portal']['connected_users'], 1)
        self.assertEqual(d['memory']['low_gib'], 8)
        self.assertTrue(d['directory']['readable'])
        self.assertEqual(d['directory']['active'], 0)
        with patch.object(admin_routes, 'directory_states_for', return_value=None):
            self.assertFalse(c.get('/admin/platform').get_json()['directory']['readable'])

    def test_deleted_local_account_is_not_asked_to_the_directory(self):
        """A local account whose row was deleted (sources « local ») is not in
        LDAP by nature: it was counted « absent de l'annuaire » (2026-10-13)."""
        import admin_routes
        c = self._client(ADMIN1)
        with portal.app.app_context():
            db = portal.get_db()
            for name, src in (('ztest-ex-local', 'local'), ('ztest-ex-ldap', 'ldap,local')):
                db.execute("INSERT OR REPLACE INTO user_sources (username, sources) VALUES (?,?)", (name, src))
            db.commit()
        asked = []
        try:
            with patch.object(admin_routes, 'directory_states_for',
                              side_effect=lambda names: asked.extend(names) or {n: 'absent' for n in names}):
                c.get('/admin/platform').get_json()
        finally:
            with portal.app.app_context():
                portal.get_db().execute("DELETE FROM user_sources WHERE username LIKE 'ztest-ex-%'")
                portal.get_db().commit()
        self.assertNotIn('ztest-ex-local', asked)
        self.assertIn('ztest-ex-ldap', asked)

    def test_platform_state_refuses_non_admin(self):
        c = self._client(SIMPLE, is_admin=False)
        # 302 to /login for a browser request, 403 for an API call: in both
        # cases the platform state is not served.
        self.assertIn(c.get('/admin/platform').status_code, (302, 403))

    def test_json_contract_on_converted_route(self):
        """No admin action answers by redirect anymore."""
        c = self._client(ADMIN1)
        # We target `sidecars._sidecar_action`: it is the one _sidecar_stop_json
        # calls (patching the name re-exported by admin_routes would intercept nothing).
        with patch.object(sidecars, '_sidecar_action', return_value=(True, '')):
            r = self._post(c, '/admin/image/stop')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'application/json')
        self.assertTrue(r.get_json()['ok'])


if __name__ == '__main__':
    unittest.main()
