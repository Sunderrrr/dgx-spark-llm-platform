"""Guardrails of the memory graph.

These tests cover the four ways this design can go wrong: node
fragmentation (« vLLM » ≠ « vllm » would make a duplicate graph, worse
than a flat list), writing without consent (memory is an opt-in), leakage
from one user to another, and the accumulation of contradictory facts
when information is updated.
"""

import unittest

import app as portal
# Memory left the monolith for memory_routes.py (28/08, 1st blueprint):
# we target it in its own module.
import memory_routes as memoire


class MemoryTestBase(unittest.TestCase):
    USER = 'memtest-a'
    OTHER = 'memtest-b'

    def setUp(self):
        self.ctx = portal.app.test_request_context()
        self.ctx.push()
        self._wipe()
        memoire._mem_set_enabled(self.USER, True)
        memoire._mem_set_enabled(self.OTHER, True)

    def tearDown(self):
        self._wipe()
        self.ctx.pop()

    def _wipe(self):
        db = portal.get_db()
        for u in (self.USER, self.OTHER):
            memoire._mem_purge(u)
            db.execute("DELETE FROM user_prefs WHERE username=?", (u,))
        db.commit()


class NormalisationTest(MemoryTestBase):
    """Spelling variants of one subject must converge on ONE node."""

    def test_casse_accents_et_ponctuation_convergent(self):
        self.assertEqual(memoire._mem_norm('vLLM'), memoire._mem_norm('VLLM'))
        self.assertEqual(memoire._mem_norm(' vllm '), memoire._mem_norm('vLLM'))
        self.assertEqual(memoire._mem_norm('Modèle'), memoire._mem_norm('modele'))
        self.assertEqual(memoire._mem_norm('DGX-Spark'), memoire._mem_norm('dgx spark'))

    def test_un_seul_noeud_pour_plusieurs_ecritures(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'utilise', 'Sert les modèles de chat.')
        memoire._mem_add_fact(self.USER, 'vllm', 'version', 'Tourne en 0.27.')
        memoire._mem_add_fact(self.USER, ' VLLM ', 'port', 'Écoute sur 8001.')
        noeuds = memoire._mem_graph(self.USER)['nodes']
        self.assertEqual(len(noeuds), 1, noeuds)
        self.assertEqual(len(memoire._mem_graph(self.USER)['edges']), 3)

    def test_ecritures_non_latines_memorisables(self):
        # [a-z0-9] alone made any Japanese/Russian/Greek subject impossible to
        # store: its normalized form was empty, thus rejected.
        for sujet in ('日本語', 'Привет', 'Ελληνικά', '中文'):
            self.assertTrue(memoire._mem_norm(sujet), sujet)
            _, ok = memoire._mem_add_fact(self.USER, sujet, 'note', f'Fait sur {sujet}')
            self.assertTrue(ok, sujet)
        self.assertEqual(len(memoire._mem_graph(self.USER)['nodes']), 4)

    def test_le_depliage_des_accents_ne_touche_que_le_latin(self):
        # In latin, « è » and « e » must converge. In Japanese NO: NFKD
        # decomposes « が » into « か » + dakuten, and dropping the latter
        # would confuse two different words.
        self.assertEqual(memoire._mem_norm('Modèle'), memoire._mem_norm('modele'))
        self.assertNotEqual(memoire._mem_norm('が'), memoire._mem_norm('か'))

    def test_sujet_vide_refuse(self):
        _, ok = memoire._mem_add_fact(self.USER, '   ', 'utilise', 'peu importe')
        self.assertFalse(ok)
        _, ok = memoire._mem_add_fact(self.USER, 'vLLM', 'utilise', '')
        self.assertFalse(ok)


