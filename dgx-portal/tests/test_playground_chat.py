"""The PLAYGROUND end to end: `/playground/chat` and its dependencies.

Why this file exists: the portal's most used route was covered by NO
test. `tests/test_app.py` locked in Support, sharing, the title and the
summary, but never the playground flow — thus never the SSE contract, the
bounding of the settings, the `reasoning_effort` fallback, the quota
notice, the end of stream without `finish_reason`, nor the client
abandonment. It is exactly there that the defects fixed on 2026-09-14
piled up (missing heartbeat during page reads, unbounded tool prompt,
false TTFT, missing `[DONE]`, context budget on the window not the input).

No model is called: the LiteLLM upstream is replaced by a fake,
steerable SSE response (`_FauxAmont`). The portal's read thread, for its
part, really runs — it carries the defects we want to forbid.
"""
import json
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from flask import session

import app as portal
import chat_routes as chat
import conversation_routes as conv_routes
import websearch_tools as outils_web


def _delta(texte):
    """A content frame, as LiteLLM produces it."""
    return ("data: " + json.dumps({'choices': [{'delta': {'content': texte}}]})).encode()


def _fin(reason='stop'):
    return ("data: " + json.dumps({'choices': [{'delta': {},
                                                'finish_reason': reason}]})).encode()


def _usage(n=42, entree=17):
    return ("data: " + json.dumps({'choices': [{'delta': {}}],
                                   'usage': {'completion_tokens': n,
                                             'prompt_tokens': entree}})).encode()


class _FauxAmont:
    """Fake upstream SSE response.

    `iter_lines` yields BYTES: the portal decodes them itself
    (`line.decode`), so yielding `str` would raise the generator — a real
    trap, not a test convenience.
    """

    def __init__(self, lignes=None, statut=200, corps_json=None, bloquant=None):
        self.statut = statut
        self.lignes = list(lignes or [])
        self.closed = False
        self.corps_json = corps_json or {}
        # Event released by close(): proves that a leaving client does close the
        # upstream connection (and thus frees the engine slot).
        self.bloquant = bloquant
        self.debloque = threading.Event()

    @property
    def ok(self):
        return 200 <= self.statut < 300

    @property
    def status_code(self):
        return self.statut

    def json(self):
        return self.corps_json

    def close(self):
        self.closed = True
        self.debloque.set()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def iter_lines(self, decode_unicode=False):
        for ligne in self.lignes:
            yield ligne
        if self.bloquant is not None:
            self.debloque.wait(timeout=5)
            for ligne in self.bloquant:
                yield ligne


class _BasePlayground(unittest.TestCase):
    """Common patches: no network call, no model, no memory."""

    CSRF = 'test-csrf'

    def _standard(self, stack, post=None, running=('fake-model',),
                  keys=({'key': 'sk-user-123'},), limites=None, entree=None,
                  web=False, image=False, memoire_off=True):
        stack.enter_context(patch.object(chat, 'maintenance_block_sse', return_value=None))
        stack.enter_context(patch.object(chat, '_chat_rate_limited', return_value=None))
        stack.enter_context(patch.object(chat, 'get_running_models', return_value=list(running)))
        stack.enter_context(patch.object(chat, 'get_user_keys', return_value=list(keys)))
        stack.enter_context(patch.object(chat, 'quota_depasse_reset', return_value=None))
        stack.enter_context(patch.object(chat, '_recherche_pertinente', return_value=web))
        stack.enter_context(patch.object(chat, 'websearch_active', return_value=web))
        stack.enter_context(patch.object(chat, 'image_disponible', return_value=image))
        stack.enter_context(patch.object(chat, '_image_demandee', return_value=image))
        if memoire_off:
            stack.enter_context(patch.object(chat.memoire, '_mem_enabled', return_value=False))
        if limites is not None:
            stack.enter_context(patch.object(chat, '_playground_model_limits',
                                             return_value=limites))
        if entree is not None:
            stack.enter_context(patch.object(chat, '_playground_input_limit',
                                             return_value=entree))
        if post is not None:
            stack.enter_context(patch.object(chat.requests, 'post', side_effect=post))
        return stack

    def _flux(self, corps, post=None, **kw):
        """Runs `/playground/chat` and returns the full SSE body (text)."""
        with ExitStack() as stack:
            self._standard(stack, post=post, **kw)
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=corps):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                resp = chat.playground_chat()
                return resp.get_data(as_text=True)

    def _db(self):
        """Application context: `get_db` parks its connection in `g`."""
        return portal.app.app_context()

    def _client(self, username='demo'):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['username'] = username
            s['auth_at'] = int(time.time())
            s['csrf'] = self.CSRF
        return c

    def _amont_unique(self, amont, vus=None, erreur=None):
        """A fake `requests.post` recording the sent body."""
        def _post(url, headers=None, json=None, stream=False, timeout=None):
            if vus is not None:
                vus.append({'url': url, 'headers': headers, 'json': json,
                            'timeout': timeout})
            if erreur is not None:
                raise erreur
            return amont
        return _post

    def _corps(self, msgs=None, **extra):
        corps = {'messages': msgs if msgs is not None
                 else [{'role': 'user', 'content': 'Salut'}]}
        corps.update(extra)
        return corps


# ── 1. The SSE contract ────────────────────────────────────────────────────

