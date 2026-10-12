"""HTTP tests of the memory routes.

The tests of `test_memory.py` target the functions. These go through the
real Flask stack — authentication decorators, CSRF protection, JSON
serialization — because that is where the defects a function test cannot
see play out: a route forgotten without `@login_required`, an isolation
holding at SQL level but bypassed by a route reading a query parameter.
"""

import time
import unittest
from unittest import mock

import app as portal
import chat_routes as chat_routes
# Memory left the monolith for memory_routes.py (28/08, 1st blueprint):
# we target it in its own module.
import memory_routes as memory
from db import get_db


class MemoryApiBase(unittest.TestCase):
    USER = 'apitest-a'
    OTHER = 'apitest-b'

    def setUp(self):
        portal.app.config['TESTING'] = True
        self.client = portal.app.test_client()
        self._wipe()

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        with portal.app.test_request_context():
            db = portal.get_db()
            for u in (self.USER, self.OTHER):
                memory._mem_purge(u)
                db.execute("DELETE FROM user_prefs WHERE username=?", (u,))
            db.commit()

    def _login(self, username, client=None):
        """Opens a valid session and returns the associated CSRF token."""
        c = client or self.client
        with c.session_transaction() as sess:
            sess['username'] = username
            sess['fullname'] = username
            sess['is_admin'] = False
            sess['auth_at'] = time.time()
            sess['csrf'] = f'jeton-{username}'
        return f'jeton-{username}'

    def _enable(self, username, client=None):
        csrf = self._login(username, client)
        (client or self.client).post('/api/memory/enabled', json={'enabled': True},
                                     headers={'X-CSRFToken': csrf})
        return csrf


class AuthGateTest(MemoryApiBase):
    """No memory route must answer without a session."""

    def test_toutes_les_routes_sont_protegees(self):
        calls = [
            ('get', '/api/memory', None),
            ('post', '/api/memory/enabled', {'enabled': True}),
            ('post', '/api/memory/facts', {'subject': 'x', 'fact': 'y'}),
            ('delete', '/api/memory/facts/1', None),
            ('post', '/api/memory/purge', None),
        ]
        for methode, route, body in calls:
            # A consistent CSRF token: we really test AUTHENTICATION, not CSRF.
            with self.client.session_transaction() as sess:
                sess.clear()
                sess['csrf'] = 'jeton-anonyme'
            r = getattr(self.client, methode)(route, json=body,
                                              headers={'X-CSRFToken': 'jeton-anonyme'})
            self.assertIn(r.status_code, (302, 401, 403),
                          f"{methode.upper()} {route} -> {r.status_code}")


class CsrfTest(MemoryApiBase):
    """The writing routes must refuse a request without a valid token."""

    def test_ecritures_sans_jeton_refusees(self):
        self._login(self.USER)
        calls = [
            ('post', '/api/memory/enabled', {'enabled': True}),
            ('post', '/api/memory/facts', {'subject': 'x', 'fact': 'y'}),
            ('delete', '/api/memory/facts/1', None),
            ('post', '/api/memory/purge', None),
        ]
        for methode, route, body in calls:
            r = getattr(self.client, methode)(route, json=body)
            self.assertEqual(r.status_code, 400, f"{methode.upper()} {route}")

    def test_lecture_ne_demande_pas_de_jeton(self):
        self._login(self.USER)
        self.assertEqual(self.client.get('/api/memory').status_code, 200)


class OptInApiTest(MemoryApiBase):
    def test_activee_par_defaut(self):
        # 2026-09: default ON — a brand-new account has memory enabled, with
        # an empty graph.
        self._login(self.USER)
        d = self.client.get('/api/memory').get_json()
        self.assertTrue(d['enabled'])
        self.assertEqual(d['edges'], [])

    def test_activation_persiste(self):
        csrf = self._enable(self.USER)
        self.assertTrue(self.client.get('/api/memory').get_json()['enabled'])
        # A new session (new client) must find the setting back: it lives in
        # database, not in the cookie.
        autre = portal.app.test_client()
        self._login(self.USER, autre)
        self.assertTrue(autre.get('/api/memory').get_json()['enabled'])
        self.client.post('/api/memory/enabled', json={'enabled': False},
                         headers={'X-CSRFToken': csrf})
        self.assertFalse(self.client.get('/api/memory').get_json()['enabled'])

    def test_ajout_manuel_possible_meme_sans_opt_in(self):
        # The opt-in frames what the MODEL stores. A manual add by the user is
        # a deliberate act: blocking it would make no sense since they wrote it
        # themselves.
        csrf = self._login(self.USER)
        r = self.client.post('/api/memory/facts', json={'subject': 'vLLM', 'fact': 'À la main.'},
                             headers={'X-CSRFToken': csrf})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])


