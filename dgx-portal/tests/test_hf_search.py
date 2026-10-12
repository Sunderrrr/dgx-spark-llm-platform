"""Model search on Hugging Face: what the UI says about it must be true.

Written on 2026-09-13, after observing live that `search_hf_models`
swallowed EVERY exception to return `[]`. Two consequences, both checked
in the code before the fix:

1. **« HF is unreachable » displayed as « no model matches ».**
   The user looking for a model concluded it did not exist. The function
   now raises `HfIndisponible` and `/api/search` answers 503 with
   `{ok: false, error}` — the admin actions contract.
2. **The GB10 filter made a misleading « aucun résultat ».** The frequent
   case is not « this model does not exist » but « it is not tagged gb10 ».
   The answer thus carries `hors_gb10` (True / False / None = we do not
   know), which lets the UI offer the unlocking gesture instead of leaving
   the user at a dead end.

An unknown task filter makes HF answer 400: it is a call error, not an
outage, and the two must not carry the same status.

And `POST /request`, which answered `flash()` + 204 in ALL cases
(including « tu as déjà une demande en attente »): the page announced
« Demande envoyée ! » while no row existed. It now answers JSON with an honest status.
"""
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import requests
from werkzeug.security import generate_password_hash

import app as portal
import notify
import vllm_health
from vllm_health import HfUnavailable, hf_model_outside_gb10, search_hf_models


def _response(payload, status=200, headers=None):
    r = MagicMock()
    r.ok = 200 <= status < 300
    r.status_code = status
    r.json.return_value = payload
    r.headers = headers or {}
    return r


MODEL = {'id': 'org/modele', 'modelId': 'org/modele', 'downloads': 10,
          'tags': ['text-generation', 'gb10', 'gguf'], 'gated': False}