class FluxSSETest(_BasePlayground):
    """What the client receives, in order, and what it never receives."""

    def test_ouverture_avant_tout_travail(self):
        """In WSGI the headers only leave at the first yield: without this
        comment, the frontend proxy cuts with a 503 before the generation
        starts."""
        amont = _FauxAmont([_delta('Bonjour'), _fin()])
        corps = self._flux(self._corps(), post=self._amont_unique(amont))
        self.assertTrue(corps.startswith(': ouverture'), corps[:60])

    def test_texte_relaye_et_usage_transmis(self):
        amont = _FauxAmont([_delta('Bon'), _delta('jour'), _usage(42), _fin(),
                            b'data: [DONE]'])
        corps = self._flux(self._corps(), post=self._amont_unique(amont))
        self.assertIn('Bon', corps)
        self.assertIn('jour', corps)
        self.assertIn('"completion_tokens": 42', corps)
        self.assertIn('[DONE]', corps)

    def test_le_prompt_part_avec_le_system_et_les_reglages(self):
        vus = []
        amont = _FauxAmont([_fin()])
        self._flux(self._corps(system='Tu es bref.', temperature=0.25,
                               max_tokens=777, top_p=0.3),
                   post=self._amont_unique(amont, vus))
        envoye = vus[0]['json']
        self.assertEqual(envoye['model'], 'fake-model')
        self.assertEqual(envoye['temperature'], 0.25)
        self.assertEqual(envoye['max_tokens'], 777)
        self.assertEqual(envoye['top_p'], 0.3)
        self.assertEqual(envoye['messages'][0]['role'], 'system')
        # The system carries the persona THEN the current date/time (the model
        # has no clock) — the persona is the prefix, the clock cannot be cut.
        self.assertTrue(envoye['messages'][0]['content'].startswith('Tu es bref.'))
        self.assertIn('Nous sommes le', envoye['messages'][0]['content'])
        self.assertEqual(envoye['messages'][1]['content'], 'Salut')
        self.assertTrue(envoye['stream'])
        self.assertEqual(envoye['stream_options'], {'include_usage': True})

    def test_la_cle_de_l_utilisateur_est_utilisee(self):
        """The playground spends the BUDGET: never the master key."""
        vus = []
        amont = _FauxAmont([_fin()])
        self._flux(self._corps(), post=self._amont_unique(amont, vus))
        self.assertEqual(vus[0]['headers']['Authorization'], 'Bearer sk-user-123')

    def test_fin_sans_finish_reason_dit_length_et_termine(self):
        """Upstream stream closed mid-word: the portal fabricates a
        `finish_reason: length` — and must ALSO send `[DONE]`. Without the
        sentinel, every OpenAI-compatible client waits forever."""
        amont = _FauxAmont([_delta('Réponse cou')])          # ni finish, ni DONE
        corps = self._flux(self._corps(), post=self._amont_unique(amont))
        self.assertIn('"finish_reason": "length"', corps)
        self.assertIn('data: [DONE]', corps)

    def test_quota_depasse_notice_structuree(self):
        amont = _FauxAmont([], statut=429)
        corps = self._flux(self._corps(), post=self._amont_unique(amont))
        self.assertIn('cronos_notice', corps)
        self.assertIn('quota_exceeded', corps)

    def test_erreur_modele_notice_structuree(self):
        amont = _FauxAmont([], statut=500)
        corps = self._flux(self._corps(), post=self._amont_unique(amont))
        self.assertIn('model_replied_error', corps)
        self.assertIn('500', corps)

    def test_demande_d_image_service_arrete_notice(self):
        """EXPLICIT image request with the sidecar off: the model would answer
        « je ne peux pas générer d'images » — false, it is the service that
        is missing. The structured notice says so (seen on 2026-10-02 with
        MiMo, which denies the capability instead of returning the error)."""
        with ExitStack() as stack:
            self._standard(stack, post=self._amont_unique(
                _FauxAmont([_delta("Je ne peux pas générer d'images."), _fin()])))
            # `_standard` ties `_image_demandee` and `image_disponible` to the same
            # flag: we separate them to get only the request.
            stack.enter_context(patch.object(chat, '_image_demandee', return_value=True))
            stack.enter_context(patch.object(chat, 'image_disponible', return_value=False))
            with portal.app.test_request_context(
                    '/playground/chat', method='POST',
                    json=self._corps(msgs=[{'role': 'user',
                                            'content': 'Crée une image de chat.'}])):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                corps = chat.playground_chat().get_data(as_text=True)
        self.assertIn('cronos_notice', corps)
        self.assertIn('image_service_off', corps)

    def test_demande_d_image_service_pret_sans_notice(self):
        """Same request, service ready: the tool arms, no notice goes out — the
        notice must never announce a switched-off service that is not."""
        def _phase(*a, **k):
            return
            yield                       # empty generator

        with ExitStack() as stack:
            self._standard(stack, post=self._amont_unique(
                _FauxAmont([_delta('Voilà.'), _fin()])), image=True)
            stack.enter_context(patch.object(chat, '_phase_outils', side_effect=_phase))
            with portal.app.test_request_context(
                    '/playground/chat', method='POST',
                    json=self._corps(msgs=[{'role': 'user',
                                            'content': 'Crée une image de chat.'}])):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                corps = chat.playground_chat().get_data(as_text=True)
        self.assertNotIn('image_service_off', corps)

    def test_timeout_de_lecture_est_une_erreur_de_transport(self):
        """LiteLLM unreachable is NOT a model error: the answer must say so, not
        announce a code 0. The notice is STRUCTURED (`model_unreachable`): the
        frontend writes the sentence, the server sends no text."""
        import requests as _rq
        corps = self._flux(self._corps(),
                           post=self._amont_unique(None, erreur=_rq.exceptions.ConnectTimeout()))
        self.assertIn('cronos_notice', corps)
        self.assertIn('model_unreachable', corps)
        self.assertNotIn('erreur (0)', corps)
        # The READ timeout (anti-stuck slot) is "no answer in time", which is
        # another id of the same contract — never a model error either.
        corps = self._flux(self._corps(),
                           post=self._amont_unique(None, erreur=_rq.exceptions.ReadTimeout()))
        self.assertIn('model_timeout', corps)
        self.assertNotIn('erreur (0)', corps)


# ── 2. Settings: bounding and reasoning ────────────────────────────────────