class IsolationApiTest(MemoryApiBase):
    """Two real accounts, two HTTP clients: nothing must cross."""

    def setUp(self):
        super().setUp()
        self.client_a = portal.app.test_client()
        self.client_b = portal.app.test_client()
        self.csrf_a = self._enable(self.USER, self.client_a)
        self.csrf_b = self._enable(self.OTHER, self.client_b)
        self.client_a.post('/api/memory/facts',
                           json={'subject': 'vLLM', 'fact': 'Secret de A.'},
                           headers={'X-CSRFToken': self.csrf_a})
        self.client_b.post('/api/memory/facts',
                           json={'subject': 'vLLM', 'fact': 'Secret de B.'},
                           headers={'X-CSRFToken': self.csrf_b})

    def _facts(self, client):
        return [e['fact'] for e in client.get('/api/memory').get_json()['edges']]

    def test_chacun_ne_voit_que_le_sien(self):
        self.assertEqual(self._facts(self.client_a), ['Secret de A.'])
        self.assertEqual(self._facts(self.client_b), ['Secret de B.'])

    def test_b_ne_peut_pas_supprimer_un_fait_de_a(self):
        id_a = self.client_a.get('/api/memory').get_json()['edges'][0]['id']
        r = self.client_b.delete(f'/api/memory/facts/{id_a}',
                                 headers={'X-CSRFToken': self.csrf_b})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._facts(self.client_a), ['Secret de A.'])

    def test_la_purge_de_b_epargne_a(self):
        self.client_b.post('/api/memory/purge', headers={'X-CSRFToken': self.csrf_b})
        self.assertEqual(self._facts(self.client_b), [])
        self.assertEqual(self._facts(self.client_a), ['Secret de A.'])

    def test_les_noeuds_ne_sont_pas_partages(self):
        # Same subject ("vLLM") on both sides: they must be TWO distinct nodes,
        # else the two graphs would be linked by that common node.
        a = self.client_a.get('/api/memory').get_json()
        b = self.client_b.get('/api/memory').get_json()
        self.assertEqual(len(a['nodes']), 1)
        self.assertEqual(len(b['nodes']), 1)
        self.assertNotEqual(a['nodes'][0]['id'], b['nodes'][0]['id'])


