"""Classement : ce qu'il classe, et ce qu'il refuse de classer.

Trois defauts corriges le 2026-09-14, que ces tests verrouillent : « nouveau »
s'affichait sur TOUTES les lignes de la periode « depuis le debut » (le backend
ne distinguait pas « pas de periode precedente » de « compte absent la periode
d'avant ») ; le bucket `inconnu` prenait un rang comme s'il etait une personne ;
et la barre donnait la part du LEADER, jamais celle de la periode.

S'y ajoute la regle de fond, heritee de `auth.etat_compte` : une donnee ABSENTE
n'est pas une preuve d'absence. Si la base des comptes est illisible, on ne
reclasse personne — un vrai compte demasque comme residu serait pire qu'un
residu affiche une fois de trop.

Le Postgres de LiteLLM n'est evidemment pas joignable ici : `_spend_conn` rend
un curseur factice qui repond selon la requete (courante ou precedente).
"""
import unittest
from unittest import mock

import stats


class _CurseurFactice:
    """Repond a chaque requete ce que le test a prepare pour elle."""

    def __init__(self, courant, precedent):
        self._courant = courant
        self._precedent = precedent
        self._lignes = []

    def execute(self, sql, params=None):
        if 'GROUP BY b, api_key' in sql:
            self._lignes = self._courant
        elif 'AND "startTime" <' in sql:
            self._lignes = self._precedent
        else:
            self._lignes = []

    def fetchall(self):
        return self._lignes

    def fetchone(self):
        # La periode « depuis le debut » commence par demander le tout premier
        # log ; sans reponse elle leve, et `ranking_full` se rabat sur un
        # classement vide — ce qui masquerait le test au lieu de le faire echouer.
        return (None,)


class _ConnFactice:
    def __init__(self, courant, precedent):
        self._c = _CurseurFactice(courant, precedent)

    def cursor(self):
        return self._c

    def close(self):
        pass


def _classement(courant, precedent=(), comptes=('alice', 'bob'), metric='total', period='month'):
    """`ranking_full` sur un Postgres factice.

    `courant` : lignes (bucket, cle, prompt, genere) de la periode.
    `precedent` : lignes (cle, prompt, genere) de la periode d'avant.
    `comptes` : les noms qui existent VRAIMENT (le reste est un residu).
    """
    conn = _ConnFactice(courant, precedent)
    with mock.patch.object(stats, '_spend_conn', return_value=conn), \
         mock.patch.object(stats, '_key_user_map', return_value={k: k for k in
                        {l[1] for l in courant} | {l[0] for l in precedent}}), \
         mock.patch.object(stats, '_comptes_connus', return_value=set(comptes)):
        return stats.ranking_full(period, metric=metric)