class ReglagesTest(_BasePlayground):

    def test_temperature_max_tokens_top_p_bornees(self):
        vus = []
        self._flux(self._corps(temperature=99, max_tokens=10 ** 9, top_p=5),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus))
        envoye = vus[0]['json']
        self.assertEqual(envoye['temperature'], 2.0)
        self.assertEqual(envoye['max_tokens'], 131072)
        self.assertEqual(envoye['top_p'], 1.0)

    def test_valeurs_non_numeriques_retombent_sur_le_defaut(self):
        vus = []
        self._flux(self._corps(temperature='chaud', max_tokens=None, top_p='x'),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus))
        envoye = vus[0]['json']
        self.assertEqual(envoye['temperature'], 0.7)
        self.assertEqual(envoye['max_tokens'], 4096)
        self.assertEqual(envoye['top_p'], 1.0)

    def test_system_tronque_a_4000(self):
        vus = []
        self._flux(self._corps(system='x' * 9000),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus))
        # Truncated at 4000 chars, then the date/time line is APPENDED: a long
        # persona must never be what cuts the clock.
        contenu = vus[0]['json']['messages'][0]['content']
        self.assertTrue(contenu.startswith('x' * 4000))
        self.assertIn('Nous sommes le', contenu[4000:])

    def test_le_system_porte_la_date_et_l_heure_courantes(self):
        """The model has NO clock (MiMo, measured 2026-10-02: « je n'ai pas
        accès à la date en temps réel »): the system prompt carries the current
        date and time, refreshed at EVERY request so a conversation spanning
        midnight stays right."""
        vus = []
        self._flux(self._corps(), post=self._amont_unique(_FauxAmont([_fin()]), vus))
        contenu = vus[0]['json']['messages'][0]['content']
        self.assertIn('Nous sommes le', contenu)
        # weekday + day + month + year + "à HH:MM (timezone)"
        self.assertRegex(contenu,
                         r'Nous sommes le \S+ \d{1,2} \S+ \d{4} à \d{2}:\d{2} \(.+\)\.')

    def test_max_tokens_rabaisse_a_la_fenetre_restante(self):
        """The output cap ADDS UP to the prompt: beyond, the engine refuses the
        request instead of answering shorter."""
        vus = []
        gros = 'a' * 30000          # ~10 000 tokens at 3 characters/token
        self._flux(self._corps([{'role': 'user', 'content': gros}], max_tokens=131072),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus),
                   limites={'fake-model': 20000})
        self.assertLess(vus[0]['json']['max_tokens'], 131072)
        self.assertGreaterEqual(vus[0]['json']['max_tokens'], 256)

    def test_max_tokens_jamais_sous_le_plancher(self):
        vus = []
        self._flux(self._corps([{'role': 'user', 'content': 'a' * 90000}],
                               max_tokens=131072),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus),
                   limites={'fake-model': 20000})
        self.assertEqual(vus[0]['json']['max_tokens'], 256)

    def test_history_bornee_sur_l_entree_reelle_pas_sur_la_fenetre(self):
        """`effective_ctx` (262 144 measured) describes the WINDOW; the input
        limit is lower (196 608 advertised by ctx_split). Bounding on the
        window let prompts go out that the engine refused with 400."""
        vus = []
        msgs = [{'role': 'user', 'content': 'vieux ' + 'a' * 20000},
                {'role': 'assistant', 'content': 'b' * 20000},
                {'role': 'user', 'content': 'récent'}]
        self._flux(self._corps(msgs), post=self._amont_unique(_FauxAmont([_fin()]), vus),
                   entree=14000)          # budget = (14000-8192)x3 = 17 424 car.
        envoyes = vus[0]['json']['messages']
        # The last exchange is ALWAYS kept; the older one is dropped whole.
        self.assertEqual(envoyes[-1]['content'], 'récent')
        self.assertNotIn('vieux ', ' '.join(m['content'] for m in envoyes))

    def test_input_limit_utilise_ctx_split(self):
        """The input limit comes from `ctx_split` (the same source as LiteLLM)."""
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM model_configs WHERE name='ctx-test'")
            db.execute("INSERT INTO model_configs (name, vllm_args, engine, "
                       "hf_model_id, added_at) VALUES (?,?,?,?,?)",
                       ('ctx-test', '--ctx-size 1048576 --parallel 4 --n-predict 65536',
                        'llamacpp', 'test/ctx-test', '2026-09-14T00:00:00'))
            db.commit()
            # 1048576 / 4 slots - 65536 de marge de sortie = 196608, exactement
            # what LiteLLM advertises in production.
            self.assertEqual(chat._playground_input_limit('ctx-test'), 196608)
            # And the WINDOW is worth the per-slot context: this gap (262 144
            # against 196 608) is what made the history bound too high.
            from vllm_health import effective_ctx
            self.assertEqual(effective_ctx('--ctx-size 1048576 --parallel 4 '
                                           '--n-predict 65536', 'llamacpp'), 262144)
            self.assertIsNone(chat._playground_input_limit('inconnu-du-catalogue'))

    def test_reasoning_active_le_template(self):
        vus = []
        self._flux(self._corps(reasoning=True, reasoning_effort='medium'),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus))
        self.assertEqual(vus[0]['json']['chat_template_kwargs'],
                         {'enable_thinking': True, 'reasoning_effort': 'medium'})

    def test_effort_inconnu_n_est_pas_transmis(self):
        vus = []
        self._flux(self._corps(reasoning=False, reasoning_effort='turbo'),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus))
        self.assertEqual(vus[0]['json']['chat_template_kwargs'], {'enable_thinking': False})

    def test_effort_high_n_est_plus_transmis(self):
        """The served model REFUSES 'high' (measured 500 Jinja): accepting
        it could only produce a lost round-trip, paid twice in
        preloading."""
        vus = []
        self._flux(self._corps(reasoning=True, reasoning_effort='high'),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus))
        self.assertEqual(vus[0]['json']['chat_template_kwargs'], {'enable_thinking': True})
        self.assertEqual(len(vus), 1, "un second appel ne doit pas être tenté")

    def test_effort_refuse_par_le_modele_est_retente_sans(self):
        """A 500 on the effort value is a template refusal, not an outage: we
        retry once WITHOUT effort rather than kill the round."""
        vus = []
        appels = {'n': 0}

        def _post(url, headers=None, json=None, stream=False, timeout=None):
            appels['n'] += 1
            vus.append(json)
            if appels['n'] == 1:
                return _FauxAmont([], statut=500)
            return _FauxAmont([_delta('Bonjour'), _fin()])

        corps = self._flux(self._corps(reasoning=True, reasoning_effort='medium'),
                           post=_post)
        self.assertEqual(appels['n'], 2)
        self.assertIn('reasoning_effort', vus[0]['chat_template_kwargs'])
        self.assertNotIn('reasoning_effort', vus[1]['chat_template_kwargs'])
        self.assertIn('Bonjour', corps)