class ImportExportApiTest(MemoryApiBase):
    """Export (JSON/Markdown) and import (merge) of the logged-in account's memory."""

    def setUp(self):
        super().setUp()
        self.csrf = self._enable(self.USER)
        self.client.post('/api/memory/facts',
                         json={'subject': 'vLLM', 'fact': 'Tourne en 0.27.', 'object': 'DGX Spark'},
                         headers={'X-CSRFToken': self.csrf})

    def test_export_json_structure(self):
        r = self.client.get('/api/memory/export')
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r.headers.get('Content-Disposition', ''))
        doc = r.get_json()
        self.assertEqual(doc['schema'], 'dgx-memory')
        self.assertEqual(doc['username'], self.USER)
        self.assertEqual(len(doc['edges']), 1)
        self.assertEqual(doc['edges'][0]['subject'], 'vLLM')
        self.assertEqual(doc['edges'][0]['fact'], 'Tourne en 0.27.')
        # A fact with an object creates TWO nodes (subject + object).
        self.assertEqual(len(doc['nodes']), 2)
        self.assertEqual({n['name'] for n in doc['nodes']}, {'vLLM', 'DGX Spark'})

    def test_export_markdown_contient_le_fait(self):
        r = self.client.get('/api/memory/export.md')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/markdown', r.headers.get('Content-Type'))
        body = r.get_data(as_text=True)
        self.assertIn('vLLM', body)
        self.assertIn('Tourne en 0.27.', body)

    def test_import_roundtrip_fusion(self):
        doc = self.client.get('/api/memory/export').get_json()
        # Purge then reimport: we must find back exactly the same fact.
        self.client.post('/api/memory/purge', headers={'X-CSRFToken': self.csrf})
        self.assertEqual(self.client.get('/api/memory').get_json()['edges'], [])
        r = self.client.post('/api/memory/import', json=doc, headers={'X-CSRFToken': self.csrf})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        faits = [e['fact'] for e in self.client.get('/api/memory').get_json()['edges']]
        self.assertEqual(faits, ['Tourne en 0.27.'])

    def test_import_accepts_exports_made_before_the_rename(self):
        doc = self.client.get('/api/memory/export').get_json()
        doc['schema'] = 'dgx-memory'      # what older exports carry
        self.client.post('/api/memory/purge', headers={'X-CSRFToken': self.csrf})
        r = self.client.post('/api/memory/import', json=doc, headers={'X-CSRFToken': self.csrf})
        self.assertEqual(r.status_code, 200)
        faits = [e['fact'] for e in self.client.get('/api/memory').get_json()['edges']]
        self.assertEqual(faits, ['Tourne en 0.27.'])

    def test_import_duplique_ne_cree_pas_de_double(self):
        doc = self.client.get('/api/memory/export').get_json()
        self.client.post('/api/memory/import', json=doc, headers={'X-CSRFToken': self.csrf})
        faits = [e['fact'] for e in self.client.get('/api/memory').get_json()['edges']]
        self.assertEqual(faits, ['Tourne en 0.27.'])  # merge, no duplicate

    def test_import_json_invalide_refuse(self):
        r = self.client.post('/api/memory/import', data='pas du json',
                             headers={'X-CSRFToken': self.csrf})
        self.assertEqual(r.status_code, 400)

    def test_import_schema_inconnu_refuse(self):
        r = self.client.post('/api/memory/import', json={'schema': 'autre-chose'},
                             headers={'X-CSRFToken': self.csrf})
        self.assertEqual(r.status_code, 400)

    def test_import_ne_traverse_pas_les_comptes(self):
        # The doc's `username` field is ignored: the SESSION is authoritative.
        doc = self.client.get('/api/memory/export').get_json()
        doc['username'] = self.OTHER  # tried to usurp the destination
        r = self.client.post('/api/memory/import', json=doc, headers={'X-CSRFToken': self.csrf})
        self.assertEqual(r.status_code, 200)
        # The import landed on USER (session), and OTHER received nothing.
        self.assertEqual(
            len([e for e in self.client.get('/api/memory').get_json()['edges'] if e['fact'] == 'Tourne en 0.27.']), 1)
        autre = portal.app.test_client()
        self._login(self.OTHER, autre)
        autre.post('/api/memory/enabled', json={'enabled': True},
                   headers={'X-CSRFToken': f'jeton-{self.OTHER}'})
        self.assertEqual(autre.get('/api/memory').get_json()['edges'], [])

    def test_export_requiert_une_session(self):
        autre = portal.app.test_client()
        r = autre.get('/api/memory/export')
        self.assertIn(r.status_code, (302, 401, 403))


