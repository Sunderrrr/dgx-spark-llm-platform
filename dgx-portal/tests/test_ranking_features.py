"""Leaderboard features of 2026-10-14: requests and size per request, model
filter, groups, reference cost, activity heatmap, records and streaks, one
account's detail, anonymity, CSV export.

LiteLLM's Postgres is a fake that answers each query by its shape (the same
approach as test_ranking.py); keys are named after their account.
"""
import copy
import csv
import io
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import app as portal
import ranking
import stats

ALICE, BOB, CAROL = 'ztest-alice', 'ztest-bob', 'ztest-carol'


class FakeCursor:
    def __init__(self, answers):
        self.answers = answers
        self.rows = []
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        self.rows = []
        for marker, rows in self.answers:
            if marker in sql:
                self.rows = rows
                break

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return (None,)


class FakeConn:
    def __init__(self, answers):
        self.cur = FakeCursor(answers)

    def cursor(self):
        return self.cur

    def close(self):
        pass


def fake(answers, accounts=(ALICE, BOB, CAROL)):
    conn = FakeConn(answers)
    keys = {ALICE, BOB, CAROL, 'unknown-key'}
    return conn, [
        mock.patch.object(stats, '_spend_conn', return_value=conn),
        mock.patch.object(stats, '_key_user_map', return_value={k: k for k in keys}),
        mock.patch.object(stats, '_known_accounts', return_value=set(accounts)),
    ]


class Patched(unittest.TestCase):
    def run_with(self, answers, fn, accounts=(ALICE, BOB, CAROL)):
        conn, patches = fake(answers, accounts)
        for p in patches:
            p.start()
        try:
            with portal.app.app_context():
                return fn(), conn
        finally:
            for p in patches:
                p.stop()


# current rows: (bucket, key, model, prompt, completion, requests)
CURRENT = [(1, ALICE, 'qwen', 9000, 1000, 2),           # 10 000 tokens, 2 requests
           (1, BOB, 'qwen', 500, 500, 10),              # 1 000 tokens, 10 requests
           (2, BOB, 'mimo', 2000, 0, 5)]
PREVIOUS = [(ALICE, 4000, 1000, 1), (BOB, 1000, 0, 30)]
MODELS = [('qwen', 12, 11000), ('mimo', 5, 1000)]


class RankingMetricsTest(Patched):

    def rank(self, metric='total', **kw):
        answers = [('GROUP BY b, api_key, model_group', CURRENT),
                   ('AND "startTime" <', PREVIOUS), ('GROUP BY model_group', MODELS)]
        return self.run_with(answers, lambda: ranking.ranking_full('month', me=BOB, metric=metric, **kw))

    def test_requests_and_size_per_request_reorder_the_board(self):
        d, _ = self.rank('requests')
        self.assertEqual([r['username'] for r in d['rows']], [BOB, ALICE])
        self.assertEqual(d['rows'][0]['value'], 15)
        d, _ = self.rank('per_request')
        self.assertEqual([r['username'] for r in d['rows']], [ALICE, BOB])
        self.assertEqual(d['rows'][0]['value'], 5000.0)
        # An average does not add up: the platform's is over all its requests.
        self.assertEqual(d['total'], round(13000 / 17, 1))
        self.assertIsNone(d['rows'][0]['share_pct'])

    def test_delta_uses_the_same_metric(self):
        d, _ = self.rank('requests')
        bob = next(r for r in d['rows'] if r['username'] == BOB)
        self.assertAlmostEqual(bob['delta'], (15 - 30) / 30 * 100)

    def test_models_listed_and_per_row(self):
        d, _ = self.rank()
        self.assertEqual([m['model'] for m in d['models']], ['qwen', 'mimo'])
        bob = next(r for r in d['rows'] if r['username'] == BOB)
        self.assertEqual([m['model'] for m in bob['models']], ['mimo', 'qwen'])
        self.assertAlmostEqual(sum(m['share_pct'] for m in bob['models']), 100)

    def test_model_filter_reaches_both_queries(self):
        _, conn = self.rank(model='qwen')
        filtered = [q for q in conn.cur.queries if 'model_group = %s' in q[0]]
        self.assertEqual(len(filtered), 2, "current AND previous period must be filtered")
        self.assertTrue(all('qwen' in q[1] for q in filtered))

    def test_reference_cost(self):
        with mock.patch.object(ranking, 'prices', return_value={'input': 2.0, 'output': 10.0, 'currency': 'EUR'}):
            d, _ = self.rank()
        alice = next(r for r in d['rows'] if r['username'] == ALICE)
        self.assertEqual(alice['cost'], round(9000 / 1e6 * 2 + 1000 / 1e6 * 10, 2))
        self.assertEqual(d['prices']['currency'], 'EUR')

    def test_groups(self):
        with mock.patch.object(ranking, '_user_groups', return_value={ALICE: {'labo'}, BOB: {'labo', 'compta'}}):
            d, _ = self.rank('requests', by='group')
        by_name = {r['username']: r for r in d['rows']}
        self.assertEqual(by_name['labo']['value'], 17)
        self.assertEqual(by_name['labo']['members'], 2)
        self.assertEqual(by_name['compta']['value'], 15)
        self.assertTrue(by_name['compta']['is_me'])
        self.assertTrue(all(r['is_group'] for r in d['rows']))


