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


class _FakeCursor:
    """Answers each query what the test prepared for it."""

    def __init__(self, current_value, previous):
        self._current = current_value
        self._precedent = previous
        self._rows = []

    def execute(self, sql, params=None):
        if 'GROUP BY b, api_key' in sql:
            self._rows = self._current
        elif 'AND "startTime" <' in sql:
            self._rows = self._precedent
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        # The « since the start » period begins by asking for the very first
        # log; without an answer it raises, and `ranking_full` falls back on an
        # empty ranking — which would mask the test instead of failing it.
        return (None,)


class _FakeConn:
    def __init__(self, current_value, previous):
        self._c = _FakeCursor(current_value, previous)

    def cursor(self):
        return self._c

    def close(self):
        pass


def _ranking(current_value, previous=(), accounts=('alice', 'bob'), metric='total', period='month'):
    """`ranking_full` on a fake Postgres.

    `courant`: rows (bucket, key, prompt, generated) of the period.
    `precedent`: rows (key, prompt, generated) of the period before.
    `comptes`: the names that REALLY exist (the rest is a leftover).
    """
    conn = _FakeConn(current_value, previous)
    with mock.patch.object(stats, '_spend_conn', return_value=conn), \
         mock.patch.object(stats, '_key_user_map', return_value={k: k for k in
                        {l[1] for l in current_value} | {l[0] for l in previous}}), \
         mock.patch.object(stats, '_known_accounts', return_value=set(accounts)):
        return stats.ranking_full(period, metric=metric)


class TestResidues(unittest.TestCase):
    def test_unknown_account_gets_no_rank_nor_medal(self):
        d = _ranking(current_value=[(1, 'alice', 900, 100), (1, 'zz-pwtest', 50, 0)])
        ranks = {r['username']: r for r in d['rows']}
        self.assertEqual(ranks['alice']['rank'], 1)
        self.assertIsNone(ranks['zz-pwtest']['rank'])
        self.assertTrue(ranks['zz-pwtest']['is_unattributed'])
        self.assertFalse(ranks['alice']['is_unattributed'])

    def test_unknown_bucket_treated_as_residue(self):
        # « inconnu » = key absent from the three token tables, thus deleted by
        # hand: it is no more a person than a test name.
        d = _ranking(current_value=[(1, 'alice', 10, 1), (1, 'unknown', 500, 0)])
        unknown = [r for r in d['rows'] if r['username'] == 'unknown'][0]
        self.assertTrue(unknown['is_unattributed'])
        self.assertIsNone(unknown['rank'])
        # ...and it goes to the bottom of the table, not among people.
        self.assertEqual(d['rows'][-1]['username'], 'unknown')

    def test_residue_not_active_account_but_in_total(self):
        d = _ranking(current_value=[(1, 'alice', 900, 100), (1, 'zz-pwtest', 50, 0)])
        self.assertEqual(d['active_count'], 1)
        self.assertEqual(d['total'], 1050)

    def test_residue_shows_no_delta(self):
        # Measurement before the fix: +290 919 % on the `inconnu` bucket. A
        # leftover has no history, hence no evolution.
        d = _ranking(current_value=[(1, 'unknown', 500, 0)], previous=[('unknown', 0.17, 0)])
        self.assertIsNone(d['rows'][0]['delta'])

    def test_unreadable_accounts_db_reranks_nobody(self):
        # `_comptes_connus` returns None when the local database is unreadable:
        # we then cannot tell a person from a leftover, and we conclude
        # nothing. Only `inconnu` stays unattributed, by construction.
        with mock.patch.object(stats, 'get_db', side_effect=RuntimeError('base illisible')):
            self.assertIsNone(stats._known_accounts())
        conn = _FakeConn([(1, 'alice', 10, 1), (1, 'martin', 5, 0)], [])
        with mock.patch.object(stats, '_spend_conn', return_value=conn), \
             mock.patch.object(stats, '_key_user_map', return_value={'alice': 'alice', 'martin': 'martin'}), \
             mock.patch.object(stats, '_known_accounts', return_value=None):
            d = stats.ranking_full('month')
        self.assertEqual([r['rank'] for r in d['rows']], [1, 2])
        self.assertFalse(any(r['is_unattributed'] for r in d['rows']))