class ImportMarkdownTest(MemoryApiBase):
    """Markdown import: tolerant parser + round-trip with the .md export."""

    def setUp(self):
        super().setUp()
        self.csrf = self._enable(self.USER)

    def test_roundtrip_avec_notre_export_markdown(self):
        # A structured fact (with object) + our .md export, then purge + reimport.
        self.client.post('/api/memory/facts',
                         json={'subject': 'vLLM', 'fact': 'Tourne en 0.27.', 'object': 'DGX Spark'},
                         headers={'X-CSRFToken': self.csrf})
        md = self.client.get('/api/memory/export.md').get_data(as_text=True)
        self.client.post('/api/memory/purge', headers={'X-CSRFToken': self.csrf})
        r = self.client.post('/api/memory/import.md', data=md.encode('utf-8'),
                             headers={'X-CSRFToken': self.csrf,
                                      'Content-Type': 'text/plain; charset=utf-8'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()['ok'])
        faits = [e['fact'] for e in self.client.get('/api/memory').get_json()['edges']]
        self.assertIn('Tourne en 0.27.', faits)

    def test_markdown_generique_claude_chatgpt(self):
        # Free format: titles = subjects, bullets = facts (no bold structure).
        md = (
            "# Memory\n"
            "## Préférences\n"
            "- Aime les réponses courtes\n"
            "- Développe en Python\n"
            "## Outils\n"
            "- Utilise Docker au quotidien\n"
        )
        r = self.client.post('/api/memory/import.md', data=md.encode('utf-8'),
                             headers={'X-CSRFToken': self.csrf,
                                      'Content-Type': 'text/plain; charset=utf-8'})
        self.assertEqual(r.status_code, 200, r.get_json())
        faits = [e['fact'] for e in self.client.get('/api/memory').get_json()['edges']]
        self.assertIn('Aime les réponses courtes', faits)
        self.assertIn('Développe en Python', faits)
        self.assertIn('Utilise Docker au quotidien', faits)

    def test_fusion_dedup_sur_markdown(self):
        md = "## vLLM\n- Tourne en 0.27.\n"
        self.client.post('/api/memory/facts',
                         json={'subject': 'vLLM', 'fact': 'Tourne en 0.27.'},
                         headers={'X-CSRFToken': self.csrf})
        self.client.post('/api/memory/import.md', data=md.encode('utf-8'),
                         headers={'X-CSRFToken': self.csrf,
                                  'Content-Type': 'text/plain; charset=utf-8'})
        faits = [e['fact'] for e in self.client.get('/api/memory').get_json()['edges']]
        self.assertEqual(faits, ['Tourne en 0.27.'])  # no duplicate

    def test_fichier_sans_fait_refuse(self):
        r = self.client.post('/api/memory/import.md', data=b'# Rien ici\n\njuste du texte',
                             headers={'X-CSRFToken': self.csrf,
                                      'Content-Type': 'text/plain'})
        self.assertEqual(r.status_code, 400)

    def test_import_md_requiert_une_session(self):
        # Without session nor consistent CSRF token, writing is refused (400 by
        # the global CSRF guard, run before login_required).
        autre = portal.app.test_client()
        r = autre.post('/api/memory/import.md', data=b'## x\n- y',
                       headers={'Content-Type': 'text/plain'})
        self.assertIn(r.status_code, (302, 400, 401, 403))

    def test_parseur_relations_structurées(self):
        # Pattern « **relation** object: fact » recognized; outside bold → generic relation.
        md = "## vLLM\n- **sert avec** DGX Spark : Tourne en 0.27.\n"
        edges, aliases = memory._md_parse(md)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]['relation'], 'sert avec')
        self.assertEqual(edges[0]['object'], 'DGX Spark')
        self.assertEqual(edges[0]['fact'], 'Tourne en 0.27.')

    def test_format_claude_legacy(self):
        # Real legacy Claude export: bold-only sections + italic-only
        # sub-sections + prose paragraphs (one dense line, no bullets).
        md = (
            "**Work context**\n"
            "\n"
            "Alex works in IT infrastructure for a small company. "
            "He maintains a small lab at home with a few servers.\n"
            "\n"
            "**Brief history**\n"
            "\n"
            "*Recent months*\n"
            "\n"
            "His lab uses a themed naming convention for hosts. "
            "He runs a DGX Spark (NVIDIA GB10, ARM64, 128GB).\n"
        )
        edges, aliases = memory._md_parse(md)
        # 4 sentences → 4 facts, spread under Work context / Recent months.
        self.assertEqual(len(edges), 4)
        by_subject = {}
        for e in edges:
            by_subject.setdefault(e['subject'], []).append(e['fact'])
        self.assertIn('Work context', by_subject)
        self.assertIn('Recent months', by_subject)
        self.assertEqual(len(by_subject['Work context']), 2)
        self.assertEqual(len(by_subject['Recent months']), 2)
        # The inline bold (DGX Spark) is stripped of its markup.
        self.assertTrue(any('DGX Spark' in f for f in by_subject['Recent months']))
        self.assertTrue(all('**' not in f for f in by_subject['Recent months']))

    def test_import_complet_format_claude(self):
        # End-to-end import of a Claude excerpt: the facts must land.
        md = (
            "**Personal context**\n"
            "\n"
            "Alex communicates primarily in French. He prefers short answers.\n"
        )
        r = self.client.post('/api/memory/import.md', data=md.encode('utf-8'),
                             headers={'X-CSRFToken': self.csrf,
                                      'Content-Type': 'text/plain; charset=utf-8'})
        self.assertEqual(r.status_code, 200, r.get_json())
        all_facts = [e['fact'] for e in self.client.get('/api/memory').get_json()['edges']]
        self.assertTrue(any('communicates primarily in French' in f for f in all_facts))
        self.assertTrue(any('short answers' in f for f in all_facts))


