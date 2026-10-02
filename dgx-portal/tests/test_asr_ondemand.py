"""On-demand ASR: auto-start at the first dictation, idle reaper, memory guard.

The `asr` sidecar used to sit on ~2.1 GiB of GPU forever (`restart=unless-stopped`,
stopped only by an admin click). It is now a true on-demand service, and these
tests lock in the four behaviours the improvement is about:

  1. sidecar stopped + enough memory  → ONE auto-start, a retry-later payload,
     and NO transcription attempted (the model is not loaded yet);
  2. sidecar stopped + low memory     → NO start at all, an honest error (the
     chat model needs that memory — on unified memory an OOM kills it);
  3. sidecar ready                    → transcription proceeds and the idle
     timer is refreshed;
  4. idle reaper                      → stops the container only after the idle
     window, never while a call is in flight, never twice, and not at all while
     a manual admin start has it pinned.

No real docker call anywhere: the runner helpers (`_sidecar_action`,
`_sidecar_proc_status`) are mocked at the seam the routes use.
"""
import io
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import app as portal
import asr_routes
import sidecars
from db import set_setting


class _Base(unittest.TestCase):
    CSRF = "test-csrf"

    def setUp(self):
        # Every test gets its OWN on-demand state directory: the idle clock,
        # the in-flight markers and the pin are file state (shared by the four
        # gunicorn workers in production), so a temp dir isolates them without
        # mocking the mechanism under test.
        self._etat = tempfile.mkdtemp(prefix='asr-ondemand-')
        self._ancien_dir = os.environ.get('ASR_ONDEMAND_DIR')
        os.environ['ASR_ONDEMAND_DIR'] = self._etat
        sidecars._asr_veilleur_demarre = False   # the real thread never runs in tests
        sidecars._asr_demarrage_auto_t = 0.0     # …nor does the start throttle carry over
        self._vider_cache()
        with self._db():
            portal.get_db().execute(
                "DELETE FROM login_attempts WHERE key LIKE 'rl-asr|%'")
            portal.get_db().commit()
            set_setting('maintenance_mode', '0')

    def tearDown(self):
        with self._db():
            portal.get_db().execute(
                "DELETE FROM login_attempts WHERE key LIKE 'rl-asr|%'")
            portal.get_db().commit()
        self._vider_cache()
        if self._ancien_dir is None:
            os.environ.pop('ASR_ONDEMAND_DIR', None)
        else:
            os.environ['ASR_ONDEMAND_DIR'] = self._ancien_dir
        shutil.rmtree(self._etat, ignore_errors=True)

    def _vider_cache(self):
        # Defence for the OTHER modules' tests (e.g. test_playground_chat's
        # /api/transcribe/available): a cached « running » left behind here
        # would change what they observe. Nothing real is ever probed here.
        sidecars._sidecar_proc_cache.clear()
        sidecars._asr_info_cache.update(t=0.0, v={})

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

    def _transcrit(self):
        return self._client().post(
            '/api/transcribe',
            data={'audio': (io.BytesIO(b'RIFFxxxxWAVE'), 'rec.wav')},
            headers={'X-CSRFToken': self.CSRF},
            content_type='multipart/form-data')

    def _recule_activite(self, secondes):
        """Backdate the idle clock (as if no transcription had happened since)."""
        p = os.path.join(self._etat, 'activite')
        ancien = time.time() - secondes
        os.utime(p, (ancien, ancien))


