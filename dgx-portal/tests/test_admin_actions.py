"""Admin actions: what the UI asserts must be true.

Written on 2026-09-13, after an audit of the Admin part. Three classes of
defects are locked in there, all verified by reading the code before the fix:

1. **Lying by redirect.** About twenty routes answered
   `flash(...)` + `redirect(...)`. The flash is rendered by NO template (the
   UI is Next.js, `get_flashed_messages` exists nowhere in the repo) and the
   HTML body of the redirect made `res.json()` fail on the client, whose
   `catch` concluded « action effectuée ». A refused model stop, a budget
   not applied on LiteLLM or a model registration failure thus displayed as
   successes. The single contract is `{ok, error}` + honest status, and the
   doubt (`incertain`) is distinguished from the refusal.

2. **Missing guardrails.** Editing an account had neither the « dernier
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
import time
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import admin_routes
import app as portal
import litellm_client
import local_users
import sidecars

ADMIN1 = 'ztest-adm1'
ADMIN2 = 'ztest-adm2'
SIMPLE = 'ztest-simple'
ANNUAIRE = 'ztest-annuaire'      # admin WITHOUT local row (LDAP/SSO case)
MDP = 'MotDePasse123'


class BaseAdmin(unittest.TestCase):
    """Socle : deux admins locaux, un compte simple, un admin d'annuaire."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self._clean()
        for nom, adm in ((ADMIN1, 1), (ADMIN2, 1), (SIMPLE, 0)):
            self._mkuser(nom, adm)

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