class OptInTest(MemoryTestBase):
    """Memory is enabled by default (2026-09); only an explicit switch-off
    prevents writing."""

    def test_outil_refuse_si_desactivee(self):
        memoire._mem_set_enabled(self.USER, False)
        msg, ok = memoire._exec_memory_tool(
            'save_memory', {'subject': 'vLLM', 'fact': 'x'}, self.USER)
        self.assertFalse(ok)
        self.assertIn('désactivée', msg)
        self.assertEqual(memoire._mem_graph(self.USER)['edges'], [])

    def test_activee_par_defaut_sans_preferences(self):
        # An account with no preference row has memory ON — the default
        # applies; only an explicit deactivation wins.
        portal.get_db().execute("DELETE FROM user_prefs WHERE username=?", ('memtest-neuf',))
        portal.get_db().commit()
        self.assertTrue(memoire._mem_enabled('memtest-neuf'))

    def test_desactiver_n_efface_pas(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'utilise', 'Sert les modèles.')
        memoire._mem_set_enabled(self.USER, False)
        self.assertEqual(len(memoire._mem_graph(self.USER)['edges']), 1)


class IsolationTest(MemoryTestBase):
    """One user's memory must never reach another."""

    def test_rappel_cloisonne(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'utilise', "Secret de A.")
        memoire._mem_add_fact(self.OTHER, 'vLLM', 'utilise', "Secret de B.")
        faits_a = [f['fact'] for f in memoire._mem_recall(self.USER, 'vLLM')]
        faits_b = [f['fact'] for f in memoire._mem_recall(self.OTHER, 'vLLM')]
        self.assertEqual(faits_a, ["Secret de A."])
        self.assertEqual(faits_b, ["Secret de B."])

    def test_graphe_cloisonne(self):
        memoire._mem_add_fact(self.USER, 'Python', 'utilise', "A code en Python.")
        self.assertEqual(memoire._mem_graph(self.OTHER)['edges'], [])

    def test_suppression_cloisonnee(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'utilise', "Fait de A.")
        edge_id = memoire._mem_graph(self.USER)['edges'][0]['id']
        # B must not be able to delete a fact of A with its identifier.
        self.assertFalse(memoire._mem_forget(self.OTHER, edge_id))
        self.assertEqual(len(memoire._mem_graph(self.USER)['edges']), 1)