class DemarrageTest(_Base):
    """Auto-start on the transcription path — and its two refusals."""

    def test_sidecar_arrete_demarre_une_fois_sans_transcrire(self):
        """(1) Stopped + enough memory: ONE auto-start, a retry-later payload the
        1 s polling tolerates, and no transcription attempted (nothing could
        answer it: whisper is not loaded yet)."""
        with patch.object(sidecars, '_mem_available_gb', return_value=12.0), \
             patch.object(asr_routes, 'asr_is_up', return_value=False), \
             patch.object(asr_routes, '_sidecar_proc_status', return_value='stopped'), \
             patch.object(sidecars, '_sidecar_action', return_value=(True, '')) as demarre, \
             patch.object(asr_routes, 'asr_demarrer_veilleur') as veilleur, \
             patch.object(asr_routes.requests, 'post') as appel:
            r = self._transcrit()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        corps = r.get_json()
        # The shape useDictation silently tolerates: a 2xx (no error shown) and
        # `text` a STRING (any other type is treated as a failed round).
        self.assertEqual(corps['text'], '')
        self.assertTrue(corps['demarrage'])
        demarre.assert_called_once_with('asr', 'start')
        appel.assert_not_called()          # NO transcription attempted
        veilleur.assert_called_once()      # the idle reaper is armed

    def test_memoire_basse_ne_demarre_pas_et_le_dit(self):
        """(2) Stopped + low memory: NO start (the model needs the memory) and an
        honest error naming the reason and the remedy."""
        with patch.object(sidecars, '_mem_available_gb', return_value=3.2), \
             patch.object(asr_routes, 'asr_is_up', return_value=False), \
             patch.object(asr_routes, '_sidecar_proc_status', return_value='stopped'), \
             patch.object(asr_routes, 'asr_demarrer_veilleur'), \
             patch.object(sidecars, '_sidecar_action') as demarre, \
             patch.object(asr_routes.requests, 'post') as appel:
            r = self._transcrit()
        self.assertEqual(r.status_code, 503, r.get_data(as_text=True))
        err = r.get_json()['error'].lower()
        self.assertIn('mémoire', err)
        self.assertIn('chat', err)          # who needs that memory is said
        demarre.assert_not_called()
        appel.assert_not_called()

    def test_sidecar_pret_transcrit_et_ravitaille_le_minuteur(self):
        """(3) Ready: the transcription goes through, no start is attempted, and
        the idle clock is refreshed (a running dictation must never age into an
        automatic stop)."""
        sidecars.asr_note_activite()
        self._recule_activite(sidecars._ASR_INACTIVITE_S + 60)

        class _R:
            ok = True
            status_code = 200

            def json(self):
                return {'text': 'bonjour le monde'}

        with patch.object(asr_routes, 'asr_is_up', return_value=True), \
             patch.object(asr_routes, 'asr_demarrer_veilleur'), \
             patch.object(sidecars, '_sidecar_action') as demarre, \
             patch.object(asr_routes.requests, 'post', return_value=_R()) as appel:
            r = self._transcrit()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['text'], 'bonjour le monde')
        appel.assert_called_once()
        demarre.assert_not_called()
        self.assertLess(sidecars.asr_inactivite_s(), 5)    # clock refreshed
        self.assertEqual(sidecars.asr_appels_en_vol(), 0)  # marker cleaned up

    def test_chargement_echoue_dit_pourquoi(self):
        """A container running with no model loaded and a PUBLISHED cause is an
        honest error, not an endless silent « démarrage » (cf. asr_load_error)."""
        with patch.object(asr_routes, 'asr_is_up', return_value=False), \
             patch.object(asr_routes, '_sidecar_proc_status', return_value='running'), \
             patch.object(asr_routes, 'asr_load_error',
                          return_value='CUDA error: out of memory'), \
             patch.object(asr_routes, 'asr_demarrer_veilleur'), \
             patch.object(sidecars, '_sidecar_action') as demarre, \
             patch.object(asr_routes.requests, 'post') as appel:
            r = self._transcrit()
        self.assertEqual(r.status_code, 503)
        self.assertIn('CUDA error', r.get_json()['error'])
        demarre.assert_not_called()
        appel.assert_not_called()

    def test_le_demarrage_n_est_pas_repete_a_chaque_poll(self):
        """The client polls every second: a successful auto-start must not fire
        one `docker start` (and one audit line) per poll while the model loads.
        A FAILED start, on the other hand, is retried — the throttle only
        follows a success."""
        commun = (patch.object(sidecars, '_mem_available_gb', return_value=12.0),
                  patch.object(asr_routes, 'asr_is_up', return_value=False),
                  patch.object(asr_routes, '_sidecar_proc_status', return_value='stopped'),
                  patch.object(asr_routes, 'asr_demarrer_veilleur'),
                  patch.object(asr_routes.requests, 'post'))
        for p in commun:
            p.start()
        try:
            with patch.object(sidecars, '_sidecar_action',
                              return_value=(True, '')) as demarre:
                self._transcrit()
                self._transcrit()          # the next poll, 1 s later
            self.assertEqual(demarre.call_count, 1)
            # Window over (simulated), and a FAILED start never arms the
            # throttle: every poll retries until the service comes up.
            sidecars._asr_demarrage_auto_t = 0.0
            with patch.object(sidecars, '_sidecar_action',
                              return_value=(False, ' (runner injoignable)')) as echec:
                r1 = self._transcrit()
                r2 = self._transcrit()
            self.assertEqual(echec.call_count, 2)
            self.assertEqual(r1.status_code, 502)
            self.assertEqual(r2.status_code, 502)
        finally:
            for p in commun:
                p.stop()

    def test_le_garde_memoire_exige_un_vrai_plancher(self):
        """The ~8 GiB threshold is the guard between « start » and « OOM the chat
        model ». Unreadable memory → we do NOT start."""
        with patch.object(sidecars, '_mem_available_gb', return_value=7.9):
            ok, motif = sidecars.asr_memoire_ok()
        self.assertFalse(ok)
        self.assertIn('mémoire', motif.lower())
        with patch.object(sidecars, '_mem_available_gb', return_value=8.5):
            self.assertTrue(sidecars.asr_memoire_ok()[0])
        with patch.object(sidecars, '_mem_available_gb', return_value=None):
            self.assertFalse(sidecars.asr_memoire_ok()[0])

    def test_le_bouton_dictee_rest_disponible_conteneur_arrete(self):
        """The auto-start must be REACHABLE: useDictation renders no dictation
        button at all when `available` is false. A stopped-but-present container
        is therefore « available »; an unreachable runner is not."""
        with patch.object(asr_routes, 'asr_is_up', return_value=False), \
             patch.object(asr_routes, '_sidecar_proc_status', return_value='stopped'):
            r = self._client().get('/api/transcribe/available')
        self.assertTrue(r.get_json()['available'])
        with patch.object(asr_routes, 'asr_is_up', return_value=False), \
             patch.object(asr_routes, '_sidecar_proc_status', return_value='unreachable'):
            r = self._client().get('/api/transcribe/available')
        self.assertFalse(r.get_json()['available'])