class InjectionContexteTest(MemoryApiBase):
    """The memory block injected in the chat system (reading)."""

    def setUp(self):
        super().setUp()
        self.csrf = self._enable(self.USER)

    def test_vide_si_desactive(self):
        with portal.app.test_request_context():
            memory._mem_set_enabled(self.USER, False)
            self.assertEqual(memory._mem_inject_context(self.USER), '')

    def test_vide_si_aucun_fait(self):
        with portal.app.test_request_context():
            self.assertEqual(memory._mem_inject_context(self.USER), '')

    def test_contient_les_faits_actifs(self):
        with portal.app.test_request_context():
            memory._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
            bloc = memory._mem_inject_context(self.USER)
        self.assertIn('### Mémoire', bloc)
        self.assertIn('vLLM — Tourne en 0.27.', bloc)
        # Framed as data (anti persistent-prompt-injection).
        self.assertIn('données', bloc)

    def test_exclut_les_faits_perimes(self):
        with portal.app.test_request_context():
            memory._mem_add_fact(self.USER, 'vLLM', 'version', 'Ancienne version.')
            row = get_db().execute(
                "SELECT e.id FROM memory_edges e WHERE e.username=?",
                (self.USER,)).fetchone()
            get_db().execute("UPDATE memory_edges SET valid_until=? WHERE id=?",
                             ('2020-01-01T00:00:00', row['id']))
            get_db().commit()
            self.assertEqual(memory._mem_inject_context(self.USER), '')


class ExtractionPostTourTest(MemoryApiBase):
    """The post-round extraction thread writes memory OUTSIDE request context."""

    def test_ecrit_hors_contexte_requete(self):
        # The post-round thread has NO Flask request context: this is exactly
        # what broke the first version (get_db raised « Working outside of
        # application context » there, swallowed by a bare except).
        with portal.app.app_context():
            memory._mem_set_enabled(self.USER, True)
        excerpts = [{'role': 'user', 'content': "Je préfère les réponses courtes."},
                    {'role': 'assistant', 'content': "Bien noté."}]

        class _Reponse:
            ok = True
            status_code = 200

            def json(self):
                return {'choices': [{'message': {'content':
                    "préférences | style | Préfère les réponses courtes."}}]}

        with portal.app.app_context():
            with mock.patch('chat_routes.requests.post', return_value=_Reponse()):
                chat_routes._mem_extract_and_save(
                    self.USER, 'm', 'k', excerpts, portal.app)
            faits = [e['fact'] for e in memory._mem_graph(self.USER)['edges']]
        self.assertTrue(any('courtes' in f for f in faits))

    def test_desactive_n_appelle_meme_pas_le_modele(self):
        with portal.app.app_context():
            memory._mem_set_enabled(self.USER, False)
            with mock.patch('chat_routes.requests.post') as faux_post:
                chat_routes._mem_extract_and_save(self.USER, 'm', 'k', [], portal.app)
        faux_post.assert_not_called()


