"""Garde-fous du graphe de mémoire.

Ces tests couvrent les quatre façons dont ce design peut mal tourner :
la fragmentation des nœuds (« vLLM » ≠ « vllm » ferait un graphe de doublons,
pire qu'une liste plate), l'écriture sans consentement (la mémoire est un
opt-in), la fuite d'un utilisateur vers un autre, et l'accumulation de faits
contradictoires quand une information est mise à jour.
"""

import unittest

import app as portal


class MemoryTestBase(unittest.TestCase):
    USER = 'memtest-a'
    OTHER = 'memtest-b'

    def setUp(self):
        self.ctx = portal.app.test_request_context()
        self.ctx.push()
        self._wipe()
        portal._mem_set_enabled(self.USER, True)
        portal._mem_set_enabled(self.OTHER, True)

    def tearDown(self):
        self._wipe()
        self.ctx.pop()

    def _wipe(self):
        db = portal.get_db()
        for u in (self.USER, self.OTHER):
            portal._mem_purge(u)
            db.execute("DELETE FROM user_prefs WHERE username=?", (u,))
        db.commit()


class NormalisationTest(MemoryTestBase):
    """Les variantes d'écriture d'un même sujet doivent converger sur UN nœud."""

    def test_casse_accents_et_ponctuation_convergent(self):
        self.assertEqual(portal._mem_norm('vLLM'), portal._mem_norm('VLLM'))
        self.assertEqual(portal._mem_norm(' vllm '), portal._mem_norm('vLLM'))
        self.assertEqual(portal._mem_norm('Modèle'), portal._mem_norm('modele'))
        self.assertEqual(portal._mem_norm('DGX-Spark'), portal._mem_norm('dgx spark'))

    def test_un_seul_noeud_pour_plusieurs_ecritures(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'utilise', 'Sert les modèles de chat.')
        portal._mem_add_fact(self.USER, 'vllm', 'version', 'Tourne en 0.27.')
        portal._mem_add_fact(self.USER, ' VLLM ', 'port', 'Écoute sur 8001.')
        noeuds = portal._mem_graph(self.USER)['nodes']
        self.assertEqual(len(noeuds), 1, noeuds)
        self.assertEqual(len(portal._mem_graph(self.USER)['edges']), 3)

    def test_sujet_vide_refuse(self):
        _, ok = portal._mem_add_fact(self.USER, '   ', 'utilise', 'peu importe')
        self.assertFalse(ok)
        _, ok = portal._mem_add_fact(self.USER, 'vLLM', 'utilise', '')
        self.assertFalse(ok)


class OptInTest(MemoryTestBase):
    """Rien ne s'écrit tant que l'utilisateur n'a pas activé la mémoire."""

    def test_outil_refuse_si_desactivee(self):
        portal._mem_set_enabled(self.USER, False)
        msg, ok = portal._exec_memory_tool(
            'save_memory', {'subject': 'vLLM', 'fact': 'x'}, self.USER)
        self.assertFalse(ok)
        self.assertIn('désactivée', msg)
        self.assertEqual(portal._mem_graph(self.USER)['edges'], [])

    def test_desactivee_par_defaut(self):
        portal.get_db().execute("DELETE FROM user_prefs WHERE username=?", ('memtest-neuf',))
        portal.get_db().commit()
        self.assertFalse(portal._mem_enabled('memtest-neuf'))

    def test_desactiver_n_efface_pas(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'utilise', 'Sert les modèles.')
        portal._mem_set_enabled(self.USER, False)
        self.assertEqual(len(portal._mem_graph(self.USER)['edges']), 1)


class IsolationTest(MemoryTestBase):
    """La mémoire d'un utilisateur ne doit jamais atteindre un autre."""

    def test_rappel_cloisonne(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'utilise', "Secret de A.")
        portal._mem_add_fact(self.OTHER, 'vLLM', 'utilise', "Secret de B.")
        faits_a = [f['fact'] for f in portal._mem_recall(self.USER, 'vLLM')]
        faits_b = [f['fact'] for f in portal._mem_recall(self.OTHER, 'vLLM')]
        self.assertEqual(faits_a, ["Secret de A."])
        self.assertEqual(faits_b, ["Secret de B."])

    def test_graphe_cloisonne(self):
        portal._mem_add_fact(self.USER, 'Python', 'utilise', "A code en Python.")
        self.assertEqual(portal._mem_graph(self.OTHER)['edges'], [])

    def test_suppression_cloisonnee(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'utilise', "Fait de A.")
        edge_id = portal._mem_graph(self.USER)['edges'][0]['id']
        # B ne doit pas pouvoir supprimer un fait de A avec son identifiant.
        self.assertFalse(portal._mem_forget(self.OTHER, edge_id))
        self.assertEqual(len(portal._mem_graph(self.USER)['edges']), 1)