class VeilleurTest(_Base):
    """The idle reaper: stops after the window, never mid-call, idempotent."""

    def test_le_veilleur_arrete_apres_la_fenetre_d_inactivite(self):
        sidecars.asr_note_activite()
        with patch.object(sidecars, '_sidecar_action') as action:
            self.assertFalse(sidecars._asr_veilleur_passe())
        action.assert_not_called()                     # window not reached
        self._recule_activite(sidecars._ASR_INACTIVITE_S + 60)
        with patch.object(sidecars, '_sidecar_proc_status', return_value='running'), \
             patch.object(sidecars, '_sidecar_action', return_value=(True, '')) as action:
            self.assertTrue(sidecars._asr_veilleur_passe())
            self.assertEqual(action.call_args[0][:2], ('asr', 'stop'))
            # Idempotent: the clock is rearmed by the stop itself, a second pass
            # (this worker's or another's) has nothing left to do.
            self.assertFalse(sidecars._asr_veilleur_passe())
        self.assertEqual(action.call_count, 1)

    def test_le_veilleur_ne_touche_jamais_a_un_appel_en_cours(self):
        """(4) Never stop while a transcription runs — the in-flight marker says
        so, and the clock restarts from the END of the call."""
        sidecars.asr_note_activite()
        self._recule_activite(sidecars._ASR_INACTIVITE_S + 60)
        with sidecars.asr_appel_en_vol():
            self.assertFalse(sidecars.asr_arret_autorise())
            with patch.object(sidecars, '_sidecar_proc_status', return_value='running'), \
                 patch.object(sidecars, '_sidecar_action') as action:
                self.assertFalse(sidecars._asr_veilleur_passe())
            action.assert_not_called()
        self.assertFalse(sidecars.asr_arret_autorise())  # fresh: the call just ended

    def test_un_demarrage_manuel_epingle_le_conteneur(self):
        """An admin start suppresses the reaper for a while (asr_epingler, 1 h);
        a manual stop clears the pin."""
        sidecars.asr_epingler()
        self._recule_activite(sidecars._ASR_INACTIVITE_S + 60)
        self.assertFalse(sidecars.asr_arret_autorise())
        sidecars.asr_depingler()
        self.assertTrue(sidecars.asr_arret_autorise())
        # An EXPIRED pin protects nothing (the window is a delay, not a lock).
        # asr_epingler resets the idle clock too, hence the new backdating.
        sidecars.asr_epingler(duree_s=-1)
        self._recule_activite(sidecars._ASR_INACTIVITE_S + 60)
        self.assertTrue(sidecars.asr_arret_autorise())

    def test_le_veilleur_ne_parle_pas_a_docker_si_deja_arrete(self):
        """`docker stop` on a stopped container is a no-op, so we simply do not
        call it — and we do not audit a stop that did not happen."""
        sidecars.asr_note_activite()
        self._recule_activite(sidecars._ASR_INACTIVITE_S + 60)
        with patch.object(sidecars, '_sidecar_proc_status', return_value='stopped'), \
             patch.object(sidecars, '_sidecar_action') as action:
            self.assertFalse(sidecars._asr_veilleur_passe())
        action.assert_not_called()

    def test_le_veilleur_est_lance_une_seule_fois(self):
        """Lazy and single: started at the first dictation, never twice in the
        same process (each gunicorn worker gets its own, everything else being
        idempotent)."""
        with patch.object(sidecars.threading, 'Thread') as thread:
            self.assertTrue(sidecars.asr_demarrer_veilleur())
            self.assertFalse(sidecars.asr_demarrer_veilleur())
        self.assertEqual(thread.call_count, 1)


if __name__ == '__main__':
    unittest.main()