class EntreesHostilesTest(MemoryApiBase):

    def setUp(self):
        super().setUp()
        self.csrf = self._enable(self.USER)

    def _post(self, body):
        return self.client.post('/api/memory/facts', json=body,
                                headers={'X-CSRFToken': self.csrf})

    def test_champs_vides_refuses(self):
        for body in ({'subject': '', 'fact': 'x'},
                      {'subject': 'x', 'fact': ''},
                      {'subject': '   ', 'fact': '   '},
                      {}):
            self.assertEqual(self._post(body).status_code, 400, body)

    def test_sujet_sans_aucun_caractere_utile(self):
        # « !!! » normalizes to an empty string: the node would be keyless.
        self.assertEqual(self._post({'subject': '!!!', 'fact': 'x'}).status_code, 400)

    def test_texte_tres_long_tronque_sans_planter(self):
        r = self._post({'subject': 'A' * 5000, 'fact': 'B' * 5000})
        self.assertEqual(r.status_code, 200)
        d = self.client.get('/api/memory').get_json()
        self.assertLessEqual(len(d['edges'][0]['fact']), memory.MEM_MAX_FACT_LEN)
        self.assertLessEqual(len(d['nodes'][0]['name']), memory.MEM_MAX_NAME_LEN)

    def test_injection_sql_traitee_comme_du_texte(self):
        charge = "'; DROP TABLE memory_edges; --"
        self.assertEqual(self._post({'subject': charge, 'fact': charge}).status_code, 200)
        # The table still exists and the fact is there, as is.
        d = self.client.get('/api/memory').get_json()
        self.assertEqual(d['edges'][0]['fact'], charge)

    def test_emoji_et_unicode(self):
        self.assertEqual(self._post({'subject': '日本語', 'fact': 'Parle japonais 🎌'}).status_code, 200)
        self.assertEqual(self._post({'subject': 'Café', 'fact': 'Aime le café ☕'}).status_code, 200)
        self.assertEqual(len(self.client.get('/api/memory').get_json()['edges']), 2)

    def test_identifiant_de_fait_inexistant(self):
        r = self.client.delete('/api/memory/facts/999999', headers={'X-CSRFToken': self.csrf})
        self.assertEqual(r.status_code, 404)

    def test_identifiant_non_numerique(self):
        r = self.client.delete('/api/memory/facts/abc', headers={'X-CSRFToken': self.csrf})
        self.assertEqual(r.status_code, 404)   # the route expects an <int:>

    def test_corps_json_absent(self):
        r = self.client.post('/api/memory/facts', headers={'X-CSRFToken': self.csrf})
        self.assertEqual(r.status_code, 400)

    def test_kind_invalide_retombe_sur_le_defaut(self):
        self.assertEqual(self._post({'subject': 'X', 'fact': 'y', 'kind': 'n_importe_quoi'}).status_code, 200)
        self.assertEqual(self.client.get('/api/memory').get_json()['nodes'][0]['kind'], 'subject')


class ParcoursTest(MemoryApiBase):
    """The graph walk must bring back the neighbourhood — no less, no all."""

    def setUp(self):
        super().setUp()
        self.ctx = portal.app.test_request_context()
        self.ctx.push()
        memory._mem_set_enabled(self.USER, True)

    def tearDown(self):
        self.ctx.pop()
        super().tearDown()

    def test_deux_sauts(self):
        # Portail —> vLLM —> CUDA: from Portail, 1 jump sees vLLM, 2 jumps CUDA.
        memory._mem_add_fact(self.USER, 'Portail', 'sert avec', 'Portail sert avec vLLM.', obj='vLLM')
        memory._mem_add_fact(self.USER, 'vLLM', 'repose sur', 'vLLM repose sur CUDA.', obj='CUDA')
        un = {f['fact'] for f in memory._mem_recall(self.USER, 'Portail', hops=1)}
        deux = {f['fact'] for f in memory._mem_recall(self.USER, 'Portail', hops=2)}
        self.assertIn('Portail sert avec vLLM.', un)
        self.assertIn('vLLM repose sur CUDA.', deux)
        self.assertTrue(deux.issuperset(un))

    def test_composante_non_reliee_exclue(self):
        memory._mem_add_fact(self.USER, 'Portail', 'sert avec', 'Portail sert avec vLLM.', obj='vLLM')
        memory._mem_add_fact(self.USER, 'Cuisine', 'aime', 'Aime le curry.')
        faits = {f['fact'] for f in memory._mem_recall(self.USER, 'Portail', hops=2)}
        self.assertNotIn('Aime le curry.', faits)

    def test_cycle_ne_boucle_pas(self):
        # A—B, B—C, C—A: the recursive CTE must stop (UNION deduplicates).
        memory._mem_add_fact(self.USER, 'A', 'lie', 'A vers B.', obj='B')
        memory._mem_add_fact(self.USER, 'B', 'lie', 'B vers C.', obj='C')
        memory._mem_add_fact(self.USER, 'C', 'lie', 'C vers A.', obj='A')
        faits = memory._mem_recall(self.USER, 'A', hops=2)
        self.assertEqual(len(faits), 3)

    def test_nombre_de_faits_borne(self):
        for i in range(40):
            memory._mem_add_fact(self.USER, 'Gros', f'note{i}', f'Fait {i}')
        self.assertLessEqual(len(memory._mem_recall(self.USER, 'Gros')), 25)

    def test_hops_hors_bornes_ramene_dans_la_plage(self):
        memory._mem_add_fact(self.USER, 'A', 'lie', 'A vers B.', obj='B')
        for mauvais in (0, -5, 99, None):
            self.assertTrue(memory._mem_recall(self.USER, 'A', hops=mauvais))


