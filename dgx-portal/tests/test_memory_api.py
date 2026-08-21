"""Tests HTTP des routes de mémoire.

Les tests de `test_memory.py` portent sur les fonctions. Ceux-ci passent par la
vraie pile Flask — décorateurs d'authentification, protection CSRF, sérialisation
JSON — parce que c'est là que se jouent les défauts qu'un test de fonction ne
voit pas : une route oubliée sans `@login_required`, une isolation qui tient au
niveau SQL mais qu'une route contourne en lisant un paramètre de requête.
"""

import time
import unittest

import app as portal


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
                portal._mem_purge(u)
                db.execute("DELETE FROM user_prefs WHERE username=?", (u,))
            db.commit()

    def _login(self, username, client=None):
        """Ouvre une session valide et renvoie le jeton CSRF associé."""
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
    """Aucune route de mémoire ne doit répondre sans session."""

    def test_toutes_les_routes_sont_protegees(self):
        appels = [
            ('get', '/api/memory', None),
            ('post', '/api/memory/enabled', {'enabled': True}),
            ('post', '/api/memory/facts', {'subject': 'x', 'fact': 'y'}),
            ('delete', '/api/memory/facts/1', None),
            ('post', '/api/memory/purge', None),
        ]
        for methode, route, corps in appels:
            # Un jeton CSRF cohérent : on teste bien l'AUTHENTIFICATION, pas le CSRF.
            with self.client.session_transaction() as sess:
                sess.clear()
                sess['csrf'] = 'jeton-anonyme'
            r = getattr(self.client, methode)(route, json=corps,
                                              headers={'X-CSRFToken': 'jeton-anonyme'})
            self.assertIn(r.status_code, (302, 401, 403),
                          f"{methode.upper()} {route} -> {r.status_code}")


class CsrfTest(MemoryApiBase):
    """Les routes qui écrivent doivent refuser une requête sans jeton valide."""

    def test_ecritures_sans_jeton_refusees(self):
        self._login(self.USER)
        appels = [
            ('post', '/api/memory/enabled', {'enabled': True}),
            ('post', '/api/memory/facts', {'subject': 'x', 'fact': 'y'}),
            ('delete', '/api/memory/facts/1', None),
            ('post', '/api/memory/purge', None),
        ]
        for methode, route, corps in appels:
            r = getattr(self.client, methode)(route, json=corps)
            self.assertEqual(r.status_code, 400, f"{methode.upper()} {route}")

    def test_lecture_ne_demande_pas_de_jeton(self):
        self._login(self.USER)
        self.assertEqual(self.client.get('/api/memory').status_code, 200)


class OptInApiTest(MemoryApiBase):
    def test_desactivee_par_defaut(self):
        self._login(self.USER)
        d = self.client.get('/api/memory').get_json()
        self.assertFalse(d['enabled'])
        self.assertEqual(d['edges'], [])

    def test_activation_persiste(self):
        csrf = self._enable(self.USER)
        self.assertTrue(self.client.get('/api/memory').get_json()['enabled'])
        # Une nouvelle session (nouveau client) doit retrouver le réglage : il
        # vit en base, pas dans le cookie.
        autre = portal.app.test_client()
        self._login(self.USER, autre)
        self.assertTrue(autre.get('/api/memory').get_json()['enabled'])
        self.client.post('/api/memory/enabled', json={'enabled': False},
                         headers={'X-CSRFToken': csrf})
        self.assertFalse(self.client.get('/api/memory').get_json()['enabled'])

    def test_ajout_manuel_possible_meme_sans_opt_in(self):
        # L'opt-in encadre ce que le MODÈLE enregistre. Un ajout fait à la main
        # par l'utilisateur est un acte volontaire : le bloquer n'aurait aucun
        # sens puisqu'il l'a écrit lui-même.
        csrf = self._login(self.USER)
        r = self.client.post('/api/memory/facts', json={'subject': 'vLLM', 'fact': 'À la main.'},
                             headers={'X-CSRFToken': csrf})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])


class IsolationApiTest(MemoryApiBase):
    """Deux comptes réels, deux clients HTTP : rien ne doit traverser."""

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
        # Même sujet ("vLLM") des deux côtés : ce doit être DEUX nœuds distincts,
        # sinon les deux graphes seraient reliés par ce nœud commun.
        a = self.client_a.get('/api/memory').get_json()
        b = self.client_b.get('/api/memory').get_json()
        self.assertEqual(len(a['nodes']), 1)
        self.assertEqual(len(b['nodes']), 1)
        self.assertNotEqual(a['nodes'][0]['id'], b['nodes'][0]['id'])


