"""Playground voice mode — the spoken half (voice_chat_routes.py).

Locks in what the browser relies on: the status says whether the voice is
Kyutai and ready, the start refuses honestly when another voice engine is
loaded, and /api/voice/speak relays the sidecar's PCM stream as it comes —
with its sample rate — and closes the upstream request when done.

No network: the sidecar is reached through a `requests` stand-in scoped to
the module (never the shared package, see test_regression_guards).
"""
import time
import types
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import app as portal  # noqa: F401  (registers the routes)
import voice_chat_routes as vc

KYUTAI = {'engine': 'kyutai', 'loaded': True, 'voices': ['fr-a', 'fr-b'],
          'default_voice': 'fr-a', 'sample_rate': 24000}


class _Resp:
    def __init__(self, status=200, body=None, chunks=(), headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.closed = False

    def json(self):
        return self._body

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        self.closed = True


def _requests(get=None, post=None):
    import requests
    ns = types.SimpleNamespace(**{k: getattr(requests, k) for k in dir(requests)
                                  if not k.startswith('_')})
    if get:
        ns.get = get
    if post:
        ns.post = post
    return ns


class VoiceChatTest(unittest.TestCase):
    CSRF = 'test-csrf'

    def setUp(self):
        vc._info_cache.update(t=0.0, v={})
        self.c = portal.app.test_client()
        with self.c.session_transaction() as s:
            s['username'] = 'demo'
            s['auth_at'] = int(time.time())
            s['csrf'] = self.CSRF

    def _stack(self, info, proc='running', post=None, autostart=(True, ''), asr_up=True):
        st = ExitStack()
        self.asr_starts = []
        st.enter_context(patch.object(vc, 'asr_is_up', return_value=asr_up))
        st.enter_context(patch.object(vc, 'asr_note_activity'))
        st.enter_context(patch.object(vc, 'asr_start_watcher'))
        st.enter_context(patch.object(vc, 'asr_memory_ok', return_value=(True, '')))
        st.enter_context(patch.object(vc, 'asr_autostart',
                                      side_effect=lambda: self.asr_starts.append(1) or (True, '')))
        st.enter_context(patch.object(vc, '_sidecar_proc_status', return_value=proc))
        st.enter_context(patch.object(vc, 'maintenance_block_json', return_value=None))
        st.enter_context(patch.object(vc, 'sidecar_autostart', return_value=autostart))
        st.enter_context(patch.object(vc, 'requests', _requests(
            get=lambda *a, **k: _Resp(body=info), post=post)))
        return st

    def test_status_reports_a_ready_kyutai_voice(self):
        with self._stack(KYUTAI):
            body = self.c.get('/api/voice/chat/status').get_json()
        self.assertTrue(body['ready'])
        self.assertFalse(body['wrong_engine'])
        self.assertEqual(body['voices'], ['fr-a', 'fr-b'])

    def test_start_says_when_another_engine_is_loaded(self):
        # The container runs Chatterbox: speaking its protocol to Kyutai's
        # routes would 404 — the voice mode must say so, not try.
        with self._stack({'engine': 'chatterbox', 'loaded': True}, autostart=(False, 'x')):
            r = self.c.post('/api/voice/chat/start', headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['code'], 'voice_engine')

    def test_start_warms_the_transcription_up(self):
        # Whisper loads in ~10 s: started with the voice mode, not after the
        # user's first sentence (2026-10-13).
        with self._stack(KYUTAI, proc='stopped', asr_up=False):
            body = self.c.post('/api/voice/chat/start', headers={'X-CSRFToken': self.CSRF}).get_json()
        self.assertEqual(body['asr'], 'starting')
        self.assertEqual(self.asr_starts, [1])
        with self._stack(KYUTAI):
            body = self.c.post('/api/voice/chat/start', headers={'X-CSRFToken': self.CSRF}).get_json()
        self.assertEqual(body['asr'], 'ready')
        self.assertEqual(self.asr_starts, [])

    def test_speak_relays_the_pcm_stream_and_closes_upstream(self):
        sent, up = {}, _Resp(chunks=[b'\x01\x00' * 4, b'', b'\x02\x00' * 4],
                             headers={'X-Sample-Rate': '24000'})

        def _post(url, json=None, stream=False, timeout=None):
            sent.update(url=url, json=json, stream=stream)
            return up

        with self._stack(KYUTAI, post=_post):
            r = self.c.post('/api/voice/speak', json={'text': '  Salut   Alex ! ', 'voice': 'fr-b'},
                            headers={'X-CSRFToken': self.CSRF})
            data = r.get_data()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['X-Sample-Rate'], '24000')
        self.assertEqual(data, b'\x01\x00' * 4 + b'\x02\x00' * 4)
        self.assertTrue(sent['url'].endswith('/api/speak'))
        self.assertEqual(sent['json'], {'text': 'Salut Alex !', 'voice': 'fr-b'})
        self.assertTrue(sent['stream'])
        self.assertTrue(up.closed, "the upstream stream must be released")

    def test_speak_refuses_empty_or_long_text_and_a_wrong_engine(self):
        with self._stack(KYUTAI, post=lambda *a, **k: self.fail("no call expected")):
            r = self.c.post('/api/voice/speak', json={'text': '   '}, headers={'X-CSRFToken': self.CSRF})
            self.assertEqual(r.status_code, 400)
            r = self.c.post('/api/voice/speak', json={'text': 'a' * (vc.SPEAK_MAX_CHARS + 1)},
                            headers={'X-CSRFToken': self.CSRF})
            self.assertEqual(r.status_code, 413)
        # Another engine in the container has no /api/speak: said as such.
        with self._stack({'engine': 'qwen3-tts', 'loaded': True},
                         post=lambda *a, **k: _Resp(status=404)):
            r = self.c.post('/api/voice/speak', json={'text': 'Bonjour'}, headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()['code'], 'voice_engine')

    def test_speak_does_not_probe_the_runner_per_sentence(self):
        # A slow runner (sudo docker inspect) once per sentence silenced the
        # voice mid-conversation: the sidecar is called directly.
        up = _Resp(chunks=[b'\x00\x00'], headers={'X-Sample-Rate': '24000'})
        with self._stack(KYUTAI, proc='unreachable', post=lambda *a, **k: up):
            r = self.c.post('/api/voice/speak', json={'text': 'Bonjour'}, headers={'X-CSRFToken': self.CSRF})
            r.get_data()
        self.assertEqual(r.status_code, 200)

    def test_routes_require_login(self):
        anon = portal.app.test_client()
        self.assertIn(anon.get('/api/voice/chat/status').status_code, (302, 401))
        self.assertIn(anon.post('/api/voice/speak', json={'text': 'x'}).status_code, (302, 400, 401, 403))


if __name__ == '__main__':
    unittest.main()