class TestAccount(unittest.TestCase):
    """Creates the local account used by the tests.

    Necessary and not decorative: the account state is RE-READ at every
    kept request (`auth.etat_compte`), so a session carrying a name absent
    from `local_users` is refused — 401 on a JSON route, redirect on a
    page route. Without this line, all tests below would measure the guard
    instead of what they target.
    """

    NAME = 'ztest-hf'

    def _mkuser(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM local_users WHERE username=?", (self.NAME,))
            db.execute(
                "INSERT INTO local_users (username, password_hash, fullname, is_admin, "
                "group_name, max_budget, enabled, created_at) VALUES (?,?,?,0,NULL,NULL,1,?)",
                (self.NAME, generate_password_hash('MotDePasse123'), 'Chercheur de test', 0))
            db.commit()

    def _rmuser(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM user_sessions WHERE username=?", (self.NAME,))
            db.execute("DELETE FROM local_users WHERE username=?", (self.NAME,))
            db.commit()

    def _client(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['csrf'] = 'tok'
            s['username'] = self.NAME
            s['fullname'] = 'Chercheur de test'
            s['is_admin'] = False
            # `auth_at` is MANDATORY: without it `_session_expired` treats the
            # session as predating the expiry mechanism, thus expired — we would
            # measure the guard instead of the search.
            s['auth_at'] = int(time.time())
        return c


class RechercheHfTest(unittest.TestCase):
    def test_network_outage_raises_instead_of_empty(self):
        with patch.object(vllm_health.requests, 'get',
                          side_effect=requests.ConnectionError('boom')):
            with self.assertRaises(HfUnavailable):
                search_hf_models('qwen')

    def test_http_error_status_raises(self):
        for code in (401, 429, 500, 503):
            with self.subTest(code=code):
                with patch.object(vllm_health.requests, 'get',
                                  return_value=_response({'error': 'nope'}, status=code)):
                    with self.assertRaises(HfUnavailable):
                        search_hf_models('qwen')

    def test_unreadable_response_raises(self):
        r = _response(None)
        r.json.side_effect = ValueError('pas du JSON')
        with patch.object(vllm_health.requests, 'get', return_value=r):
            with self.assertRaises(HfUnavailable):
                search_hf_models('qwen')

    def test_success_annotates_engine_and_requests_full_fields(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_response([dict(MODEL)])) as get:
            out = search_hf_models('qwen', 'text-generation', gb10_only=True, skip=24)
        self.assertEqual(len(out), 1)
        # GGUF → llama.cpp: this is the badge telling the user which engine
        # will serve the model.
        self.assertEqual(out[0]['engine'], 'llamacpp')
        params = get.call_args.kwargs['params']
        self.assertEqual(params['skip'], 24)
        self.assertEqual(params['limit'], vllm_health._SEARCH_PAGE_SIZE)
        # `full=true` brings `gated`: the failure cause really met on this repo
        # (the Flash-Next mmproj), which we want to see BEFORE launching.
        self.assertEqual(params['full'], 'true')
        # `siblings` is what `full=true` reports that is heaviest (58 % of the
        # response, measured) and nothing reads it: it must not come out of here.
        self.assertNotIn('siblings', out[0])

    def test_gb10_filter_passed_never_imposed(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_response([dict(MODEL)])) as get:
            search_hf_models('qwen', None, gb10_only=True)
            self.assertEqual(get.call_args.kwargs['params']['filter'], [vllm_health.GB10_TAG])
            # Without asking for it, NO filter: this is the 2026-09-14 fix.
            search_hf_models('qwen', None, gb10_only=False)
            self.assertNotIn('filter', get.call_args.kwargs['params'])
            # A task is still possible, but it adds instead of replacing: several
            # `filter` = AND on the HF side.
            search_hf_models('qwen', 'text-generation', gb10_only=True)
            self.assertEqual(sorted(get.call_args.kwargs['params']['filter']),
                             sorted(['text-generation', vllm_health.GB10_TAG]))

    def test_default_search_covers_all_hugging_face(self):
        """The reported case: « ornith-1.5 » (4 repos, including the GGUFs) is
        tagged neither `gb10` nor only `text-generation`. The two automatic
        filters made it unfindable, and nothing said so."""
        with patch.object(vllm_health.requests, 'get',
                          return_value=_response([dict(MODEL)])) as get:
            search_hf_models('ornith-1.5')
        params = get.call_args.kwargs['params']
        self.assertEqual(params['search'], 'ornith-1.5')
        self.assertNotIn('filter', params)
        self.assertEqual(params['sort'], 'downloads')

    def test_has_more_comes_from_hf_link_header(self):
        """A full page does not PROVE there are others: `Link` says so."""
        lien = {'Link': '<https://huggingface.co/api/models?search=q&skip=48>; rel="next"'}
        with patch.object(vllm_health.requests, 'get',
                          return_value=_response([dict(MODEL)], headers=lien)):
            _, more = vllm_health.search_hf_models_page('q')
        self.assertTrue(more)
        with patch.object(vllm_health.requests, 'get',
                          return_value=_response([dict(MODEL)])):
            _, more = vllm_health.search_hf_models_page('q')
        self.assertFalse(more)

    def test_hf_token_sent_as_header_never_leaked(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'hf_token')
            with patch.dict(os.environ, {'HF_TOKEN': ''}), \
                 patch.object(vllm_health, 'HF_TOKEN_FILE', path):
                # No file: anonymous search, no authorization header.
                self.assertFalse(vllm_health.hf_token_present())
                with patch.object(vllm_health.requests, 'get',
                                  return_value=_response([dict(MODEL)])) as get:
                    search_hf_models('qwen')
                self.assertEqual(get.call_args.kwargs['headers'], {})
                # Token present: it goes in a header, and only its PRESENCE is said.
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('hf_jeton_de_test\n')
                self.assertTrue(vllm_health.hf_token_present())
                with patch.object(vllm_health.requests, 'get',
                                  return_value=_response([dict(MODEL)])) as get:
                    search_hf_models('qwen')
                self.assertEqual(get.call_args.kwargs['headers'],
                                 {'Authorization': 'Bearer hf_jeton_de_test'})

    def test_refused_token_differs_from_outage(self):
        """401 with a token = « remplace-le », not « HF est en panne »."""
        r = _response({'error': 'invalid token'}, status=401)
        with patch.object(vllm_health, '_hf_token', return_value='hf_stale'), \
             patch.object(vllm_health.requests, 'get', return_value=r):
            with self.assertRaises(HfUnavailable) as ctx:
                search_hf_models('qwen')
        self.assertIn('jeton', str(ctx.exception))
        with patch.object(vllm_health, '_hf_token', return_value=''), \
             patch.object(vllm_health.requests, 'get', return_value=r):
            with self.assertRaises(HfUnavailable) as ctx:
                search_hf_models('qwen')
        self.assertIn('401', str(ctx.exception))

    def test_outside_gb10_answers_true_false_or_unknown(self):
        with patch.object(vllm_health.requests, 'get', return_value=_response([dict(MODEL)])):
            self.assertTrue(hf_model_outside_gb10('qwen'))
        with patch.object(vllm_health.requests, 'get', return_value=_response([])):
            self.assertFalse(hf_model_outside_gb10('qwen'))
        # A doubt must not display as a « non ».
        with patch.object(vllm_health.requests, 'get',
                          side_effect=requests.ConnectionError('boom')):
            self.assertIsNone(hf_model_outside_gb10('qwen'))
        # Without a query, the question is meaningless: nothing to say.
        self.assertIsNone(hf_model_outside_gb10(''))


class ApiSearchTest(TestAccount):
    def setUp(self):
        portal.app.config['TESTING'] = True
        self._mkuser()
        self.c = self._client()

    def tearDown(self):
        self._rmuser()

    def test_hf_outage_answers_503_not_zero_results(self):
        with patch.object(vllm_health.requests, 'get',
                          side_effect=requests.ConnectionError('boom')):
            r = self.c.get('/api/search?q=qwen')
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertFalse(body['ok'])
        self.assertIn('Hugging Face', body['error'])
        self.assertEqual(body['results'], [])
        # `code`: without it, an English-speaking reader reads only a French sentence.
        self.assertEqual(body['code'], 'hf_unavailable')

    def test_unknown_task_refused_400_not_outage(self):
        with patch.object(vllm_health.requests, 'get') as get:
            r = self.c.get('/api/search?q=qwen&task=pas-une-tache')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])
        get.assert_not_called()

    def test_normal_search_ok_with_results(self):
        with patch.object(vllm_health.requests, 'get', return_value=_response([dict(MODEL)])):
            r = self.c.get('/api/search?q=qwen')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(len(body['results']), 1)
        self.assertEqual(body['results'][0]['engine'], 'llamacpp')
        # Without filter: the answer says so, and `hf_token` is only a boolean.
        self.assertFalse(body['gb10_only'])
        self.assertIn('hf_token', body)
        self.assertIsInstance(body['hf_token'], bool)

    def test_unfiltered_search_is_route_default(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_response([dict(MODEL)])) as get:
            self.c.get('/api/search?q=ornith-1.5')
        self.assertNotIn('filter', get.call_args.kwargs['params'])
        # `all=1` (the old parameter that disabled the filter) is still accepted:
        # it must not reintroduce a filter either.
        with patch.object(vllm_health.requests, 'get',
                          return_value=_response([dict(MODEL)])) as get:
            self.c.get('/api/search?q=ornith-1.5&all=1')
        self.assertNotIn('filter', get.call_args.kwargs['params'])
        # And an EMPTY search explores the catalogue (the most downloaded)
        # instead of returning nothing.
        with patch.object(vllm_health.requests, 'get',
                          return_value=_response([dict(MODEL)])) as get:
            body = self.c.get('/api/search').get_json()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(body['results']), 1)

    def test_empty_gb10_filter_says_it_exists_elsewhere(self):
        # 1st call (gb10-filtered, ASKED for): nothing. 2nd call (no filter): a model.
        with patch.object(vllm_health.requests, 'get',
                          side_effect=[_response([]), _response([dict(MODEL)])]):
            r = self.c.get('/api/search?q=ornith&gb10=1')
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['results'], [])
        self.assertTrue(body['outside_gb10'])

    def test_no_second_call_when_filter_finds_something(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_response([dict(MODEL)])) as get:
            r = self.c.get('/api/search?q=qwen&gb10=1')
        self.assertEqual(get.call_count, 1)
        self.assertIsNone(r.get_json()['outside_gb10'])


