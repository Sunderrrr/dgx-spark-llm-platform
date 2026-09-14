"""Le PLAYGROUND de bout en bout : `/playground/chat` et ses dépendances.

Pourquoi ce fichier existe : la route la plus utilisée du portail n'était
couverte par AUCUN test. `tests/test_app.py` verrouillait le Support, le partage,
le titre et le résumé, mais jamais le flux du playground — donc jamais le contrat
SSE, le bornage des réglages, le repli sur `reasoning_effort`, la notice de quota,
la fin de flux sans `finish_reason`, ni l'abandon du client. C'est exactement là
que se sont accumulés les défauts corrigés le 2026-09-14 (battement manquant
pendant la lecture de pages, prompt d'outils non borné, TTFT faux, `[DONE]`
absent, budget de contexte calculé sur la fenêtre au lieu de l'entrée).

Aucun modèle n'est appelé : l'amont LiteLLM est remplacé par une réponse SSE
factice et pilotable (`_FauxAmont`). Le fil de lecture du portail, lui, tourne
pour de vrai — c'est lui qui porte les défauts qu'on veut interdire.
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
    """Une trame de contenu, telle que LiteLLM la produit."""
    return ("data: " + json.dumps({'choices': [{'delta': {'content': texte}}]})).encode()


def _fin(reason='stop'):
    return ("data: " + json.dumps({'choices': [{'delta': {},
                                                'finish_reason': reason}]})).encode()


def _usage(n=42, entree=17):
    return ("data: " + json.dumps({'choices': [{'delta': {}}],
                                   'usage': {'completion_tokens': n,
                                             'prompt_tokens': entree}})).encode()


class _FauxAmont:
    """Réponse SSE amont factice.

    `iter_lines` rend des OCTETS : le portail les décode lui-même
    (`line.decode`), donc rendre des `str` ferait lever le générateur — c'est un
    piège réel, pas une commodité de test.
    """

    def __init__(self, lignes=None, statut=200, corps_json=None, bloquant=None):
        self.statut = statut
        self.lignes = list(lignes or [])
        self.closed = False
        self.corps_json = corps_json or {}
        # Événement relâché par close() : sert à prouver qu'un client qui part
        # fait bien fermer la connexion amont (et donc libérer le slot du moteur).
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
    """Patches communs : aucun appel réseau, aucun modèle, aucune mémoire."""

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
        """Exécute `/playground/chat` et rend le corps SSE complet (texte)."""
        with ExitStack() as stack:
            self._standard(stack, post=post, **kw)
            with portal.app.test_request_context('/playground/chat', method='POST',
                                                 json=corps):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                resp = chat.playground_chat()
                return resp.get_data(as_text=True)

    def _db(self):
        """Contexte applicatif : `get_db` range sa connexion dans `g`."""
        return portal.app.app_context()

    def _client(self, username='demo'):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['username'] = username
            s['auth_at'] = int(time.time())
            s['csrf'] = self.CSRF
        return c

    def _amont_unique(self, amont, vus=None, erreur=None):
        """Un `requests.post` factice qui enregistre le corps envoyé."""
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


# ── 1. Le contrat SSE ────────────────────────────────────────────────────────

class FluxSSETest(_BasePlayground):
    """Ce que le client reçoit, dans l'ordre, et ce qu'il ne reçoit jamais."""

    def test_ouverture_avant_tout_travail(self):
        """En WSGI les en-têtes ne partent qu'au premier yield : sans ce
        commentaire, le proxy du frontend coupe sur un 502 avant que la
        génération ne commence."""
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
        self.assertEqual(envoye['messages'][0]['content'], 'Tu es bref.')
        self.assertEqual(envoye['messages'][1]['content'], 'Salut')
        self.assertTrue(envoye['stream'])
        self.assertEqual(envoye['stream_options'], {'include_usage': True})

    def test_la_cle_de_l_utilisateur_est_utilisee(self):
        """Le playground consomme le BUDGET : jamais la clé master."""
        vus = []
        amont = _FauxAmont([_fin()])
        self._flux(self._corps(), post=self._amont_unique(amont, vus))
        self.assertEqual(vus[0]['headers']['Authorization'], 'Bearer sk-user-123')

    def test_fin_sans_finish_reason_dit_length_et_termine(self):
        """Flux amont fermé en plein mot : le portail fabrique un
        `finish_reason: length` — et doit AUSSI envoyer `[DONE]`. Sans la
        sentinelle, tout client compatible OpenAI attend indéfiniment."""
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
        self.assertIn('model_error', corps)
        self.assertIn('500', corps)

    def test_timeout_de_lecture_est_une_erreur_de_transport(self):
        """LiteLLM injoignable n'est PAS une erreur de modèle : la réponse doit
        le dire, pas annoncer un code 0."""
        import requests as _rq
        corps = self._flux(self._corps(),
                           post=self._amont_unique(None, erreur=_rq.exceptions.ConnectTimeout()))
        self.assertIn('stream interrupted', corps.lower())
        self.assertNotIn('erreur (0)', corps)


# ── 2. Réglages : bornage et raisonnement ────────────────────────────────────

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
        self.assertEqual(len(vus[0]['json']['messages'][0]['content']), 4000)

    def test_max_tokens_rabaisse_a_la_fenetre_restante(self):
        """Le plafond de sortie S'AJOUTE au prompt : au-delà, le moteur refuse
        la requête au lieu de répondre plus court."""
        vus = []
        gros = 'a' * 30000          # ~10 000 tokens à 3 caractères/token
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
        """`effective_ctx` (262 144 mesurés) décrit la FENÊTRE ; la limite
        d'entrée est plus basse (196 608 annoncés par ctx_split). Borner sur la
        fenêtre laissait partir des prompts que le moteur refusait en 400."""
        vus = []
        msgs = [{'role': 'user', 'content': 'vieux ' + 'a' * 20000},
                {'role': 'assistant', 'content': 'b' * 20000},
                {'role': 'user', 'content': 'récent'}]
        self._flux(self._corps(msgs), post=self._amont_unique(_FauxAmont([_fin()]), vus),
                   entree=14000)          # budget = (14000-8192)x3 = 17 424 car.
        envoyes = vus[0]['json']['messages']
        # Le dernier échange est TOUJOURS conservé ; l'ancien est écarté entier.
        self.assertEqual(envoyes[-1]['content'], 'récent')
        self.assertNotIn('vieux ', ' '.join(m['content'] for m in envoyes))

    def test_input_limit_utilise_ctx_split(self):
        """La limite d'entrée vient de `ctx_split` (la même source que LiteLLM)."""
        with self._db():
            db = portal.get_db()
            db.execute("DELETE FROM model_configs WHERE name='ctx-test'")
            db.execute("INSERT INTO model_configs (name, vllm_args, engine, "
                       "hf_model_id, added_at) VALUES (?,?,?,?,?)",
                       ('ctx-test', '--ctx-size 1048576 --parallel 4 --n-predict 65536',
                        'llamacpp', 'test/ctx-test', '2026-09-14T00:00:00'))
            db.commit()
            # 1048576 / 4 slots - 65536 de marge de sortie = 196608, exactement
            # ce que LiteLLM annonce en production.
            self.assertEqual(chat._playground_input_limit('ctx-test'), 196608)
            # Et la FENÊTRE vaut le contexte par slot : c'est cet écart
            # (262 144 contre 196 608) qui faisait borner l'historique trop haut.
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
        """Le modèle servi REFUSE 'high' (500 Jinja mesuré) : l'accepter ne
        pouvait produire qu'un aller-retour perdu, payé deux fois en
        préchargement."""
        vus = []
        self._flux(self._corps(reasoning=True, reasoning_effort='high'),
                   post=self._amont_unique(_FauxAmont([_fin()]), vus))
        self.assertEqual(vus[0]['json']['chat_template_kwargs'], {'enable_thinking': True})
        self.assertEqual(len(vus), 1, "un second appel ne doit pas être tenté")

    def test_effort_refuse_par_le_modele_est_retente_sans(self):
        """Un 500 sur la valeur d'effort est un refus de gabarit, pas une panne :
        on retente une fois SANS effort plutôt que de tuer le tour."""
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


