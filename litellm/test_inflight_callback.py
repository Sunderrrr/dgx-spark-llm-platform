"""Tests of the LiteLLM in-flight activity callback (`litellm/litellm_inflight.py`).

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

# The module lives in litellm/, next to the proxy config: it is mounted into
# the proxy as `dgx_inflight` — see docker-compose.yml.
import litellm_inflight


def _charge(db_path):
    """Reloads the module with a throwaway tracking file."""
    os.environ['DGX_INFLIGHT_DB'] = db_path
    importlib.reload(litellm_inflight)
    return litellm_inflight


def _kwargs(key='c1', alias='alice-1783330573', user_id='alice', model_name='auto-model'):
    return {'litellm_call_id': key, 'model': model_name,
            'litellm_params': {'metadata': {'litellm_call_id': key,
                                            'user_api_key_alias': alias,
                                            'user_api_key_user_id': user_id}}}


class InflightActivityTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.path = os.path.join(self.folder, 'inflight.db')
        self.mod = _charge(self.path)

    def _rows(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute('SELECT request_key, alias, user_id, model FROM inflight').fetchall()
        finally:
            conn.close()

    def test_request_opened_then_closed(self):
        self.assertEqual(self.mod._record(_kwargs()), 1)
        self.assertEqual(self._rows(), [('c1', 'alice-1783330573', 'alice', 'auto-model')])
        self.assertEqual(self.mod._remove(_kwargs()), 1)
        self.assertEqual(self._rows(), [])

    def test_two_concurrent_requests_independent(self):
        """4 sessions on this engine: removing one must not touch the other."""
        self.mod._record(_kwargs('c1', 'alice-1', 'alice'))
        self.mod._record(_kwargs('c2', 'bob-1', 'bob'))
        self.mod._remove(_kwargs('c1', 'alice-1', 'alice'))
        self.assertEqual([l[0] for l in self._rows()], ['c2'])

    def test_stale_row_swept(self):
        """A client killed mid-flight triggers neither success nor failure."""
        self.mod._record(_kwargs('vieux'))
        conn = sqlite3.connect(self.path)
        conn.execute('UPDATE inflight SET started_at=?', (time.time() - 99999,))
        conn.commit()
        conn.close()
        self.mod._record(_kwargs('recent'))
        self.assertEqual([l[0] for l in self._rows()], ['recent'])

    def test_no_identifier_no_write(self):
        self.assertEqual(self.mod._record({'litellm_params': {}}), 0)
        self.assertEqual(self.mod._remove({'litellm_params': {}}), 0)

    def test_unreadable_db_does_not_fail_request(self):
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

    def test_hooks_write_and_remove(self):
        import asyncio
        hooks = self.mod.InflightActivity()
        asyncio.run(hooks.async_log_pre_api_call('auto-model', [], _kwargs()))
        self.assertEqual(len(self._rows()), 1)
        asyncio.run(hooks.async_log_success_event(_kwargs(), None, None, None))
        self.assertEqual(self._rows(), [])

    def test_SYNC_hooks_write_and_remove(self):
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
        self.assertEqual(len(self._rows()), 1)

        # The failure removes too: a refused request must not leave a name
        # displayed as « en cours » until expiry.
        hooks.log_failure_event(kwargs=_kwargs())
        self.assertEqual(self._rows(), [])

        hooks.log_pre_api_call(model='auto-model', messages=[], kwargs=_kwargs())
        hooks.log_success_event(kwargs=_kwargs(), response_obj=None,
                                start_time=None, end_time=None)
        self.assertEqual(self._rows(), [])

    def test_sync_hooks_not_fatal(self):
        """Same guarantee as for the async one: never a raised exception."""
        hooks = self.mod.InflightActivity()
        with mock.patch.object(self.mod, '_open', side_effect=RuntimeError('disque plein')):
            hooks.log_pre_api_call(model='m', messages=[], kwargs=_kwargs())
            hooks.log_success_event(kwargs=_kwargs())
            hooks.log_failure_event(kwargs=_kwargs())


class SystemNormalisationTest(unittest.TestCase):
    """`system` must be ONE message, first — llama.cpp's template refuses the rest."""

    def setUp(self):
        import importlib.util, pathlib
        here = pathlib.Path(__file__).with_name("litellm_inflight.py")
        spec = importlib.util.spec_from_file_location("inflight_under_test", here)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_a_late_system_message_is_moved_first(self):
        msgs = [{"role": "user", "content": "salut"}, {"role": "system", "content": "réponds en français"}]
        out = self.mod._normalise_system(msgs)
        self.assertEqual([m["role"] for m in out], ["system", "user"])
        self.assertIn("français", out[0]["content"])

    def test_two_system_messages_are_merged(self):
        msgs = [{"role": "system", "content": "A"}, {"role": "user", "content": "q"}, {"role": "system", "content": "B"}]
        out = self.mod._normalise_system(msgs)
        self.assertEqual(len([m for m in out if m["role"] == "system"]), 1)
        self.assertIn("A", out[0]["content"])
        self.assertIn("B", out[0]["content"])

    def test_a_correct_request_is_left_alone(self):
        msgs = [{"role": "system", "content": "A"}, {"role": "user", "content": "q"}]
        self.assertIs(self.mod._normalise_system(msgs), msgs)


if __name__ == '__main__':
    unittest.main()