class TestSharesAndDeltas(unittest.TestCase):
    def test_shares_sum_to_100_even_with_residues(self):
        d = _ranking(current_value=[(1, 'alice', 600, 0), (1, 'bob', 300, 0), (1, 'zz-pwtest', 100, 0)])
        self.assertAlmostEqual(sum(r['share_pct'] for r in d['rows']), 100.0, places=6)
        self.assertAlmostEqual([r for r in d['rows'] if r['username'] == 'alice'][0]['share_pct'], 60.0)

    def test_since_start_has_no_previous_period(self):
        d = _ranking(current_value=[(1, 'alice', 600, 0)], period='all')
        self.assertFalse(d['has_prev'])
        self.assertIsNone(d['rows'][0]['delta'])

    def test_has_prev_true_on_bounded_periods(self):
        d = _ranking(current_value=[(1, 'alice', 600, 0)], period='month')
        self.assertTrue(d['has_prev'])

    def test_delta_on_displayed_metric(self):
        # Alice generates more, but writes far less than before: ranked on the
        # generated, she goes up; ranked on the input, she collapses. A delta
        # computed on the total would mix the two and mean nothing.
        current_value = [(1, 'alice', 10, 1000)]
        previous = [('alice', 1000, 100)]
        self.assertAlmostEqual(_ranking(current_value, previous, metric='completion')['rows'][0]['delta'],
                               (1000 - 100) / 100 * 100)
        self.assertAlmostEqual(_ranking(current_value, previous, metric='prompt')['rows'][0]['delta'],
                               (10 - 1000) / 1000 * 100)

    def test_average_on_ranked_accounts(self):
        d = _ranking(current_value=[(1, 'alice', 600, 0), (1, 'bob', 200, 0), (1, 'zz-pwtest', 200, 0)])
        self.assertEqual(d['avg'], 400)   # (600 + 200) / 2, residu exclu
        self.assertEqual(d['total'], 1000)  # but counts in the total


class TestMetric(unittest.TestCase):
    def test_metric_changes_order_and_value(self):
        # Real measurement of 2026-09-14: the #1 in total volume generated 0,46 %
        # of its tokens. The two readings do not crown the same winner.
        current_value = [(1, 'alice', 900, 100), (1, 'bob', 300, 700)]
        by_total = _ranking(current_value, metric='total')
        by_generated = _ranking(current_value, metric='completion')
        self.assertEqual([r['username'] for r in by_total['rows']], ['alice', 'bob'])
        self.assertEqual([r['username'] for r in by_generated['rows']], ['bob', 'alice'])
        self.assertEqual(by_total['rows'][0]['value'], 1000)
        self.assertEqual(by_generated['rows'][0]['value'], 700)
        # Both counters are still fetched: the UI shows « dont N generes ».
        self.assertEqual(by_total['rows'][0]['completion'], 100)

    def test_unknown_metric_falls_back_to_total(self):
        self.assertEqual(_ranking([(1, 'alice', 1, 1)], metric='nimportequoi')['metric'], 'total')

    def test_account_without_usage_on_metric_disappears(self):
        # Alice only sent, Bob only generated.
        current_value = [(1, 'alice', 500, 0), (1, 'bob', 0, 500)]
        d = _ranking(current_value, metric='completion')
        self.assertEqual([r['username'] for r in d['rows']], ['bob'])


class TestTrend(unittest.TestCase):
    def test_trend_follows_period_bucket_count(self):
        # 24 h on « jour », 7 days on « semaine », 30 days on « mois », 12 months
        # on « annee ». Under 5 points the UI shows nothing rather than a
        # curve: « depuis le debut » only has 3.
        self.assertEqual(len(_ranking([(3, 'alice', 10, 0)], period='day')['rows'][0]['trend']), 24)
        self.assertEqual(len(_ranking([(3, 'alice', 10, 0)], period='week')['rows'][0]['trend']), 7)
        self.assertEqual(len(_ranking([(3, 'alice', 10, 0)], period='month')['rows'][0]['trend']), 30)

    def test_trend_on_chosen_metric(self):
        # On « jour » the buckets are HOURS (0..23), so the test keys fall
        # right. The buckets of a « mois » period are dates: putting an integer
        # there fills nothing, and the sum would be zero.
        current_value = [(1, 'alice', 100, 0), (2, 'alice', 0, 5)]
        by_total = _ranking(current_value, metric='total', period='day')['rows'][0]['trend']
        by_generated = _ranking(current_value, metric='completion', period='day')['rows'][0]['trend']
        self.assertEqual(sum(by_total), 105)
        self.assertEqual(sum(by_generated), 5)
        self.assertEqual((by_total[1], by_total[2]), (100, 5))
        self.assertEqual((by_generated[1], by_generated[2]), (0, 5))


class TestEdgeCases(unittest.TestCase):
    def test_no_usage(self):
        d = _ranking(current_value=[])
        self.assertEqual(d['rows'], [])
        self.assertEqual(d['active_count'], 0)
        self.assertEqual(d['total'], 0)
        self.assertEqual(d['avg'], 0)

    def test_only_residues(self):
        # Division by zero forbidden: with no ranked account the average is
        # null and the ranking empty — but the total does exist.
        d = _ranking(current_value=[(1, 'zz-pwtest', 50, 0)])
        self.assertEqual(d['active_count'], 0)
        self.assertEqual(d['avg'], 0)
        self.assertEqual(d['total'], 50)
        self.assertIsNone(d['rows'][0]['rank'])

    def test_postgres_unreachable(self):
        with mock.patch.object(stats, '_spend_conn', return_value=None):
            d = stats.ranking_full('month')
        self.assertEqual(d['rows'], [])
        self.assertTrue(d['has_prev'])


if __name__ == '__main__':
    unittest.main()