class PeremptionTest(MemoryTestBase):
    """Une information mise à jour remplace l'ancienne au lieu de s'y ajouter."""

    def test_meme_relation_remplace(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.25.")
        portal._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        faits = [f['fact'] for f in portal._mem_recall(self.USER, 'vLLM')]
        self.assertEqual(faits, ["Tourne en 0.27."])

    def test_relations_differentes_coexistent(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        portal._mem_add_fact(self.USER, 'vLLM', 'port', "Écoute sur 8001.")
        self.assertEqual(len(portal._mem_recall(self.USER, 'vLLM')), 2)

    def test_le_fait_perime_reste_consultable(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.25.")
        portal._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        avec = portal._mem_graph(self.USER, include_expired=True)['edges']
        self.assertEqual(len(avec), 2)
        self.assertEqual(len(portal._mem_graph(self.USER)['edges']), 1)


class RappelTest(MemoryTestBase):
    """Le rappel rend le voisinage du sujet, pas toute la mémoire."""

    def test_sujet_inconnu_ne_rend_rien(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'utilise', "Sert les modèles.")
        self.assertEqual(portal._mem_recall(self.USER, 'Kubernetes'), [])

    def test_alias_par_l_objet_relie(self):
        # Un fait qui relie deux sujets doit être retrouvé depuis l'un OU l'autre.
        portal._mem_add_fact(self.USER, 'Cronos', 'sert avec', "Cronos sert ses modèles avec vLLM.",
                             obj='vLLM')
        depuis_cronos = [f['fact'] for f in portal._mem_recall(self.USER, 'Cronos')]
        depuis_vllm = [f['fact'] for f in portal._mem_recall(self.USER, 'vLLM')]
        self.assertEqual(depuis_cronos, depuis_vllm)
        self.assertEqual(len(depuis_vllm), 1)

    def test_sujet_isole_ne_ramene_pas_le_reste(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        portal._mem_add_fact(self.USER, 'Cuisine', 'aime', "Aime le curry.")
        faits = [f['fact'] for f in portal._mem_recall(self.USER, 'vLLM')]
        self.assertEqual(faits, ["Tourne en 0.27."])

    def test_outil_de_rappel_cadre_les_faits_comme_des_donnees(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        msg, ok = portal._exec_memory_tool('recall_memory', {'subject': 'vLLM'}, self.USER)
        self.assertTrue(ok)
        # Un fait mémorisé vient d'une conversation passée : il pourrait avoir été
        # rédigé pour manipuler le modèle. Il doit arriver étiqueté « données ».
        self.assertIn('données', msg)
        self.assertIn('Tourne en 0.27.', msg)


class PlafondEtPurgeTest(MemoryTestBase):
    def test_plafond_de_faits(self):
        limite = portal.MEM_MAX_FACTS
        portal.MEM_MAX_FACTS = 3
        try:
            for i in range(3):
                _, ok = portal._mem_add_fact(self.USER, f'Sujet{i}', 'note', f'Fait {i}')
                self.assertTrue(ok)
            msg, ok = portal._mem_add_fact(self.USER, 'DeTrop', 'note', 'Un de trop')
            self.assertFalse(ok)
            self.assertIn('pleine', msg)
        finally:
            portal.MEM_MAX_FACTS = limite

    def test_purge_efface_tout(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        portal._mem_add_fact(self.USER, 'Python', 'utilise', "Code en Python.")
        self.assertEqual(portal._mem_purge(self.USER), 2)
        g = portal._mem_graph(self.USER, include_expired=True)
        self.assertEqual(g['edges'], [])
        self.assertEqual(g['nodes'], [])

    def test_oubli_nettoie_le_noeud_orphelin(self):
        portal._mem_add_fact(self.USER, 'vLLM', 'version', "Tourne en 0.27.")
        edge_id = portal._mem_graph(self.USER)['edges'][0]['id']
        self.assertTrue(portal._mem_forget(self.USER, edge_id))
        self.assertEqual(portal._mem_graph(self.USER)['nodes'], [])


if __name__ == '__main__':
    unittest.main()