class ContratJsonTest(BaseAdmin):
    """A refusal must be reported as a refusal, never as a success."""

    def test_arret_refuse_est_rapporte_comme_un_echec(self):
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'runner_stop',
                          return_value=(False, 'runner injoignable (ConnectionError)', False)), \
             patch.object(admin_routes, 'notify_infra_alert_email'):
            r = self._post(c, '/admin/model/stop')
        self.assertEqual(r.status_code, 502)
        corps = r.get_json()
        self.assertFalse(corps['ok'])
        self.assertIn('injoignable', corps['error'])

    def test_arret_incertain_est_signale_comme_doute(self):
        """The runner did not answer: we do not claim the stop failed."""
        c = self._client(ADMIN1)
        motif = "Le runner n'a pas répondu dans le délai imparti (45 s) : l'arrêt est peut-être en cours."
        with patch.object(admin_routes, 'runner_stop', return_value=(False, motif, True)):
            r = self._post(c, '/admin/model/stop')
        self.assertEqual(r.status_code, 202)
        corps = r.get_json()
        self.assertFalse(corps['ok'])
        self.assertTrue(corps['incertain'])
        self.assertIn('peut-être en cours', corps['error'])

    def test_lancement_refuse_alerte_l_infra(self):
        c = self._client(ADMIN1)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                       "VALUES ('ztest-modele', 'org/modele', '--ctx-size 4096', 'vllm', ?)",
                       (time.time(),))
            db.commit()
        with patch.object(admin_routes, 'runner_launch',
                          return_value=(False, 'flag not allowed: --x', False)), \
             patch.object(admin_routes, 'notify_infra_alert_email') as alerte:
            r = self._post(c, '/admin/model/launch', {'model_name': 'ztest-modele'})
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()['ok'])
        alerte.assert_called_once()

    def test_lancement_incertain_n_alerte_pas(self):
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
             patch.object(admin_routes, 'notify_infra_alert_email') as alerte:
            r = self._post(c, '/admin/model/launch', {'model_name': 'ztest-modele'})
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.get_json()['incertain'])
        alerte.assert_not_called()

    def test_lancement_reussi_n_annonce_pas_le_modele_au_spawn(self):
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
             patch.object(admin_routes, 'add_announcement') as annonce:
            r = self._post(c, '/admin/model/launch', {'model_name': 'ztest-modele'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        annonce.assert_not_called()

    def test_arret_sidecar_rapporte_le_motif_du_runner(self):
        """« Échec du démarrage ocr. » did not say WHY: unreachable runner,
        missing container and transient error were identical."""
        class Reponse:
            ok = False
            status_code = 500
            text = ''

            @staticmethod
            def json():
                return {'detail': 'conteneur absent (docker start refusé)'}

        c = self._client(ADMIN1)
        with patch.object(sidecars.requests, 'post', return_value=Reponse()):
            r = self._post(c, '/admin/ocr/stop')
        self.assertEqual(r.status_code, 502)
        self.assertIn('conteneur absent', r.get_json()['error'])

    def test_parametres_globaux_sont_audites(self):
        c = self._client(ADMIN1)
        r = self._post(c, '/admin/settings',
                       {'default_key_budget': '150000000', 'default_key_duration': '7d'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        self.assertTrue(self._audit('settings.budget'),
                        "le plafond global s'applique à tout compte sans override : "
                        "il doit laisser une trace")

    def test_maintenance_auditee_et_avertissement_surface(self):
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'notify_maintenance_email', return_value=False), \
             patch.object(admin_routes, 'SMTP_HOST', 'smtp.example'), \
             patch.object(admin_routes, 'SMTP_USER', 'u'), \
             patch.object(admin_routes, 'SMTP_PASS', 'p'), \
             patch.object(admin_routes, 'ADMIN_EMAIL', 'a@example'):
            r = self._post(c, '/admin/maintenance/toggle')
        self.assertEqual(r.status_code, 200)
        corps = r.get_json()
        self.assertTrue(corps['ok'])
        self.assertIn('warning', corps)
        # We restore the original state: maintenance is global.
        self._post(c, '/admin/maintenance/toggle')
        self.assertTrue(self._audit('maintenance'))


class GardeDernierAdminTest(BaseAdmin):
    """The portal must not be left without a local administrator."""

    def test_auto_retrogradation_refusee(self):
        c = self._client(ADMIN1)
        r = self._post(c, f'/admin/users/update/{self._uid(ADMIN1)}', {'is_admin': '0'})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.get_json()['ok'])
        with portal.app.app_context():
            still = portal.get_db().execute(
                "SELECT is_admin FROM local_users WHERE username=?", (ADMIN1,)).fetchone()
        self.assertEqual(still['is_admin'], 1, "l'admin s'est rétrogradé lui-même")

    def test_auto_desactivation_refusee(self):
        c = self._client(ADMIN1)
        r = self._post(c, f'/admin/users/update/{self._uid(ADMIN1)}', {'enabled': '0'})
        self.assertEqual(r.status_code, 409)

    def test_dernier_admin_local_protege(self):
        """Seen by a DIRECTORY admin (no local row), disabling the last local
        admin would leave the portal without local administration."""
        c = self._client(ANNUAIRE)
        r = self._post(c, f'/admin/users/update/{self._uid(ADMIN2)}', {'enabled': '0'})
        self.assertEqual(r.status_code, 200)          # ADMIN1 reste admin local
        r = self._post(c, f'/admin/users/update/{self._uid(ADMIN1)}', {'enabled': '0'})
        self.assertEqual(r.status_code, 409)
        self.assertIn('dernier administrateur local', r.get_json()['error'])

    def test_compte_simple_retrogradable_sans_garde(self):
        """The guard must not get in the way of the normal case."""
        c = self._client(ADMIN1)
        r = self._post(c, f'/admin/users/update/{self._uid(SIMPLE)}', {'enabled': '0'})
        self.assertEqual(r.status_code, 200)

    def test_groupe_admin_a_deux_membres_ne_peut_pas_disparaitre(self):
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
        c = self._client(ANNUAIRE)
        r = self._post(c, '/admin/groups/delete/ztest-groupe')
        self.assertEqual(r.status_code, 409)
        self.assertIn('administrateur local', r.get_json()['error'])
        with portal.app.app_context():
            encore = portal.get_db().execute(
                "SELECT COUNT(*) AS n FROM user_groups WHERE name='ztest-groupe'").fetchone()['n']
        self.assertEqual(encore, 1)

    def test_retirer_le_droit_admin_du_groupe_est_garde(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) "
                       "VALUES ('ztest-groupe', NULL, 1, ?)", (time.time(),))
            db.execute("UPDATE local_users SET is_admin=0, group_name='ztest-groupe' "
                       "WHERE username IN (?,?)", (ADMIN1, ADMIN2))
            db.commit()
        c = self._client(ANNUAIRE)
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

    def test_subvention_conservee_si_le_retour_echoue(self):
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
                ramenes = admin_routes.revert_expired_grants()
        self.assertEqual(ramenes, 0)
        with portal.app.app_context():
            reste = portal.get_db().execute(
                "SELECT COUNT(*) AS n FROM budget_grants WHERE username=?", (SIMPLE,)).fetchone()['n']
        self.assertEqual(reste, 1, "la ligne portait l'échéance : la supprimer rendait "
                                   "la hausse permanente")
        self.assertTrue(self._audit('budget.grant_revert_echec'))

    def test_resynchronisation_transmet_la_duree(self):
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT * FROM local_users WHERE username=?", (SIMPLE,)).fetchone()
        with patch.object(local_users, '_ensure_litellm_user'), \
             patch.object(local_users, 'litellm_update_user_budget', return_value=True) as maj:
            with portal.app.app_context():
                local_users._sync_local_user_budget(SIMPLE, row)
        self.assertTrue(maj.call_args.kwargs.get('budget_duration'),
                        "un update sans durée laisse un compte sans fenêtre de remise à zéro")


class CycleDeVieModeleTest(BaseAdmin):
    """Removing a model must not destroy the means to relaunch it."""

    def _modele(self, name='ztest-modele'):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                       "VALUES (?, 'org/modele', '--ctx-size 4096', 'vllm', ?)",
                       (name, time.time()))
            db.commit()
            return db.execute("SELECT id FROM model_configs WHERE name=?", (name,)).fetchone()['id']

    def test_suppression_modele_servi_refusee(self):
        """Its weights are read by the engine: we ask to stop it first, and
        nothing is touched — neither the catalog, nor the files."""
        mid = self._modele()
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=['ztest-modele']), \
             patch.object(admin_routes, 'runner_delete_files') as effacer:
            r = self._post(c, f'/admin/model/delete/{mid}', {'confirm': '1'})
        self.assertEqual(r.status_code, 409)
        effacer.assert_not_called()
        with portal.app.app_context():
            self.assertIsNotNone(portal.get_db().execute(
                "SELECT 1 FROM model_configs WHERE id=?", (mid,)).fetchone())

    def test_suppression_efface_les_fichiers(self):
        mid = self._modele('ztest-modele3')
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=[]), \
             patch.object(admin_routes, '_unregister_litellm_model', return_value=True), \
             patch.object(admin_routes, 'runner_delete_files',
                          return_value=(True, 3 * 2**30, '')) as effacer:
            r = self._post(c, f'/admin/model/delete/{mid}')
        self.assertEqual(r.status_code, 200)
        effacer.assert_called_once_with('org/modele')
        self.assertIn('3.0 Gio', r.get_json()['message'])
        self.assertNotIn('warning', r.get_json())

    def test_fichiers_partages_conserves(self):
        """Another entry points to the same weights: we do not erase them."""
        mid = self._modele('ztest-modele4')
        self._modele('ztest-modele5')
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=[]), \
             patch.object(admin_routes, '_unregister_litellm_model', return_value=True), \
             patch.object(admin_routes, 'runner_delete_files') as effacer:
            r = self._post(c, f'/admin/model/delete/{mid}')
        self.assertEqual(r.status_code, 200)
        effacer.assert_not_called()
        self.assertIn('ztest-modele5', r.get_json()['warning'])

    def test_echec_d_effacement_signale(self):
        mid = self._modele('ztest-modele6')
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=[]), \
             patch.object(admin_routes, '_unregister_litellm_model', return_value=True), \
             patch.object(admin_routes, 'runner_delete_files',
                          return_value=(False, 0, 'runner injoignable')):
            r = self._post(c, f'/admin/model/delete/{mid}')
        self.assertEqual(r.status_code, 200)
        self.assertIn('runner injoignable', r.get_json()['warning'])

    def test_suppression_signale_l_echec_de_deregistration(self):
        mid = self._modele('ztest-modele2')
        c = self._client(ADMIN1)
        with patch.object(admin_routes, 'get_running_models', return_value=[]), \
             patch.object(admin_routes, '_unregister_litellm_model', return_value=False), \
             patch.object(admin_routes, 'runner_delete_files', return_value=(True, 0, '')):
            r = self._post(c, f'/admin/model/delete/{mid}')
        self.assertEqual(r.status_code, 200)
        self.assertIn('warning', r.get_json())

    def test_relance_voix_soumise_a_la_garde_memoire(self):
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

    def test_demande_non_validee_si_le_routage_echoue(self):
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
        self.assertEqual(r.status_code, 502)
        mail.assert_not_called()
        with portal.app.app_context():
            statut = portal.get_db().execute(
                "SELECT status FROM model_requests WHERE id=?", (rid,)).fetchone()['status']
            portal.get_db().execute("DELETE FROM model_requests WHERE id=?", (rid,))
            portal.get_db().execute("DELETE FROM model_configs WHERE hf_model_id='org/nouveau'")
            portal.get_db().commit()
        self.assertEqual(statut, 'pending', "la demande ne doit pas être marquée traitée")

    def test_restauration_litellm_apres_echec_de_creation(self):
        """`_litellm_upsert` deletes the entry before recreating it: if the
        creation fails, a model that was answering must be put back in place."""
        entree = {'model_name': 'ztest-modele', 'litellm_params': {'model': 'openai/ztest-modele'},
                  'model_info': {'id': 'abc', 'db_model': True, 'max_input_tokens': 4096}}
        appels = []

        class Reponse:
            def __init__(self, code):
                self.status_code = code
                self.ok = code < 300

        def faux_post(url, **kwargs):
            appels.append(url)
            if url.endswith('/model/delete'):
                return Reponse(200)
            if len([u for u in appels if u.endswith('/model/new')]) == 1:
                return Reponse(500)                  # the creation fails
            return Reponse(200)                      # the restoration succeeds

        with patch.object(litellm_client, 'LITELLM_KEY', 'clef'), \
             patch.object(litellm_client, '_litellm_model_entry', return_value=('abc', entree)), \
             patch.object(litellm_client.requests, 'post', side_effect=faux_post):
            ok = litellm_client._litellm_upsert('ztest-modele', 'ztest-modele', 4096, 1024)
        self.assertFalse(ok)
        self.assertEqual(len([u for u in appels if u.endswith('/model/new')]), 2,
                         "l'entrée précédente n'a pas été restaurée")


class EtatPlateformeTest(BaseAdmin):
    """The diagnosis must no longer require a shell on the host."""

    def test_etat_plateforme_expose_les_cles_attendues(self):
        c = self._client(ADMIN1)
        r = c.get('/admin/platform')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        for cle in ('disk', 'backup', 'monitor', 'model', 'portal', 'maintenance', 'checked_at'):
            self.assertIn(cle, d)
        self.assertIsInstance(d['disk']['free_gb'], float)
        # The mounted paths do not exist in the test container: the state must
        # say « non lisible », never pretend all is well.
        self.assertFalse(d['backup']['readable'])
        self.assertFalse(d['monitor']['readable'])

    def test_etat_plateforme_refuse_un_non_admin(self):
        c = self._client(SIMPLE, is_admin=False)
        # 302 to /login for a browser request, 403 for an API call: in both
        # cases the platform state is not served.
        self.assertIn(c.get('/admin/platform').status_code, (302, 403))

    def test_contrat_json_sur_une_route_convertie(self):
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