class ModelRequestTest(TestAccount):
    """POST /request: the page must no longer announce a send that did not happen."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self._mkuser()
        self.c = self._client()
        self._purge()

    def tearDown(self):
        self._purge()
        self._rmuser()

    def _purge(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM model_requests WHERE username=?", (self.NAME,))
            db.commit()

    def _post(self, **form):
        return self.c.post('/request', data=form, headers={'X-CSRFToken': 'tok'})

    def test_empty_identifier_refused(self):
        r = self._post(model_id='   ')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()['code'], 'identifier_required')

    def test_request_recorded_then_duplicate_refused(self):
        with patch.object(portal, 'notify_email', return_value=True), \
             patch.object(portal, 'notify_discord', return_value=True):
            r1 = self._post(model_id='org/ztest-modele', reason='parce que')
            r2 = self._post(model_id='org/ztest-modele')
        self.assertEqual(r1.status_code, 200)
        body = r1.get_json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['email_sent'])
        self.assertTrue(body['discord_sent'])
        # Before: silent 204, so « Demande envoyée ! » even for this duplicate.
        self.assertEqual(r2.status_code, 409)
        self.assertFalse(r2.get_json()['ok'])
        self.assertEqual(r2.get_json()['code'], 'already_pending')

    def test_no_notification_channel_warns_without_failing(self):
        with patch.object(portal, 'notify_email', return_value=False), \
             patch.object(portal, 'notify_discord', return_value=False):
            r = self._post(model_id='org/ztest-modele-2')
        body = r.get_json()
        # The request IS recorded: a warning, not an error.
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body['ok'])
        self.assertIn('warning', body)


class NotifyDiscordTest(unittest.TestCase):
    """`notify_discord` returned nothing, so `discord_sent` lied."""

    def test_returns_false_without_webhook(self):
        with patch.object(notify, 'discord_config', return_value={'webhook': ''}):
            self.assertFalse(notify.notify_discord('m', 'u', 'U', 'r'))

    def test_returns_true_when_webhook_answers(self):
        with patch.object(notify, 'discord_config', return_value={'webhook': 'https://discord.invalid/hook'}), \
             patch.object(notify.requests, 'post', return_value=_response({}, status=204)):
            self.assertTrue(notify.notify_discord('m', 'u', 'U', 'r'))

    def test_returns_false_when_webhook_fails(self):
        with patch.object(notify, 'discord_config', return_value={'webhook': 'https://discord.invalid/hook'}), \
             patch.object(notify.requests, 'post', return_value=_response({}, status=500)):
            self.assertFalse(notify.notify_discord('m', 'u', 'U', 'r'))
        with patch.object(notify, 'discord_config', return_value={'webhook': 'https://discord.invalid/hook'}), \
             patch.object(notify.requests, 'post',
                          side_effect=requests.ConnectionError('boom')):
            self.assertFalse(notify.notify_discord('m', 'u', 'U', 'r'))


if __name__ == '__main__':
    unittest.main()