class TestResidus(unittest.TestCase):
    def test_un_compte_inconnu_ne_prend_ni_rang_ni_medaille(self):
        d = _classement(courant=[(1, 'alice', 900, 100), (1, 'zz-pwtest', 50, 0)])
        rangs = {r['username']: r for r in d['rows']}
        self.assertEqual(rangs['alice']['rank'], 1)
        self.assertIsNone(rangs['zz-pwtest']['rank'])
        self.assertTrue(rangs['zz-pwtest']['is_unattributed'])
        self.assertFalse(rangs['alice']['is_unattributed'])

    def test_le_bucket_inconnu_est_traite_comme_un_residu(self):
        # « inconnu » = cle absente des trois tables de jetons, donc supprimee a
        # la main : ce n'est pas davantage une personne qu'un nom de test.
        d = _classement(courant=[(1, 'alice', 10, 1), (1, 'inconnu', 500, 0)])
        inconnu = [r for r in d['rows'] if r['username'] == 'inconnu'][0]
        self.assertTrue(inconnu['is_unattributed'])
        self.assertIsNone(inconnu['rank'])
        # ...et il passe en fin de tableau, pas au milieu des gens.
        self.assertEqual(d['rows'][-1]['username'], 'inconnu')

    def test_un_residu_ne_compte_pas_comme_compte_actif_mais_reste_dans_le_total(self):
        d = _classement(courant=[(1, 'alice', 900, 100), (1, 'zz-pwtest', 50, 0)])
        self.assertEqual(d['active_count'], 1)
        self.assertEqual(d['total'], 1050)

    def test_un_residu_n_affiche_pas_de_delta(self):
        # Mesure avant correctif : +290 919 % sur le bucket `inconnu`. Un residu
        # n'a pas d'histoire, donc pas d'evolution.
        d = _classement(courant=[(1, 'inconnu', 500, 0)], precedent=[('inconnu', 0.17, 0)])
        self.assertIsNone(d['rows'][0]['delta'])

    def test_base_des_comptes_illisible_on_ne_reclasse_personne(self):
        # `_comptes_connus` renvoie None quand la base locale est illisible : on
        # ne peut alors pas distinguer une personne d'un residu, et on ne
        # conclut rien. Seul `inconnu` reste non attribue, par construction.
        with mock.patch.object(stats, 'get_db', side_effect=RuntimeError('base illisible')):
            self.assertIsNone(stats._comptes_connus())
        conn = _ConnFactice([(1, 'alice', 10, 1), (1, 'martin', 5, 0)], [])
        with mock.patch.object(stats, '_spend_conn', return_value=conn), \
             mock.patch.object(stats, '_key_user_map', return_value={'alice': 'alice', 'martin': 'martin'}), \
             mock.patch.object(stats, '_comptes_connus', return_value=None):
            d = stats.ranking_full('month')
        self.assertEqual([r['rank'] for r in d['rows']], [1, 2])
        self.assertFalse(any(r['is_unattributed'] for r in d['rows']))


class TestPartsEtDeltas(unittest.TestCase):
    def test_les_parts_somment_a_100_meme_avec_des_residus(self):
        d = _classement(courant=[(1, 'alice', 600, 0), (1, 'bob', 300, 0), (1, 'zz-pwtest', 100, 0)])
        self.assertAlmostEqual(sum(r['share_pct'] for r in d['rows']), 100.0, places=6)
        self.assertAlmostEqual([r for r in d['rows'] if r['username'] == 'alice'][0]['share_pct'], 60.0)

    def test_depuis_le_debut_n_a_pas_de_periode_precedente(self):
        d = _classement(courant=[(1, 'alice', 600, 0)], period='all')
        self.assertFalse(d['has_prev'])
        self.assertIsNone(d['rows'][0]['delta'])

    def test_has_prev_vrai_sur_les_periodes_bornees(self):
        d = _classement(courant=[(1, 'alice', 600, 0)], period='month')
        self.assertTrue(d['has_prev'])

    def test_le_delta_porte_sur_la_metrique_affichee(self):
        # Alice genere plus, mais ecrit beaucoup moins qu'avant : classee sur le
        # genere, elle monte ; classee sur l'entree, elle s'effondre. Un delta
        # calcule sur le total melangerait les deux et ne voudrait rien dire.
        courant = [(1, 'alice', 10, 1000)]
        precedent = [('alice', 1000, 100)]
        self.assertAlmostEqual(_classement(courant, precedent, metric='completion')['rows'][0]['delta'],
                               (1000 - 100) / 100 * 100)
        self.assertAlmostEqual(_classement(courant, precedent, metric='prompt')['rows'][0]['delta'],
                               (10 - 1000) / 1000 * 100)

    def test_la_moyenne_porte_sur_les_comptes_classes(self):
        d = _classement(courant=[(1, 'alice', 600, 0), (1, 'bob', 200, 0), (1, 'zz-pwtest', 200, 0)])
        self.assertEqual(d['avg'], 400)   # (600 + 200) / 2, residu exclu
        self.assertEqual(d['total'], 1000)  # mais compte dans le total