# ── 3. Gardes d'entrée ───────────────────────────────────────────────────────

class GardesTest(_BasePlayground):

    def test_message_vide(self):
        corps = self._flux({'messages': []})
        self.assertIn('Empty message.', corps)

    def test_roles_inconnus_ignores(self):
        corps = self._flux({'messages': [{'role': 'tool', 'content': 'x'}]})
        self.assertIn('Empty message.', corps)

    def test_aucun_modele(self):
        corps = self._flux(self._corps(), running=())
        self.assertIn('No model is currently running.', corps)

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
        self.assertIn('7s', corps)

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
        self.assertIn(b'Empty message.', r.data)


# ── 4. Abandon du client et métriques ────────────────────────────────────────

class MetriquesEtAbandonTest(_BasePlayground):

    def test_ttft_inclut_la_phase_outils(self):
        """Le TTFT publié doit être le délai RÉELLEMENT subi : il partait après
        la recherche, donc une demande qui passait 40 s à chercher annonçait
        « TTFT 1,2 s »."""
        mesures = []

        def _phase(*a, **k):
            time.sleep(0.35)
            return
            yield                       # générateur vide

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
        """Onglet fermé / Arrêter : le fil de lecture doit sortir de son `with`,
        donc fermer la connexion amont et libérer le slot du moteur."""
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
                next(it)                      # premier delta relayé
                it.close()                    # le client part
                amont.debloque.set()          # le faux amont rend la main
                for _ in range(50):
                    if amont.closed:
                        break
                    time.sleep(0.05)
        self.assertTrue(amont.closed, "la connexion amont n'a pas été fermée")

    def test_le_slot_en_vol_est_libere(self):
        """`inflight_requests` ne doit garder aucune ligne après un abandon."""
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


