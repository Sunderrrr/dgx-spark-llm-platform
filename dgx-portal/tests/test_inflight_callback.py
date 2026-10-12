"""Tests of the LiteLLM in-flight activity callback (`dgx-portal/litellm_inflight.py`).

This module runs INSIDE the proxy (mounted as `dgx_inflight`), but its
logic is critical: it is the only source naming a user while they generate.
Two properties to freeze, the first one non-negotiable:

  - a write error must NEVER surface: a request cannot fail because the
    activity tracking could not write;
  - an opened row is removed at the end, on success as on failure, else the
    panel would display ghost sessions indefinitely.
"""
import importlib.util
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

# The module lives in dgx-portal/ (not in litellm/) because the test
# container only mounts that folder: this is what keeps it covered by CI.
# LiteLLM, for its part, gets it mounted as `dgx_inflight` — see docker-compose.yml.
import litellm_inflight


def _charge(chemin_db):
    """Reloads the module with a throwaway tracking file."""
    os.environ['DGX_INFLIGHT_DB'] = chemin_db
    importlib.reload(litellm_inflight)
    return litellm_inflight


def _kwargs(key='c1', alias='alice-1783330573', user_id='alice', model_name='auto-model'):
    return {'litellm_call_id': key, 'model': model_name,
            'litellm_params': {'metadata': {'litellm_call_id': key,
                                            'user_api_key_alias': alias,
                                            'user_api_key_user_id': user_id}}}


class ActiviteEnVolTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.path = os.path.join(self.folder, 'inflight.db')
        self.mod = _charge(self.path)

    def _lignes(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute('SELECT request_key, alias, user_id, model FROM inflight').fetchall()
        finally:
            conn.close()

    def test_une_requete_ouverte_puis_fermee(self):
        self.assertEqual(self.mod._record(_kwargs()), 1)
        self.assertEqual(self._lignes(), [('c1', 'alice-1783330573', 'alice', 'auto-model')])
        self.assertEqual(self.mod._remove(_kwargs()), 1)
        self.assertEqual(self._lignes(), [])

    def test_deux_requetes_concurrentes_sont_independantes(self):
        """4 sessions on this engine: removing one must not touch the other."""
        self.mod._record(_kwargs('c1', 'alice-1', 'alice'))
        self.mod._record(_kwargs('c2', 'bob-1', 'bob'))
        self.mod._remove(_kwargs('c1', 'alice-1', 'alice'))
        self.assertEqual([l[0] for l in self._lignes()], ['c2'])

    def test_une_ligne_perimee_est_balayee(self):
        """A client killed mid-flight triggers neither success nor failure."""
        self.mod._record(_kwargs('vieux'))
        conn = sqlite3.connect(self.path)
        conn.execute('UPDATE inflight SET started_at=?', (time.time() - 99999,))
        conn.commit()
        conn.close()
        self.mod._record(_kwargs('recent'))
        self.assertEqual([l[0] for l in self._lignes()], ['recent'])

    def test_sans_identifiant_aucune_ecriture(self):
        self.assertEqual(self.mod._record({'litellm_params': {}}), 0)
        self.assertEqual(self.mod._remove({'litellm_params': {}}), 0)

    def test_une_base_illisible_ne_fait_pas_echouer_la_requete(self):
        """First rule: activity tracking is never fatal."""
        with mock.patch.object(self.mod, '_open', side_effect=RuntimeError('disque plein')):
            with self.assertRaises(RuntimeError):
                self.mod._record(_kwargs())
            # The hooks, for their part, swallow the error: this is what protects the request.
            hooks = self.mod.InflightActivity()
            import asyncio
            asyncio.run(hooks.async_log_pre_api_call('m', [], _kwargs()))
            asyncio.run(hooks.async_log_success_event(_kwargs(), None, None, None))
            asyncio.run(hooks.async_log_failure_event(_kwargs(), None, None, None))

    def test_les_hooks_ecrivent_et_retirent(self):
        import asyncio
        hooks = self.mod.InflightActivity()
        asyncio.run(hooks.async_log_pre_api_call('auto-model', [], _kwargs()))
        self.assertEqual(len(self._lignes()), 1)
        asyncio.run(hooks.async_log_success_event(_kwargs(), None, None, None))
        self.assertEqual(self._lignes(), [])

    def test_les_hooks_SYNCHRONES_ecrivent_et_retirent(self):
        """These are the ones this LiteLLM version really calls.

        Regression of 2026-09-14: the class only implemented the async
        variants. Yet `async_log_pre_api_call` is declared by `CustomLogger`
        but never invoked by the proxy (checked in the installed package):
        the pre-call goes through `litellm.input_callback` and the success
        through `litellm.callbacks`, which call the SYNCHRONOUS methods.
        Observed in production: table created, table empty, no error —
        perfect silence. This test is the missing guard.
        """
        hooks = self.mod.InflightActivity()
        hooks.log_pre_api_call(model='auto-model', messages=[], kwargs=_kwargs())
        self.assertEqual(len(self._lignes()), 1)

        # The failure removes too: a refused request must not leave a name
        # displayed as « en cours » until expiry.
        hooks.log_failure_event(kwargs=_kwargs())
        self.assertEqual(self._lignes(), [])

        hooks.log_pre_api_call(model='auto-model', messages=[], kwargs=_kwargs())
        hooks.log_success_event(kwargs=_kwargs(), response_obj=None,
                                start_time=None, end_time=None)
        self.assertEqual(self._lignes(), [])

    def test_les_hooks_synchrones_ne_sont_pas_fatals(self):
        """Same guarantee as for the async one: never a raised exception."""
        hooks = self.mod.InflightActivity()
        with mock.patch.object(self.mod, '_open', side_effect=RuntimeError('disque plein')):
            hooks.log_pre_api_call(model='m', messages=[], kwargs=_kwargs())
            hooks.log_success_event(kwargs=_kwargs())
            hooks.log_failure_event(kwargs=_kwargs())


if __name__ == '__main__':
    unittest.main()