class TestMetrique(unittest.TestCase):
    def test_la_metrique_change_l_ordre_et_la_valeur(self):
        # Mesure reelle du 2026-09-14 : le 1er en volume total generee 0,46 % de
        # ses tokens. Les deux lectures ne designent pas le meme vainqueur.
        courant = [(1, 'alice', 900, 100), (1, 'bob', 300, 700)]
        par_total = _classement(courant, metric='total')
        par_genere = _classement(courant, metric='completion')
        self.assertEqual([r['username'] for r in par_total['rows']], ['alice', 'bob'])
        self.assertEqual([r['username'] for r in par_genere['rows']], ['bob', 'alice'])
        self.assertEqual(par_total['rows'][0]['value'], 1000)
        self.assertEqual(par_genere['rows'][0]['value'], 700)
        # Les deux compteurs restent ramenes : l'interface montre « dont N generes ».
        self.assertEqual(par_total['rows'][0]['completion'], 100)

    def test_metrique_inconnue_retombe_sur_le_total(self):
        self.assertEqual(_classement([(1, 'alice', 1, 1)], metric='nimportequoi')['metric'], 'total')

    def test_un_compte_sans_consommation_sur_la_metrique_choisie_disparait(self):
        # Alice n'a fait qu'envoyer, Bob que generer.
        courant = [(1, 'alice', 500, 0), (1, 'bob', 0, 500)]
        d = _classement(courant, metric='completion')
        self.assertEqual([r['username'] for r in d['rows']], ['bob'])


class TestTendance(unittest.TestCase):
    def test_la_tendance_suit_le_nombre_de_buckets_de_la_periode(self):
        # 24 h sur « jour », 7 j sur « semaine », 30 j sur « mois », 12 mois sur
        # « annee ». Sous 5 points l'interface n'affiche rien plutot qu'une
        # courbe : « depuis le debut » n'en a que 3.
        self.assertEqual(len(_classement([(3, 'alice', 10, 0)], period='day')['rows'][0]['trend']), 24)
        self.assertEqual(len(_classement([(3, 'alice', 10, 0)], period='week')['rows'][0]['trend']), 7)
        self.assertEqual(len(_classement([(3, 'alice', 10, 0)], period='month')['rows'][0]['trend']), 30)

    def test_la_tendance_porte_sur_la_metrique_choisie(self):
        # Sur « jour » les buckets sont des HEURES (0..23), donc les cles du test
        # tombent juste. Les buckets d'une periode « mois » sont des dates : y
        # placer un entier ne remplit rien, et la somme vaudrait zero.
        courant = [(1, 'alice', 100, 0), (2, 'alice', 0, 5)]
        par_total = _classement(courant, metric='total', period='day')['rows'][0]['trend']
        par_genere = _classement(courant, metric='completion', period='day')['rows'][0]['trend']
        self.assertEqual(sum(par_total), 105)
        self.assertEqual(sum(par_genere), 5)
        self.assertEqual((par_total[1], par_total[2]), (100, 5))
        self.assertEqual((par_genere[1], par_genere[2]), (0, 5))


class TestCasLimites(unittest.TestCase):
    def test_aucune_consommation(self):
        d = _classement(courant=[])
        self.assertEqual(d['rows'], [])
        self.assertEqual(d['active_count'], 0)
        self.assertEqual(d['total'], 0)
        self.assertEqual(d['avg'], 0)

    def test_que_des_residus(self):
        # Division par zero interdite : sans compte classe, la moyenne est nulle
        # et le classement vide — mais le total, lui, existe.
        d = _classement(courant=[(1, 'zz-pwtest', 50, 0)])
        self.assertEqual(d['active_count'], 0)
        self.assertEqual(d['avg'], 0)
        self.assertEqual(d['total'], 50)
        self.assertIsNone(d['rows'][0]['rank'])

    def test_postgres_injoignable(self):
        with mock.patch.object(stats, '_spend_conn', return_value=None):
            d = stats.ranking_full('month')
        self.assertEqual(d['rows'], [])
        self.assertTrue(d['has_prev'])


if __name__ == '__main__':
    unittest.main()
