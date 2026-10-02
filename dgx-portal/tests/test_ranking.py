"""Ranking: what it ranks, and what it refuses to rank.

Three defects fixed on 2026-09-14, which these tests lock in: « nouveau »
displayed on ALL rows of the « depuis le debut » period (the backend did
not distinguish « no previous period » from « account absent the period
before »); the `inconnu` bucket took a rank as if it were a person; and
the bar showed the LEADER's share, never the period's.

On top of that the underlying rule, inherited from `auth.etat_compte`:
MISSING data is not proof of absence. If the accounts database is
unreadable, we re-rank nobody — a real account unmasked as a leftover
would be worse than a leftover displayed once too often.

LiteLLM's Postgres is obviously unreachable here: `_spend_conn` returns
a fake cursor that answers according to the query (current or previous).
"""
import unittest
from unittest import mock

import stats


class _CurseurFactice:
    """Answers each query what the test prepared for it."""

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
        # The « since the start » period begins by asking for the very first
        # log; without an answer it raises, and `ranking_full` falls back on an
        # empty ranking — which would mask the test instead of failing it.
        return (None,)


class _ConnFactice:
    def __init__(self, courant, precedent):
        self._c = _CurseurFactice(courant, precedent)

    def cursor(self):
        return self._c

    def close(self):
        pass


def _classement(courant, precedent=(), comptes=('alice', 'bob'), metric='total', period='month'):
    """`ranking_full` on a fake Postgres.

    `courant`: rows (bucket, key, prompt, generated) of the period.
    `precedent`: rows (key, prompt, generated) of the period before.
    `comptes`: the names that REALLY exist (the rest is a leftover).
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
        # « inconnu » = key absent from the three token tables, thus deleted by
        # hand: it is no more a person than a test name.
        d = _classement(courant=[(1, 'alice', 10, 1), (1, 'inconnu', 500, 0)])
        inconnu = [r for r in d['rows'] if r['username'] == 'inconnu'][0]
        self.assertTrue(inconnu['is_unattributed'])
        self.assertIsNone(inconnu['rank'])
        # ...and it goes to the bottom of the table, not among people.
        self.assertEqual(d['rows'][-1]['username'], 'inconnu')

    def test_un_residu_ne_compte_pas_comme_compte_actif_mais_reste_dans_le_total(self):
        d = _classement(courant=[(1, 'alice', 900, 100), (1, 'zz-pwtest', 50, 0)])
        self.assertEqual(d['active_count'], 1)
        self.assertEqual(d['total'], 1050)

    def test_un_residu_n_affiche_pas_de_delta(self):
        # Measurement before the fix: +290 919 % on the `inconnu` bucket. A
        # leftover has no history, hence no evolution.
        d = _classement(courant=[(1, 'inconnu', 500, 0)], precedent=[('inconnu', 0.17, 0)])
        self.assertIsNone(d['rows'][0]['delta'])

    def test_base_des_comptes_illisible_on_ne_reclasse_personne(self):
        # `_comptes_connus` returns None when the local database is unreadable:
        # we then cannot tell a person from a leftover, and we conclude
        # nothing. Only `inconnu` stays unattributed, by construction.
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
        # Alice generates more, but writes far less than before: ranked on the
        # generated, she goes up; ranked on the input, she collapses. A delta
        # computed on the total would mix the two and mean nothing.
        courant = [(1, 'alice', 10, 1000)]
        precedent = [('alice', 1000, 100)]
        self.assertAlmostEqual(_classement(courant, precedent, metric='completion')['rows'][0]['delta'],
                               (1000 - 100) / 100 * 100)
        self.assertAlmostEqual(_classement(courant, precedent, metric='prompt')['rows'][0]['delta'],
                               (10 - 1000) / 1000 * 100)

    def test_la_moyenne_porte_sur_les_comptes_classes(self):
        d = _classement(courant=[(1, 'alice', 600, 0), (1, 'bob', 200, 0), (1, 'zz-pwtest', 200, 0)])
        self.assertEqual(d['avg'], 400)   # (600 + 200) / 2, residu exclu
        self.assertEqual(d['total'], 1000)  # but counts in the total


class TestMetrique(unittest.TestCase):
    def test_la_metrique_change_l_ordre_et_la_valeur(self):
        # Real measurement of 2026-09-14: the #1 in total volume generated 0,46 %
        # of its tokens. The two readings do not crown the same winner.
        courant = [(1, 'alice', 900, 100), (1, 'bob', 300, 700)]
        par_total = _classement(courant, metric='total')
        par_genere = _classement(courant, metric='completion')
        self.assertEqual([r['username'] for r in par_total['rows']], ['alice', 'bob'])
        self.assertEqual([r['username'] for r in par_genere['rows']], ['bob', 'alice'])
        self.assertEqual(par_total['rows'][0]['value'], 1000)
        self.assertEqual(par_genere['rows'][0]['value'], 700)
        # Both counters are still fetched: the UI shows « dont N generes ».
        self.assertEqual(par_total['rows'][0]['completion'], 100)

    def test_metrique_inconnue_retombe_sur_le_total(self):
        self.assertEqual(_classement([(1, 'alice', 1, 1)], metric='nimportequoi')['metric'], 'total')

    def test_un_compte_sans_consommation_sur_la_metrique_choisie_disparait(self):
        # Alice only sent, Bob only generated.
        courant = [(1, 'alice', 500, 0), (1, 'bob', 0, 500)]
        d = _classement(courant, metric='completion')
        self.assertEqual([r['username'] for r in d['rows']], ['bob'])


class TestTendance(unittest.TestCase):
    def test_la_tendance_suit_le_nombre_de_buckets_de_la_periode(self):
        # 24 h on « jour », 7 days on « semaine », 30 days on « mois », 12 months
        # on « annee ». Under 5 points the UI shows nothing rather than a
        # curve: « depuis le debut » only has 3.
        self.assertEqual(len(_classement([(3, 'alice', 10, 0)], period='day')['rows'][0]['trend']), 24)
        self.assertEqual(len(_classement([(3, 'alice', 10, 0)], period='week')['rows'][0]['trend']), 7)
        self.assertEqual(len(_classement([(3, 'alice', 10, 0)], period='month')['rows'][0]['trend']), 30)

    def test_la_tendance_porte_sur_la_metrique_choisie(self):
        # On « jour » the buckets are HOURS (0..23), so the test keys fall
        # right. The buckets of a « mois » period are dates: putting an integer
        # there fills nothing, and the sum would be zero.
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
        # Division by zero forbidden: with no ranked account the average is
        # null and the ranking empty — but the total does exist.
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