class ActivityRecordsTest(Patched):

    def test_activity_is_real_moments(self):
        """One cell = one real hour (week) or one real day (month, year):
        never every Monday of the period folded into one slot."""
        today = datetime.now(ranking.ZoneInfo(ranking.LOCAL_TZ)).date()
        two_days_ago = today - timedelta(days=2)
        rows = [(today, 9, ALICE, 4, 100), (today, 9, BOB, 2, 50), (two_days_ago, 23, ALICE, 1, 10),
                (today - timedelta(days=200), 9, ALICE, 7, 70)]       # outside the week, inside the year
        d, _ = self.run_with([('GROUP BY 1, 2, api_key', rows)], lambda: ranking.activity('week'))
        self.assertEqual(d['mode'], 'hours')
        self.assertEqual([r['day'] for r in d['rows']][-1], today.isoformat())
        self.assertEqual(len(d['rows']), 7)
        self.assertEqual(d['rows'][-1]['cells'][9]['requests'], 6)
        self.assertEqual(d['rows'][-3]['cells'][23]['tokens'], 10)
        self.assertEqual((d['max_requests'], d['total_requests']), (6, 7))
        d, _ = self.run_with([('GROUP BY 1, 2, api_key', rows)], lambda: ranking.activity('year'))
        self.assertEqual(d['mode'], 'days')
        self.assertEqual(len(d['days']), 365)
        by_day = {x['day']: x['requests'] for x in d['days']}
        self.assertEqual(by_day[today.isoformat()], 6)
        self.assertEqual(by_day[(today - timedelta(days=200)).isoformat()], 7)
        self.assertEqual(d['total_requests'], 14)
        d, _ = self.run_with([('GROUP BY 1, 2, api_key', rows)], lambda: ranking.activity('week', BOB))
        self.assertEqual(d['total_requests'], 2)

    def test_records_and_streaks(self):
        today = date.today()
        old = datetime(2026, 1, 1)
        rows = [(today - timedelta(days=i), ALICE, 'qwen', 100 * (i + 1), 1, old + timedelta(days=i))
                for i in range(3)]                                    # 3 days in a row, up to today
        rows += [(today - timedelta(days=10), BOB, 'mimo', 5000, 3, datetime(2025, 6, 1)),
                 (today - timedelta(days=12), BOB, 'qwen', 10, 1, datetime(2025, 5, 1))]
        ranking._RECORDS_CACHE.update(at=0, data=None)
        d, _ = self.run_with([('GROUP BY d, api_key, model_group', rows)], lambda: ranking.records(ALICE))
        ranking._RECORDS_CACHE.update(at=0, data=None)
        self.assertEqual(d['best_days'][0]['username'], BOB)
        self.assertEqual(d['me']['current_streak'], 3)
        self.assertEqual(d['me']['longest_streak'], 3)
        self.assertEqual([c['username'] for c in d['current_streaks']], [ALICE])
        firsts = {f['model']: f['username'] for f in d['firsts']}
        self.assertEqual(firsts, {'qwen': BOB, 'mimo': BOB})