class EntreesHostilesTest(MemoryApiBase):
    """Entrées limites et malveillantes : rien ne doit planter ni déborder."""

    def setUp(self):
        super().setUp()
        self.csrf = self._enable(self.USER)

    def _post(self, corps):
        return self.client.post('/api/memory/facts', json=corps,
                                headers={'X-CSRFToken': self.csrf})

    def test_champs_vides_refuses(self):
        for corps in ({'subject': '', 'fact': 'x'},
                      {'subject': 'x', 'fact': ''},
                      {'subject': '   ', 'fact': '   '},
                      {}):
            self.assertEqual(self._post(corps).status_code, 400, corps)

    def test_sujet_sans_aucun_caractere_utile(self):
        # « !!! » se normalise en chaîne vide : le nœud serait sans clé.
        self.assertEqual(self._post({'subject': '!!!', 'fact': 'x'}).status_code, 400)

    def test_texte_tres_long_tronque_sans_planter(self):
        r = self._post({'subject': 'A' * 5000, 'fact': 'B' * 5000})
        self.assertEqual(r.status_code, 200)
        d = self.client.get('/api/memory').get_json()
        self.assertLessEqual(len(d['edges'][0]['fact']), portal.MEM_MAX_FACT_LEN)
        self.assertLessEqual(len(d['nodes'][0]['name']), portal.MEM_MAX_NAME_LEN)

    def test_injection_sql_traitee_comme_du_texte(self):
        charge = "'; DROP TABLE memory_edges; --"
        self.assertEqual(self._post({'subject': charge, 'fact': charge}).status_code, 200)
        # La table existe toujours et le fait est là, tel quel.
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
        self.assertEqual(r.status_code, 404)   # la route attend un <int:>

    def test_corps_json_absent(self):
        r = self.client.post('/api/memory/facts', headers={'X-CSRFToken': self.csrf})
        self.assertEqual(r.status_code, 400)

    def test_kind_invalide_retombe_sur_le_defaut(self):
        self.assertEqual(self._post({'subject': 'X', 'fact': 'y', 'kind': 'n_importe_quoi'}).status_code, 200)
        self.assertEqual(self.client.get('/api/memory').get_json()['nodes'][0]['kind'], 'sujet')


class ParcoursTest(MemoryApiBase):
    """Le parcours du graphe doit ramener le voisinage — ni moins, ni tout."""

    def setUp(self):
        super().setUp()
        self.ctx = portal.app.test_request_context()
        self.ctx.push()
        portal._mem_set_enabled(self.USER, True)

    def tearDown(self):
        self.ctx.pop()
        super().tearDown()

    def test_deux_sauts(self):
        # Cronos —> vLLM —> CUDA : depuis Cronos, 1 saut voit vLLM, 2 sauts CUDA.
        portal._mem_add_fact(self.USER, 'Cronos', 'sert avec', 'Cronos sert avec vLLM.', obj='vLLM')
        portal._mem_add_fact(self.USER, 'vLLM', 'repose sur', 'vLLM repose sur CUDA.', obj='CUDA')
        un = {f['fact'] for f in portal._mem_recall(self.USER, 'Cronos', hops=1)}
        deux = {f['fact'] for f in portal._mem_recall(self.USER, 'Cronos', hops=2)}
        self.assertIn('Cronos sert avec vLLM.', un)
        self.assertIn('vLLM repose sur CUDA.', deux)
        self.assertTrue(deux.issuperset(un))

    def test_composante_non_reliee_exclue(self):
        portal._mem_add_fact(self.USER, 'Cronos', 'sert avec', 'Cronos sert avec vLLM.', obj='vLLM')
        portal._mem_add_fact(self.USER, 'Cuisine', 'aime', 'Aime le curry.')
        faits = {f['fact'] for f in portal._mem_recall(self.USER, 'Cronos', hops=2)}
        self.assertNotIn('Aime le curry.', faits)

    def test_cycle_ne_boucle_pas(self):
        # A—B, B—C, C—A : la CTE récursive doit s'arrêter (UNION dédoublonne).
        portal._mem_add_fact(self.USER, 'A', 'lie', 'A vers B.', obj='B')
        portal._mem_add_fact(self.USER, 'B', 'lie', 'B vers C.', obj='C')
        portal._mem_add_fact(self.USER, 'C', 'lie', 'C vers A.', obj='A')
        faits = portal._mem_recall(self.USER, 'A', hops=2)
        self.assertEqual(len(faits), 3)

    def test_nombre_de_faits_borne(self):
        for i in range(40):
            portal._mem_add_fact(self.USER, 'Gros', f'note{i}', f'Fait {i}')
        self.assertLessEqual(len(portal._mem_recall(self.USER, 'Gros')), 25)

    def test_hops_hors_bornes_ramene_dans_la_plage(self):
        portal._mem_add_fact(self.USER, 'A', 'lie', 'A vers B.', obj='B')
        for mauvais in (0, -5, 99, None):
            self.assertTrue(portal._mem_recall(self.USER, 'A', hops=mauvais))