# ── 3. Input guards ───────────────────────────────────────────────────────

class GardesTest(_BasePlayground):

    def test_message_vide(self):
        corps = self._flux({'messages': []})
        self.assertIn('empty_message', corps)

    def test_roles_inconnus_ignores(self):
        corps = self._flux({'messages': [{'role': 'tool', 'content': 'x'}]})
        self.assertIn('empty_message', corps)

    def test_aucun_modele(self):
        corps = self._flux(self._corps(), running=())
        self.assertIn('no_model_running', corps)

    def test_modele_inconnu_retombe_sur_le_modele_servi(self):
        vus = []
        self._flux(self._corps(model='modele-inexistant'),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus))
        self.assertEqual(vus[0]['json']['model'], 'fake-model')

    def test_sans_cle_api_notice_et_aucun_appel(self):
        corps = self._flux(self._corps(), keys=(),
                           post=self._amont_unique(None,
                                                   erreur=AssertionError('appel interdit')))
        self.assertIn('no_api_key', corps)

    def test_quota_connu_avant_l_appel(self):
        with ExitStack() as stack:
            self._standard(stack, post=self._amont_unique(
                None, erreur=AssertionError('appel interdit')))
            stack.enter_context(patch.object(chat, 'quota_depasse_reset', return_value=86400))
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=self._corps()):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                corps = chat.playground_chat().get_data(as_text=True)
        self.assertIn('quota_exceeded', corps)

    def test_maintenance_bloque_le_playground(self):
        from flask import Response
        with ExitStack() as stack:
            self._standard(stack)
            stack.enter_context(patch.object(
                chat, 'maintenance_block_sse',
                return_value=Response('data: {"choices":[{"delta":{"content":"Maintenance"}}]}\n\n',
                                      mimetype='text/event-stream')))
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=self._corps()):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                corps = chat.playground_chat().get_data(as_text=True)
        self.assertIn('Maintenance', corps)

    def test_trop_de_messages_d_affilee(self):
        with ExitStack() as stack:
            self._standard(stack)
            stack.enter_context(patch.object(chat, '_chat_rate_limited', return_value=7))
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=self._corps()):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                corps = chat.playground_chat().get_data(as_text=True)
        self.assertIn('chat_rate_limited', corps)
        self.assertIn('"wait": 7', corps)

    def test_rate_limit_notice_structuree_sans_phrase(self):
        """The rate-limit refusal is a STRUCTURED notice, exactly
        `{"cronos_notice": {"id": "chat_rate_limited", "wait": N}}`: the server
        sends the id and the wait, the frontend writes the sentence. No raw
        French sentence may leak into the stream."""
        with ExitStack() as stack:
            self._standard(stack)
            stack.enter_context(patch.object(chat, '_chat_rate_limited', return_value=12))
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=self._corps()):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                corps = chat.playground_chat().get_data(as_text=True)
        trames = [l for l in corps.splitlines() if l.startswith('data: ')]
        payloads = [json.loads(l[6:]) for l in trames if l != 'data: [DONE]']
        self.assertEqual(payloads,
                         [{'cronos_notice': {'id': 'chat_rate_limited', 'wait': 12}}])
        self.assertEqual(trames[-1], 'data: [DONE]')
        for phrase in ('Trop de', 'réessaie', 'messages', 'choices'):
            self.assertNotIn(phrase, corps)

    def test_csrf_manquant_refuse_400(self):
        r = self._client().post('/playground/chat', json=self._corps())
        self.assertEqual(r.status_code, 400)

    def test_anonyme_refuse_401(self):
        r = portal.app.test_client().post('/playground/chat', json=self._corps(),
                                          headers={'X-CSRFToken': 'x'})
        self.assertIn(r.status_code, (400, 401))

    def test_json_illisible_ne_casse_pas(self):
        r = self._client().post('/playground/chat', data='pas du json',
                                headers={'X-CSRFToken': self.CSRF,
                                         'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'empty_message', r.data)


# ── 4. Client abandonment and metrics ──────────────────────────────────────

class MetriquesEtAbandonTest(_BasePlayground):

    def test_ttft_inclut_la_phase_outils(self):
        """The published TTFT must be the delay ACTUALLY endured: it started after
        the search, so a request spending 40 s searching announced
        « TTFT 1,2 s »."""
        mesures = []

        def _phase(*a, **k):
            time.sleep(0.35)
            return
            yield                       # empty generator

        with ExitStack() as stack:
            self._standard(stack, post=self._amont_unique(
                _FauxAmont([_delta('Bonjour'), _fin()])), web=True)
            stack.enter_context(patch.object(chat, '_phase_outils', side_effect=_phase))
            stack.enter_context(patch.object(chat, 'enregistrer_ttft',
                                             side_effect=lambda ms: mesures.append(ms)))
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=self._corps()):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                chat.playground_chat().get_data(as_text=True)
        self.assertTrue(mesures, 'aucun TTFT enregistré')
        self.assertGreaterEqual(mesures[0], 300,
                                f'TTFT {mesures[0]:.0f} ms : la phase outils est exclue')

    def test_client_qui_part_ferme_l_amont(self):
        """Tab closed / Stop: the read thread must leave its `with`, thus closing
        the upstream connection and freeing the engine slot."""
        amont = _FauxAmont([_delta('Bon'), _delta('jour')],
                           bloquant=[_delta('jamais'), _fin()])
        with ExitStack() as stack:
            self._standard(stack, post=self._amont_unique(amont))
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=self._corps()):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                resp = chat.playground_chat()
                it = iter(resp.response)
                next(it)                      # ": ouverture"
                next(it)                      # first relayed delta
                it.close()                    # the client leaves
                amont.debloque.set()          # the fake upstream hands back
                for _ in range(50):
                    if amont.closed:
                        break
                    time.sleep(0.05)
        self.assertTrue(amont.closed, "la connexion amont n'a pas été fermée")

    def test_le_slot_en_vol_est_libere(self):
        """`inflight_requests` must keep no row after an abandonment."""
        amont = _FauxAmont([_delta('a')])
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM inflight_requests WHERE username='demo'")
            db.commit()
        with ExitStack() as stack:
            self._standard(stack, post=self._amont_unique(amont))
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=self._corps()):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                chat.playground_chat().get_data(as_text=True)
        with self._db():
            reste = portal.get_db().execute(
                "SELECT COUNT(*) c FROM inflight_requests "
                "WHERE username='demo'").fetchone()['c']
        self.assertEqual(reste, 0)