class AliasTest(MemoryApiBase):
    """An alias must lead back to the existing node, not create a second one."""

    def setUp(self):
        super().setUp()
        self.ctx = portal.app.test_request_context()
        self.ctx.push()
        memory._mem_set_enabled(self.USER, True)

    def tearDown(self):
        self.ctx.pop()
        super().tearDown()

    def test_alias_rattache_au_meme_noeud(self):
        memory._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
        noeud = memory._mem_node(self.USER, 'vLLM', create=False)
        db = portal.get_db()
        db.execute("INSERT INTO memory_aliases (node_id, username, alias_norm) VALUES (?,?,?)",
                   (noeud['id'], self.USER, memory._mem_norm("le serveur d'inférence")))
        db.commit()
        retrouve = memory._mem_node(self.USER, "Le serveur d'inférence", create=False)
        self.assertIsNotNone(retrouve)
        self.assertEqual(retrouve['id'], noeud['id'])
        self.assertTrue(memory._mem_recall(self.USER, "le serveur d'inférence"))

    def test_alias_cloisonne_par_utilisateur(self):
        memory._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
        noeud = memory._mem_node(self.USER, 'vLLM', create=False)
        db = portal.get_db()
        db.execute("INSERT INTO memory_aliases (node_id, username, alias_norm) VALUES (?,?,?)",
                   (noeud['id'], self.USER, memory._mem_norm('le moteur')))
        db.commit()
        self.assertIsNone(memory._mem_node(self.OTHER, 'le moteur', create=False))


class OutilsTest(MemoryApiBase):
    """The tools exposed to the model: contract and guardrails."""

    def setUp(self):
        super().setUp()
        self.ctx = portal.app.test_request_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        super().tearDown()

    def test_schemas_bien_formes(self):
        for outil in memory._mem_tools():
            self.assertEqual(outil['type'], 'function')
            fn = outil['function']
            self.assertTrue(fn['name'] and fn['description'])
            self.assertEqual(fn['parameters']['type'], 'object')
            for champ in fn['parameters'].get('required', []):
                self.assertIn(champ, fn['parameters']['properties'], fn['name'])

    def test_noms_uniques(self):
        names = [o['function']['name'] for o in memory._mem_tools()]
        self.assertEqual(len(names), len(set(names)))

    def test_outil_inconnu_refuse(self):
        memory._mem_set_enabled(self.USER, True)
        _, ok = memory._exec_memory_tool('drop_everything', {}, self.USER)
        self.assertFalse(ok)

    def test_le_modele_ne_choisit_pas_pour_qui(self):
        # A « username » argument injected in the tool call must have no
        # effect: the target comes from the session.
        memory._mem_set_enabled(self.USER, True)
        memory._exec_memory_tool(
            'save_memory',
            {'subject': 'vLLM', 'fact': 'Écrit par le modèle.', 'username': self.OTHER},
            self.USER)
        self.assertEqual(len(memory._mem_graph(self.USER)['edges']), 1)
        self.assertEqual(memory._mem_graph(self.OTHER)['edges'], [])

    def test_rappel_sans_opt_in_ne_lit_rien(self):
        memory._mem_set_enabled(self.USER, True)
        memory._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
        memory._mem_set_enabled(self.USER, False)
        msg, ok = memory._exec_memory_tool('recall_memory', {'subject': 'vLLM'}, self.USER)
        self.assertFalse(ok)
        self.assertNotIn('0.27', msg)


if __name__ == '__main__':
    unittest.main()