class AliasTest(MemoryApiBase):
    """Un alias doit ramener sur le nœud existant, pas en créer un second."""

    def setUp(self):
        super().setUp()
        self.ctx = portal.app.test_request_context()
        self.ctx.push()
        portal._mem_set_enabled(self.USER, True)

    def tearDown(self):
        self.ctx.pop()
        super().tearDown()

    def test_alias_rattache_au_meme_noeud(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
        noeud = portal._mem_node(self.USER, 'vLLM', create=False)
        db = portal.get_db()
        db.execute("INSERT INTO memory_aliases (node_id, username, alias_norm) VALUES (?,?,?)",
                   (noeud['id'], self.USER, portal._mem_norm("le serveur d'inférence")))
        db.commit()
        retrouve = portal._mem_node(self.USER, "Le serveur d'inférence", create=False)
        self.assertIsNotNone(retrouve)
        self.assertEqual(retrouve['id'], noeud['id'])
        self.assertTrue(portal._mem_recall(self.USER, "le serveur d'inférence"))

    def test_alias_cloisonne_par_utilisateur(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
        noeud = portal._mem_node(self.USER, 'vLLM', create=False)
        db = portal.get_db()
        db.execute("INSERT INTO memory_aliases (node_id, username, alias_norm) VALUES (?,?,?)",
                   (noeud['id'], self.USER, portal._mem_norm('le moteur')))
        db.commit()
        self.assertIsNone(portal._mem_node(self.OTHER, 'le moteur', create=False))


class OutilsTest(MemoryApiBase):
    """Les outils exposés au modèle : contrat et garde-fous."""

    def setUp(self):
        super().setUp()
        self.ctx = portal.app.test_request_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        super().tearDown()

    def test_schemas_bien_formes(self):
        for outil in portal._mem_tools():
            self.assertEqual(outil['type'], 'function')
            fn = outil['function']
            self.assertTrue(fn['name'] and fn['description'])
            self.assertEqual(fn['parameters']['type'], 'object')
            for champ in fn['parameters'].get('required', []):
                self.assertIn(champ, fn['parameters']['properties'], fn['name'])

    def test_noms_uniques(self):
        noms = [o['function']['name'] for o in portal._mem_tools()]
        self.assertEqual(len(noms), len(set(noms)))

    def test_outil_inconnu_refuse(self):
        portal._mem_set_enabled(self.USER, True)
        _, ok = portal._exec_memory_tool('drop_everything', {}, self.USER)
        self.assertFalse(ok)

    def test_le_modele_ne_choisit_pas_pour_qui(self):
        # Un argument « username » injecté dans l'appel d'outil ne doit avoir
        # aucun effet : la cible vient de la session.
        portal._mem_set_enabled(self.USER, True)
        portal._exec_memory_tool(
            'save_memory',
            {'subject': 'vLLM', 'fact': 'Écrit par le modèle.', 'username': self.OTHER},
            self.USER)
        self.assertEqual(len(portal._mem_graph(self.USER)['edges']), 1)
        self.assertEqual(portal._mem_graph(self.OTHER)['edges'], [])

    def test_rappel_sans_opt_in_ne_lit_rien(self):
        portal._mem_set_enabled(self.USER, True)
        portal._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
        portal._mem_set_enabled(self.USER, False)
        msg, ok = portal._exec_memory_tool('recall_memory', {'subject': 'vLLM'}, self.USER)
        self.assertFalse(ok)
        self.assertNotIn('0.27', msg)


if __name__ == '__main__':
    unittest.main()