class RankingRoutesTest(unittest.TestCase):
    """Privacy, detail and export, through the routes."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self.addCleanup(self._clean)
        self._clean()
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_prefs (username, ranking_anonymous) VALUES (?, 1)", (ALICE,))
            db.commit()
        rows = [{'rank': 1, 'username': ALICE, 'is_me': False, 'is_group': False, 'members': None,
                 'is_unattributed': False, 'value': 10, 'tokens': 10, 'prompt': 5, 'completion': 5,
                 'requests': 1, 'share_pct': 50.0, 'delta': None, 'trend': [], 'models': [], 'cost': 0.1},
                {'rank': 2, 'username': BOB, 'is_me': False, 'is_group': False, 'members': None,
                 'is_unattributed': False, 'value': 10, 'tokens': 10, 'prompt': 5, 'completion': 5,
                 'requests': 1, 'share_pct': 50.0, 'delta': None, 'trend': [], 'models': [], 'cost': 0.1}]
        data = {'period': 'day', 'metric': 'total', 'rows': rows, 'by': 'account', 'model': None,
                'active_count': 2, 'total': 20, 'avg': 10, 'has_prev': True, 'models': [],
                'prices': {'input': 2.5, 'output': 10, 'currency': 'USD'}, 'cost_total': 0.2}
        # A fresh copy per call: the route masks names IN the rows it gets.
        for p in (mock.patch.object(ranking, 'ranking_full', side_effect=lambda *a, **k: copy.deepcopy(data)),
                  mock.patch.object(ranking, 'quotas', return_value={ALICE: 42.0})):
            p.start()
            self.addCleanup(p.stop)

    def _clean(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM user_prefs WHERE username LIKE 'ztest-%'")
            db.commit()

    def client(self, user, admin=False):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['csrf'] = 'tok'
            s['username'] = user
            s['auth_at'] = int(time.time())
            s['is_admin'] = admin
        return c

    def names(self, c):
        return [r['username'] for r in c.get('/api/ranking').get_json()['rows']]

    def test_anonymous_hidden_from_others_only(self):
        self.assertEqual(self.names(self.client(BOB)), [None, BOB])
        self.assertEqual(self.names(self.client(ALICE)), [ALICE, BOB])
        d = self.client(CAROL, admin=True).get('/api/ranking').get_json()
        self.assertEqual([r['username'] for r in d['rows']], [ALICE, BOB])
        self.assertEqual(d['rows'][0]['quota_pct'], 42.0)
        self.assertNotIn('quota_pct', self.client(BOB).get('/api/ranking').get_json()['rows'][1])

    def test_privacy_toggle(self):
        c = self.client(BOB)
        r = c.post('/api/ranking/privacy', data={'anonymous': '1'}, headers={'X-CSRFToken': 'tok'})
        self.assertTrue(r.get_json()['anonymous'])
        self.assertTrue(c.get('/api/ranking').get_json()['me_anonymous'])
        self.assertEqual(self.names(self.client(CAROL)), [None, None])
        c.post('/api/ranking/privacy', data={'anonymous': '0'}, headers={'X-CSRFToken': 'tok'})
        self.assertEqual(self.names(self.client(CAROL)), [None, BOB])

    def test_detail_self_or_admin(self):
        with mock.patch.object(ranking, 'user_detail', return_value={'username': BOB}):
            self.assertEqual(self.client(BOB).get(f'/api/ranking/user/{BOB}').status_code, 200)
            self.assertEqual(self.client(BOB).get(f'/api/ranking/user/{ALICE}').status_code, 404)
            self.assertEqual(self.client(CAROL, admin=True).get(f'/api/ranking/user/{ALICE}').status_code, 200)

    def test_export_admin_only(self):
        self.assertEqual(self.client(BOB).get('/api/ranking/export.csv').status_code, 404)
        r = self.client(CAROL, admin=True).get('/api/ranking/export.csv?period=day')
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r.headers['Content-Disposition'])
        rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
        self.assertEqual(rows[0][:3], ['rank', 'name', 'value'])
        self.assertEqual([x[1] for x in rows[1:]], [ALICE, BOB])

    def test_records_masked(self):
        rec = {'best_days': [{'username': ALICE, 'day': '2026-10-01', 'tokens': 5, 'requests': 1}],
               'longest_streaks': [], 'current_streaks': [], 'firsts': [], 'me': None}
        with mock.patch.object(ranking, 'records', return_value=rec):
            d = self.client(BOB).get('/api/ranking/records').get_json()
        self.assertIsNone(d['best_days'][0]['username'])


if __name__ == '__main__':
    unittest.main()