class PeremptionTest(MemoryTestBase):
    """Updated information replaces the old one instead of adding to it."""

    def test_meme_relation_remplace(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.25.")
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        faits = [f['fact'] for f in memoire._mem_recall(self.USER, 'vLLM')]
        self.assertEqual(faits, ["Tourne en 0.27."])

    def test_relations_differentes_coexistent(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        memoire._mem_add_fact(self.USER, 'vLLM', 'port', "Écoute sur 8001.")
        self.assertEqual(len(memoire._mem_recall(self.USER, 'vLLM')), 2)

    def test_le_fait_perime_reste_consultable(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.25.")
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        avec = memoire._mem_graph(self.USER, include_expired=True)['edges']
        self.assertEqual(len(avec), 2)
        self.assertEqual(len(memoire._mem_graph(self.USER)['edges']), 1)


class MiseAJourTest(MemoryTestBase):
    """Information that evolves must be correctable, without two distinct
    pieces of information erasing each other."""

    def test_deux_infos_sur_un_meme_sujet_coexistent(self):
        # Regression: the two manual adds share the generic relation. Treating
        # them as two versions of one fact erased the first one SILENTLY — we
        # lost information the user had entered.
        memoire._mem_add_fact(self.USER, 'vLLM', memoire.MEM_GENERIC_RELATION,
                             'Sert les modèles de chat.', source='user')
        memoire._mem_add_fact(self.USER, 'vLLM', memoire.MEM_GENERIC_RELATION,
                             'Écoute sur le port 8001.', source='user')
        faits = sorted(e['fact'] for e in memoire._mem_graph(self.USER)['edges'])
        self.assertEqual(faits, ['Sert les modèles de chat.', 'Écoute sur le port 8001.'])

    def test_relation_explicite_remplace_toujours(self):
        # Conversely, an explicit relation IS the update key.
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.25.')
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
        self.assertEqual([e['fact'] for e in memoire._mem_graph(self.USER)['edges']],
                         ['Tourne en 0.27.'])

    def test_modification_sur_place(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.25.')
        edge = memoire._mem_graph(self.USER)['edges'][0]
        msg, ok = memoire._mem_update_fact(self.USER, edge['id'], fact='Tourne en 0.27.')
        self.assertTrue(ok, msg)
        apres = memoire._mem_graph(self.USER)['edges']
        self.assertEqual(len(apres), 1)
        self.assertEqual(apres[0]['fact'], 'Tourne en 0.27.')
        # Same identifier: it is a correction, not a deletion + an addition.
        self.assertEqual(apres[0]['id'], edge['id'])

    def test_modification_texte_vide_refusee(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
        edge_id = memoire._mem_graph(self.USER)['edges'][0]['id']
        _, ok = memoire._mem_update_fact(self.USER, edge_id, fact='   ')
        self.assertFalse(ok)
        self.assertEqual(len(memoire._mem_graph(self.USER)['edges']), 1)

    def test_modification_cloisonnee(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', 'Fait de A.')
        edge_id = memoire._mem_graph(self.USER)['edges'][0]['id']
        _, ok = memoire._mem_update_fact(self.OTHER, edge_id, fact='Détourné par B.')
        self.assertFalse(ok)
        self.assertEqual(memoire._mem_graph(self.USER)['edges'][0]['fact'], 'Fait de A.')

    def test_modification_d_un_fait_perime_refusee(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.25.')
        ancien = memoire._mem_graph(self.USER, include_expired=True)['edges'][0]['id']
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', 'Tourne en 0.27.')
        _, ok = memoire._mem_update_fact(self.USER, ancien, fact='Ressuscité.')
        self.assertFalse(ok)


class RappelTest(MemoryTestBase):
    """Recall returns the subject's neighbourhood, not the whole memory."""

    def test_sujet_inconnu_ne_rend_rien(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'utilise', "Sert les modèles.")
        self.assertEqual(memoire._mem_recall(self.USER, 'Kubernetes'), [])

    def test_alias_par_l_objet_relie(self):
        # A fact linking two subjects must be found from either one.
        memoire._mem_add_fact(self.USER, 'Cronos', 'sert avec', "Cronos sert ses modèles avec vLLM.",
                             obj='vLLM')
        depuis_cronos = [f['fact'] for f in memoire._mem_recall(self.USER, 'Cronos')]
        depuis_vllm = [f['fact'] for f in memoire._mem_recall(self.USER, 'vLLM')]
        self.assertEqual(depuis_cronos, depuis_vllm)
        self.assertEqual(len(depuis_vllm), 1)

    def test_sujet_isole_ne_ramene_pas_le_reste(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        memoire._mem_add_fact(self.USER, 'Cuisine', 'aime', "Aime le curry.")
        faits = [f['fact'] for f in memoire._mem_recall(self.USER, 'vLLM')]
        self.assertEqual(faits, ["Tourne en 0.27."])

    def test_outil_de_rappel_cadre_les_faits_comme_des_donnees(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        msg, ok = memoire._exec_memory_tool('recall_memory', {'subject': 'vLLM'}, self.USER)
        self.assertTrue(ok)
        # A stored fact comes from a past conversation: it might have been written
        # to manipulate the model. It must arrive tagged « données ».
        self.assertIn('données', msg)
        self.assertIn('Tourne en 0.27.', msg)


class PlafondEtPurgeTest(MemoryTestBase):
    def test_plafond_de_faits(self):
        limite = memoire.MEM_MAX_FACTS
        memoire.MEM_MAX_FACTS = 3
        try:
            for i in range(3):
                _, ok = memoire._mem_add_fact(self.USER, f'Sujet{i}', 'note', f'Fait {i}')
                self.assertTrue(ok)
            msg, ok = memoire._mem_add_fact(self.USER, 'DeTrop', 'note', 'Un de trop')
            self.assertFalse(ok)
            self.assertIn('pleine', msg)
        finally:
            memoire.MEM_MAX_FACTS = limite

    def test_purge_efface_tout(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        memoire._mem_add_fact(self.USER, 'Python', 'utilise', "Code en Python.")
        self.assertEqual(memoire._mem_purge(self.USER), 2)
        g = memoire._mem_graph(self.USER, include_expired=True)
        self.assertEqual(g['edges'], [])
        self.assertEqual(g['nodes'], [])

    def test_oubli_nettoie_le_noeud_orphelin(self):
        memoire._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        edge_id = memoire._mem_graph(self.USER)['edges'][0]['id']
        self.assertTrue(memoire._mem_forget(self.USER, edge_id))
        self.assertEqual(memoire._mem_graph(self.USER)['nodes'], [])


if __name__ == '__main__':
    unittest.main()