# ── 5. Phase outils : battement, bornes, erreurs visibles ────────────────────

class PhaseOutilsTest(_BasePlayground):

    def test_battement_pendant_un_outil_long(self):
        """`lire_pages` peut bloquer 90 s sans rendre la main ; le proxy du
        frontend coupe à 60 s SANS OCTET. Le résultat était donc perdu et
        l'utilisateur voyait « Erreur réseau » alors que la lecture aboutissait."""
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
        """`OUTILS_TOTAL_MAX` ne borne que la conversation de DÉPART : chaque tour
        empilait ensuite jusqu'à 4 résultats de 20 000 caractères, repostés en
        entier au tour suivant (~88 000 caractères au 2e tour)."""
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
        # Aucun message `tool` orphelin : un appel sans réponse casse le gabarit.
        ids_appeles = {tc['id'] for m in court if m.get('tool_calls')
                       for tc in m['tool_calls']}
        ids_repondus = {m.get('tool_call_id') for m in court if m.get('role') == 'tool'}
        self.assertEqual(ids_repondus - ids_appeles, set())

    def test_decision_d_outil_injoignable_est_dite(self):
        """Une panne de transport de la phase outils était AVALÉE : l'utilisateur
        avait demandé une recherche et recevait une réponse d'apparence normale,
        sans un mot."""
        import requests as _rq
        amont = _FauxAmont([_delta('Bonjour'), _fin()])
        appels = {'n': 0}

        def _post(url, headers=None, json=None, stream=False, timeout=None):
            appels['n'] += 1
            if appels['n'] == 1:            # l'appel de DÉCISION d'outil
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
            if appels['n'] == 1:            # décision : le modèle demande un outil
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


# ── 6. Données du playground ─────────────────────────────────────────────────

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


# ── 7. Conversations : bornes, isolation, drapeaux ───────────────────────────

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
        """30 conversations pleines font ~60 Mo de JSON chargés à chaque
        ouverture du playground : au-delà du budget, la liste porte
        `messages_omis` et la conversation se demande à l'unité."""
        gros = 'a' * 400000
        for i in range(6):
            self._enregistre(f'g{i}', [{'role': 'user', 'content': gros + str(i)}])
        with patch.object(conv_routes, 'LISTE_MAX_OCTETS', 1_000_000):
            convs = self._client().get('/api/conversations').get_json()['conversations']
        omis = [c for c in convs if c.get('messages_omis')]
        complets = [c for c in convs if not c.get('messages_omis')]
        self.assertTrue(omis, 'aucune conversation omise : la borne ne joue pas')
        self.assertTrue(complets)
        # La première est TOUJOURS entière : on n'ouvre pas sur un fil vide.
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


# ── 8. Feedback du Support : ne rien inventer ────────────────────────────────

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
        """`int(vote or 0) > 0` faisait d'un corps VIDE un avis négatif : la
        route inventait « pas utile » à partir d'une absence."""
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


# ── 9. Dictée : le code du sidecar n'est plus écrasé ─────────────────────────

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
        with patch.object(chat.requests, 'post', return_value=_R()) as post:
            import asr_routes
            with patch.object(asr_routes.requests, 'post', return_value=_R()) as p2:
                r = self._envoie()
        self.assertIn(r.status_code, (200,))
        self.assertEqual(r.get_json()['text'], 'bonjour le monde')

    def test_erreur_d_entree_du_sidecar_reste_400(self):
        """Un enregistrement trop court est une ERREUR D'ENTRÉE : la présenter
        en 502 faisait croire à une panne de service."""
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

    def test_panne_du_sidecar_502(self):
        import requests as _rq
        import asr_routes
        with patch.object(asr_routes.requests, 'post',
                          side_effect=_rq.exceptions.ConnectionError()):
            r = self._envoie()
        self.assertEqual(r.status_code, 502)

    def test_timeout_504(self):
        import requests as _rq
        import asr_routes
        with patch.object(asr_routes.requests, 'post',
                          side_effect=_rq.exceptions.Timeout()):
            r = self._envoie()
        self.assertEqual(r.status_code, 504)

    def test_disponibilite(self):
        import asr_routes
        with patch.object(asr_routes, 'asr_is_up', return_value=False):
            self.assertFalse(self._client().get('/api/transcribe/available')
                             .get_json()['available'])


if __name__ == '__main__':
    unittest.main()