# ── 5. Tool phase: heartbeat, bounds, visible errors ───────────────────────

IMG = 'data:image/jpeg;base64,' + 'A' * 64


class ImagesTest(_BasePlayground):
    """Attached images: sent as `image_url` parts to the ONLY models that see."""

    def _envoye(self, msgs, voit=True):
        vus = []
        with patch.object(chat, '_playground_model_vision',
                          return_value={'fake-model': voit}):
            self._flux(self._corps(msgs), post=self._amont_unique(_FauxAmont([_fin()]), vus))
        return vus[0]['json']['messages']

    def test_image_envoyee_en_partie_image_url(self):
        m = self._envoye([{'role': 'user', 'content': 'Que vois-tu ?', 'images': [IMG]}])
        self.assertEqual(m[-1]['content'], [
            {'type': 'image_url', 'image_url': {'url': IMG}},
            {'type': 'text', 'text': 'Que vois-tu ?'}])

    def test_modele_sans_vision_recoit_du_texte_seul(self):
        m = self._envoye([{'role': 'user', 'content': 'Que vois-tu ?', 'images': [IMG]}], voit=False)
        self.assertEqual(m[-1]['content'], 'Que vois-tu ?')

    def test_une_adresse_n_est_jamais_relayee(self):
        """An http URL would make the engine fetch the image (SSRF)."""
        m = self._envoye([{'role': 'user', 'content': 'x',
                           'images': ['http://169.254.169.254/latest', 'data:text/html;base64,AAAA']}])
        self.assertEqual(m[-1]['content'], 'x')

    def test_seules_les_images_recentes_partent(self):
        msgs = [{'role': 'user', 'content': f'q{i}', 'images': [IMG] * 4} for i in range(3)]
        m = self._envoye(msgs)
        # The system message (persona + current date) never carries an image:
        # the budget being tested is the conversation's.
        m = [x for x in m if x['role'] != 'system']
        nb = [sum(1 for p in x['content'] if p['type'] == 'image_url')
              if isinstance(x['content'], list) else 0 for x in m]
        self.assertEqual(nb, [0, 4, 4])

    def test_vision_lue_dans_les_arguments(self):
        self.assertTrue(chat._modele_voit('--ctx-size 8 --mmproj m.gguf', 'llamacpp'))
        self.assertFalse(chat._modele_voit('--ctx-size 8', 'llamacpp'))
        self.assertTrue(chat._modele_voit('--max-seq-len 8 --vision true', 'exllamav3'))
        self.assertFalse(chat._modele_voit('--vision false', 'exllamav3'))
        self.assertFalse(chat._modele_voit('--max-model-len 8', 'vllm'))
        self.assertTrue(chat._modele_voit('--limit-mm-per-prompt {"image":2}', 'vllm'))

    def test_images_conservees_a_la_sauvegarde(self):
        c = self._client()
        msgs = [{'role': 'user', 'content': 'a', 'images': [IMG, 'http://x/y.png']},
                {'role': 'assistant', 'content': 'b', 'images': [IMG]}]
        r = c.post('/conversations', data={'action': 'save', 'id': 'img-1', 'title': 't',
                                           'messages': json.dumps(msgs)},
                   headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(r.status_code, 200)
        g = c.get('/api/conversations/img-1').get_json()['conversation']['messages']
        self.assertEqual(g[0]['images'], [IMG])
        self.assertNotIn('images', g[1])


class PhaseOutilsTest(_BasePlayground):

    def test_battement_pendant_un_outil_long(self):
        """`lire_pages` can block 90 s without yielding; the frontend proxy cuts
        at 60 s WITHOUT A BYTE. The result was thus lost and the user saw
        « Erreur réseau » while the read succeeded."""
        def _outil_lent(nom, args, journal):
            time.sleep(0.4)
            journal.append({'etape': 'lecture_finie', 'outil': 'crawl4ai',
                            'lues': 1, 'urls': [], 'echecs': []})
            return 'contenu'

        with patch.object(outils_web, '_exec_web_tool', side_effect=_outil_lent), \
             patch.object(outils_web, 'BATTEMENT_OUTIL_S', 0.1):
            trames = list(outils_web._exec_web_tool_avec_battements('lire_pages', {}, []))
        battements = [t for t in trames if t == ': battement\n\n']
        self.assertGreaterEqual(len(battements), 2,
                                f'battements insuffisants : {trames}')

    def test_outil_qui_leve_est_rapporte(self):
        with patch.object(outils_web, '_exec_web_tool',
                          side_effect=RuntimeError('crawl4ai mort')), \
             patch.object(outils_web, 'BATTEMENT_OUTIL_S', 0.05):
            with self.assertRaises(RuntimeError):
                list(outils_web._exec_web_tool_avec_battements('lire_pages', {}, []))

    def test_le_prompt_d_outils_est_borne(self):
        """`OUTILS_TOTAL_MAX` only bounds the STARTING conversation: each round
        then stacked up to 4 results of 20 000 characters, reposted in full
        on the next round (~88 000 characters at the 2nd round)."""
        court = [{'role': 'user', 'content': 'x' * 8000}]
        for i in range(3):
            court.append({'role': 'assistant', 'content': '', 'tool_calls': [{'id': str(i)}]})
            court.append({'role': 'tool', 'tool_call_id': str(i),
                          'name': 'lire_pages', 'content': 'y' * 20000})
        taille_avant = sum(len(m['content']) for m in court)
        outils_web._borner_prompt_outils(court)
        taille_apres = sum(len(m['content']) for m in court)
        self.assertLessEqual(taille_apres, outils_web.OUTILS_PROMPT_MAX)
        self.assertLess(taille_apres, taille_avant)
        # No orphan `tool` message: a call without an answer breaks the template.
        ids_appeles = {tc['id'] for m in court if m.get('tool_calls')
                       for tc in m['tool_calls']}
        ids_repondus = {m.get('tool_call_id') for m in court if m.get('role') == 'tool'}
        self.assertEqual(ids_repondus - ids_appeles, set())

    def test_decision_d_outil_injoignable_est_dite(self):
        """A transport failure of the tool phase was SWALLOWED: the user had
        asked for a search and received a normal-looking answer, without a
        word."""
        import requests as _rq
        amont = _FauxAmont([_delta('Bonjour'), _fin()])
        appels = {'n': 0}

        def _post(url, headers=None, json=None, stream=False, timeout=None):
            appels['n'] += 1
            if appels['n'] == 1:            # the tool DECISION call
                raise _rq.exceptions.ConnectionError('litellm injoignable')
            return amont

        corps = self._flux(self._corps(), post=_post, web=True)
        self.assertIn('cronos_web', corps)
        self.assertIn('injoignable', corps)

    def test_outil_en_panne_est_dit_au_client(self):
        def _explose(*a, **k):
            raise RuntimeError('crawl4ai mort')

        amont = _FauxAmont([_delta('Bonjour'), _fin()])
        appels = {'n': 0}

        def _post(url, headers=None, json=None, stream=False, timeout=None):
            appels['n'] += 1
            if appels['n'] == 1:            # decision: the model asks for a tool
                return _FauxAmont([], corps_json={'choices': [{'message': {
                    'tool_calls': [{'id': '1', 'function': {
                        'name': 'lire_pages', 'arguments': '{"urls":["https://x"]}'}}]}}]})
            return amont

        with patch.object(outils_web, '_exec_web_tool', side_effect=_explose):
            corps = self._flux(self._corps(), post=_post, web=True)
        self.assertIn('panne', corps)

    def test_phase_outils_absente_sans_recherche(self):
        amont = _FauxAmont([_delta('Bonjour'), _fin()])
        corps = self._flux(self._corps(), post=self._amont_unique(amont), web=False)
        self.assertNotIn('cronos_web', corps)


# ── 6. Playground data ────────────────────────────────────────────────────

class DonneesPlaygroundTest(_BasePlayground):

    def test_data_expose_modeles_limites_et_cle(self):
        with patch.object(chat, 'get_running_models', return_value=['fake-model']), \
             patch.object(chat, 'get_user_keys', return_value=[{'key': 'sk-1'}]), \
             patch.object(chat, '_playground_model_limits',
                          return_value={'fake-model': 262144}):
            r = self._client().get('/api/playground/data')
        self.assertEqual(r.status_code, 200)
        corps = r.get_json()
        self.assertEqual(corps['running_models'], ['fake-model'])
        self.assertEqual(corps['model_limits'], {'fake-model': 262144})
        self.assertTrue(corps['has_key'])

    def test_data_sans_cle(self):
        with patch.object(chat, 'get_running_models', return_value=[]), \
             patch.object(chat, 'get_user_keys', return_value=[]):
            r = self._client().get('/api/playground/data')
        self.assertFalse(r.get_json()['has_key'])

    def test_data_exige_une_session(self):
        self.assertEqual(portal.app.test_client().get('/api/playground/data').status_code, 401)


# ── 7. Conversations: bounds, isolation, flags ─────────────────────────────

class ConversationsTest(_BasePlayground):

    def setUp(self):
        portal.app.config['TESTING'] = True
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM conversations")
            db.execute("DELETE FROM conversation_shares")
            db.commit()

    def _enregistre(self, client_id, messages, title='T', csrf=True):
        c = self._client()
        return c.post('/conversations',
                      data={'action': 'save', 'id': client_id, 'title': title,
                            'model': 'fake-model', 'messages': json.dumps(messages)},
                      headers={'X-CSRFToken': self.CSRF} if csrf else {})

    def test_aller_retour_et_drapeaux_conserves(self):
        self._enregistre('c1', [
            {'role': 'user', 'content': 'Question'},
            {'role': 'assistant', 'content': 'Réponse coupée', 'truncated': True},
            {'role': 'user', 'content': 'Caché ?', 'hidden': True},
        ])
        r = self._client().get('/api/conversations')
        convs = r.get_json()['conversations']
        self.assertEqual(len(convs), 1)
        msgs = convs[0]['messages']
        self.assertEqual([m['role'] for m in msgs], ['user', 'assistant', 'user'])
        self.assertTrue(msgs[1]['truncated'])
        self.assertTrue(msgs[2]['hidden'])

    def test_id_manquant_refuse(self):
        c = self._client()
        r = c.post('/conversations', data={'action': 'save', 'messages': '[]'},
                   headers={'X-CSRFToken': self.CSRF})
        self.assertFalse(r.get_json()['ok'])

    def test_messages_non_json_refuses(self):
        c = self._client()
        r = c.post('/conversations', data={'action': 'save', 'id': 'x', 'messages': 'pas du json'},
                   headers={'X-CSRFToken': self.CSRF})
        self.assertFalse(r.get_json()['ok'])

    def test_60_messages_maximum(self):
        self._enregistre('c2', [{'role': 'user', 'content': f'm{i}'} for i in range(80)])
        msgs = self._client().get('/api/conversations').get_json()['conversations'][0]['messages']
        self.assertEqual(len(msgs), 60)
        self.assertEqual(msgs[-1]['content'], 'm79')

    def test_rognage_du_total_conserve_le_dernier(self):
        gros = 'a' * 400000
        self._enregistre('c3', [{'role': 'assistant', 'content': gros},
                                {'role': 'assistant', 'content': gros},
                                {'role': 'user', 'content': gros},
                                {'role': 'assistant', 'content': 'dernier'}])
        msgs = self._client().get('/api/conversations').get_json()['conversations'][0]['messages']
        self.assertLessEqual(len(json.dumps(msgs)), conv_routes.CONV_MAX_CHARS)
        self.assertEqual(msgs[-1]['content'], 'dernier')

    def test_liste_bornee_et_conversation_a_la_demande(self):
        """30 full conversations make ~60 Mo of JSON loaded at every playground
        opening: past the budget, the list carries `messages_omis` and the
        conversation is requested one by one."""
        gros = 'a' * 400000
        for i in range(6):
            self._enregistre(f'g{i}', [{'role': 'user', 'content': gros + str(i)}])
        with patch.object(conv_routes, 'LISTE_MAX_OCTETS', 1_000_000):
            convs = self._client().get('/api/conversations').get_json()['conversations']
        omis = [c for c in convs if c.get('messages_omis')]
        complets = [c for c in convs if not c.get('messages_omis')]
        self.assertTrue(omis, 'aucune conversation omise : la borne ne joue pas')
        self.assertTrue(complets)
        # The first one is ALWAYS whole: we do not open on an empty thread.
        self.assertFalse(convs[0].get('messages_omis'))
        cible = omis[0]
        r = self._client().get(f"/api/conversations/{cible['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['conversation']['messages'])

    def test_conversation_d_un_autre_compte_invisible(self):
        self._enregistre('prive', [{'role': 'user', 'content': 'secret'}])
        r = self._client('zz-autre').get('/api/conversations/prive')
        self.assertEqual(r.status_code, 404)
        convs = self._client('zz-autre').get('/api/conversations').get_json()['conversations']
        self.assertEqual(convs, [])

    def test_suppression(self):
        self._enregistre('c4', [{'role': 'user', 'content': 'x'}])
        c = self._client()
        c.post('/conversations', data={'action': 'delete', 'id': 'c4'},
               headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(c.get('/api/conversations').get_json()['conversations'], [])

    def test_purge_au_dela_de_30(self):
        for i in range(31):
            self._enregistre(f'p{i}', [{'role': 'user', 'content': f'{i}'}], title=f'T{i}')
        with self._db():
            n = portal.get_db().execute(
                "SELECT COUNT(*) c FROM conversations WHERE username='demo'").fetchone()['c']
        self.assertEqual(n, conv_routes.CONVERSATIONS_MAX)

    def test_csrf_exige(self):
        r = self._client().post('/conversations', data={'action': 'delete', 'id': 'x'})
        self.assertEqual(r.status_code, 400)

    def test_partage_ne_montre_pas_les_messages_caches(self):
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM conversation_shares")
            db.commit()
        self._enregistre('c5', [
            {'role': 'user', 'content': 'Visible'},
            {'role': 'assistant', 'content': 'PROMPT INTERNE DE REPRISE', 'hidden': True},
            {'role': 'assistant', 'content': 'Réponse visible'},
        ])
        c = self._client()
        partage = c.post('/conversations/share', json={'client_id': 'c5'},
                         headers={'X-CSRFToken': self.CSRF}).get_json()
        self.assertTrue(partage['ok'])
        vue = c.get(f"/c/{partage['token']}").get_data(as_text=True)
        self.assertIn('Visible', vue)
        self.assertNotIn('PROMPT INTERNE DE REPRISE', vue)

    def test_jeton_de_partage_inconnu_404(self):
        self.assertEqual(self._client().get('/c/inconnu').status_code, 404)


# ── 8. Support feedback: invent nothing ───────────────────────────────────

class FeedbackTest(_BasePlayground):

    def _lignes(self):
        with self._db():
            return portal.get_db().execute(
                "SELECT COUNT(*) c FROM support_feedback "
                "WHERE username='demo'").fetchone()['c']

    def _dernier_vote(self):
        with self._db():
            return portal.get_db().execute(
                "SELECT vote, comment FROM support_feedback WHERE username='demo' "
                "ORDER BY id DESC LIMIT 1").fetchone()

    def test_corps_vide_n_enregistre_rien(self):
        """`int(vote or 0) > 0` made an EMPTY body a negative opinion: the route
        invented « pas utile » out of an absence."""
        c = self._client()
        avant = self._lignes()
        r = c.post('/support/feedback', json={}, headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._lignes(), avant)

    def test_vote_zero_refuse(self):
        r = self._client().post('/support/feedback', json={'vote': 0},
                                headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(r.status_code, 400)

    def test_vote_positif_enregistre(self):
        r = self._client().post('/support/feedback',
                                json={'vote': 1, 'comment': 'clair'},
                                headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(r.status_code, 200)
        row = self._dernier_vote()
        self.assertEqual(row['vote'], 1)
        self.assertEqual(row['comment'], 'clair')

    def test_vote_negatif_enregistre(self):
        r = self._client().post('/support/feedback', json={'vote': -1},
                                headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._dernier_vote()['vote'], -1)


# ── 9. Dictation: the sidecar's code is no longer overwritten ───────────────

class DicteeTest(_BasePlayground):

    def _envoie(self, contenu=b'RIFFxxxxWAVE', nom='rec.wav'):
        c = self._client()
        return c.post('/api/transcribe',
                      data={'audio': (__import__('io').BytesIO(contenu), nom)},
                      headers={'X-CSRFToken': self.CSRF},
                      content_type='multipart/form-data')

    def test_aucun_fichier(self):
        r = self._client().post('/api/transcribe', data={},
                                headers={'X-CSRFToken': self.CSRF},
                                content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)

    def test_transcription_nominale(self):
        class _R:
            ok = True
            status_code = 200

            def json(self):
                return {'text': 'bonjour le monde'}
        with patch.object(chat.requests, 'post', return_value=_R()):
            import asr_routes
            with patch.object(asr_routes.requests, 'post', return_value=_R()):
                r = self._envoie()
        self.assertIn(r.status_code, (200,))
        self.assertEqual(r.get_json()['text'], 'bonjour le monde')

    def test_erreur_d_entree_du_sidecar_reste_400(self):
        """A recording too short is an INPUT ERROR: presenting it as an upstream failure made it
        look like a service failure."""
        class _R:
            ok = False
            status_code = 400

            def json(self):
                return {'detail': 'Enregistrement trop court.'}
        import asr_routes
        with patch.object(asr_routes.requests, 'post', return_value=_R()):
            r = self._envoie()
        self.assertEqual(r.status_code, 400)
        self.assertIn('trop court', r.get_json()['error'])

    def test_modele_non_charge_reste_503(self):
        class _R:
            ok = False
            status_code = 503

            def json(self):
                return {'detail': 'Modèle non chargé.'}
        import asr_routes
        with patch.object(asr_routes.requests, 'post', return_value=_R()):
            r = self._envoie()
        self.assertEqual(r.status_code, 503)

    def test_panne_du_sidecar_503(self):
        import requests as _rq
        import asr_routes
        with patch.object(asr_routes.requests, 'post',
                          side_effect=_rq.exceptions.ConnectionError()):
            r = self._envoie()
        self.assertEqual(r.status_code, 503)

    def test_timeout_repondu_en_503(self):
        import requests as _rq
        import asr_routes
        with patch.object(asr_routes.requests, 'post',
                          side_effect=_rq.exceptions.Timeout()):
            r = self._envoie()
        self.assertEqual(r.status_code, 503)

    def test_disponibilite(self):
        import asr_routes
        with patch.object(asr_routes, 'asr_is_up', return_value=False):
            self.assertFalse(self._client().get('/api/transcribe/available')
                             .get_json()['available'])

    def test_chargement_echoue_se_distingue_d_un_demarrage(self):
        """A dictation container running with no model loaded, and publishing WHY,
        is not « en cours de démarrage »: the admin displayed « Démarrage… »
        indefinitely (reported on 2026-10-01). With no published cause,
        however, nothing allows concluding a failure — this is the other
        sidecars' behaviour, which do not tell their errors."""
        import sidecars
        with patch.object(sidecars, '_sidecar_proc_status', return_value='running'), \
             patch.object(sidecars, 'asr_is_up', return_value=False), \
             patch.object(sidecars, 'asr_load_error',
                          return_value='CUDA error: out of memory'):
            self.assertEqual(sidecars._sidecar_status('asr'), 'failed')
        with patch.object(sidecars, '_sidecar_proc_status', return_value='running'), \
             patch.object(sidecars, 'asr_is_up', return_value=False), \
             patch.object(sidecars, 'asr_load_error', return_value=None):
            self.assertEqual(sidecars._sidecar_status('asr'), 'starting')


class JaugeDebitCableeTest(_BasePlayground):
    """Le câblage relay → jauge vivante (scan C, 2026-10-03). Sans cette
    assertion, si le relay cessait d'appeler _inflight_tokens /
    _ratio_dernier / _prefill_dernier, le « 0 tok/s » reviendrait
    SILENCIEUSEMENT : les tests de stats.* ne voient que les fonctions, pas
    celui qui les appelle."""

    def test_le_relais_alimente_compteur_ratio_et_prefill(self):
        amont = _FauxAmont([_delta('Bonjour '), _delta('le monde'),
                            _usage(n=12, entree=340), _fin(), b'data: [DONE]'])
        tokens, ratio, prefill = [], [], []
        with ExitStack() as stack:
            self._standard(stack, post=self._amont_unique(amont))
            stack.enter_context(patch.object(
                chat, '_inflight_tokens', side_effect=lambda *a: tokens.append(a)))
            stack.enter_context(patch.object(
                chat, '_ratio_dernier', side_effect=lambda *a: ratio.append(a)))
            stack.enter_context(patch.object(
                chat, '_prefill_dernier', side_effect=lambda *a: prefill.append(a)))
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=self._corps()):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                chat.playground_chat().get_data(as_text=True)
        # 1. le compteur vivant est alimenté, caractères croissants, avec le
        #    début de décodage (le dénominateur du débit)
        self.assertTrue(tokens, "le relay n'alimente plus la jauge — « 0 tok/s » silencieux")
        vus = [a[1] for a in tokens]
        self.assertEqual(vus, sorted(vus))
        self.assertTrue(any(a[2] for a in tokens), "le début de décodage n'est pas transmis")
        # 2. la calibration reçoit le compte EXACT de l'usage (16 car. émis, 12 tokens)
        self.assertEqual(ratio, [(16, 12)])
        # 3. le préfill reçoit prompt_tokens (340) et un TTFT mesuré
        self.assertEqual(len(prefill), 1)
        self.assertEqual(prefill[0][0], 340)
        self.assertGreater(prefill[0][1], 0)


if __name__ == '__main__':
    unittest.main()
