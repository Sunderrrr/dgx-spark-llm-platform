"""Model health: what each engine can say, and what it cannot.

The two engines expose different metrics, and the « requetes servies »
tile long displayed for llama.cpp a TOKEN counter (39 303 requests
announced for 39 303 generated tokens). Fixing it was to change nothing
for vLLM, which publishes a real request counter: these tests freeze the
boundary so the vLLM path does not leave with the next cleanup.
"""
import os
import shutil
import sqlite3
import tempfile
import time
import types
import unittest
from unittest import mock

import vllm_health
import stats


def _metrics_vllm(gen="1000", success=("42", "8"), ttft=("12.5", "50")):
    """Rend un /metrics vLLM realiste : metriques etiquetees, lignes a sommer."""
    lines = [
        'vllm:generation_tokens_total{model_name="m"} %s' % gen,
        'vllm:num_requests_running{model_name="m"} 2.0',
        'vllm:num_requests_waiting{model_name="m"} 1.0',
        'vllm:request_success_total{finished_reason="stop",model_name="m"} %s' % success[0],
        'vllm:request_success_total{finished_reason="length",model_name="m"} %s' % success[1],
        'vllm:time_to_first_token_seconds_sum{model_name="m"} %s' % ttft[0],
        'vllm:time_to_first_token_seconds_count{model_name="m"} %s' % ttft[1],
    ]
    return "\n".join(lines) + "\n"


_METRICS_LLAMA = ("llamacpp:tokens_predicted_total 39080\n"
                  "llamacpp:tokens_predicted_seconds_total 1235.5\n"
                  "llamacpp:n_decode_total 39303\n"
                  "llamacpp:predicted_tokens_seconds 0\n"
                  "llamacpp:requests_processing 0\n"
                  "llamacpp:requests_deferred 0\n")


def _health(engine, text):
    """Calls vllm_health() pretending `engine` serves `texte`."""
    line = {'engine': engine}
    fake_db = types.SimpleNamespace(
        execute=lambda *a, **k: types.SimpleNamespace(fetchone=lambda: line))
    vllm_health._vllm_health_cache['t'] = 0.0
    with mock.patch.object(vllm_health, 'get_running_models', return_value=['m']), \
         mock.patch.object(vllm_health, 'get_db', return_value=fake_db), \
         mock.patch.object(vllm_health.requests, 'get',
                           return_value=types.SimpleNamespace(text=text)):
        return vllm_health.vllm_health()


class VllmHealthTest(unittest.TestCase):
    """vLLM publishes everything needed: nothing of its display must move."""

    def setUp(self):
        vllm_health._vllm_tps.update(t=0.0, gen=0.0)

    def test_request_counter_sums_labels(self):
        d = _health('vllm', _metrics_vllm())
        self.assertEqual(d['requests'], 50)      # 42 "stop" + 8 "length"
        self.assertEqual(d['running'], 2)
        self.assertEqual(d['waiting'], 1)

    def test_ttft_from_engine_histogram(self):
        d = _health('vllm', _metrics_vllm())
        self.assertEqual(d['ttft'], 0.25)        # 12.5 s / 50 requetes

    def test_rate_computed_as_delta_over_two_samples(self):
        self.assertIsNone(_health('vllm', _metrics_vllm("1000"))['tps'])
        time.sleep(1.05)
        tps = _health('vllm', _metrics_vllm("1300"))['tps']
        self.assertIsNotNone(tps)
        self.assertTrue(250 < tps < 350, tps)    # ~300 tokens en ~1 s

    def test_idle_vllm_does_not_inherit_other_engine_ttft(self):
        """The « measured by the portal » fallback is reserved for llama.cpp.

        On `ttft_cnt == 0` it would display, for a just-launched vLLM, the
        TTFT left by the previous engine.
        """
        with mock.patch('stats.measured_ttft', return_value=9.99):
            d = _health('vllm', _metrics_vllm("0", success=("0", "0"), ttft=("0", "0")))
        self.assertEqual(d['requests'], 0)
        self.assertIsNone(d['ttft'])


class LlamacppHealthTest(unittest.TestCase):
    """llama.cpp knows less: it must stay silent rather than invent."""

    def test_no_invented_request_counter(self):
        self.assertIsNone(_health('llamacpp', _METRICS_LLAMA)['requests'])

    def test_ttft_from_portal_measure(self):
        with mock.patch('stats.measured_ttft', return_value=0.27):
            self.assertEqual(_health('llamacpp', _METRICS_LLAMA)['ttft'], 0.27)

    def test_rate_read_from_n_decode_total_the_only_one_moving(self):
        """During a generation the gauge reads 0 and tokens_predicted_total does
        not move: only n_decode_total advances.

        Its DECODE STEPS multiply by the number of active slots to yield
        tokens (see DebitAgregeTest): 30 steps/s over 2 slots = 60 tok/s.
        """
        vllm_health._llama_tps.update(t=0.0, dec=None)
        base = ("llamacpp:tokens_predicted_total 39080\n"
                "llamacpp:tokens_predicted_seconds_total 1235.5\n"
                "llamacpp:predicted_tokens_seconds 0\n"
                "llamacpp:requests_processing 2\n"
                "llamacpp:requests_deferred 0\n")
        _health('llamacpp', base + "llamacpp:n_decode_total 1000\n")
        time.sleep(1.05)
        tps = _health('llamacpp', base + "llamacpp:n_decode_total 1030\n")['tps']
        self.assertTrue(52 < tps < 68, tps)      # 30 steps/s x 2 slots

    def test_rate_drops_to_zero_when_nobody_generates(self):
        """Counter still between two readings = nobody generates anymore.

        We displayed the last known throughput, which read as a frozen
        counter while the machine did nothing.
        """
        vllm_health._llama_tps.update(t=0.0, dec=None)
        base = ("llamacpp:tokens_predicted_total 39080\n"
                "llamacpp:tokens_predicted_seconds_total 1235.5\n"
                "llamacpp:predicted_tokens_seconds 0\n"
                "llamacpp:requests_deferred 0\n")
        active = base + "llamacpp:requests_processing 1\n"
        idle = base + "llamacpp:requests_processing 0\n"
        _health('llamacpp', active + "llamacpp:n_decode_total 1000\n")
        time.sleep(1.05)
        # a generation happened: non-zero throughput
        self.assertTrue(_health('llamacpp', active + "llamacpp:n_decode_total 1030\n")['tps'] > 0)
        time.sleep(1.05)
        # the counter stopped moving: nobody uses the model
        self.assertEqual(_health('llamacpp', idle + "llamacpp:n_decode_total 1030\n")['tps'], 0.0)


class EffectiveContextTest(unittest.TestCase):
    """llama.cpp's `--ctx-size` is a TOTAL... except with a unified cache.

    The dashboard announced 58 254 / 29 127 where the engine served 524 288
    per slot: the division by `--parallel` does not apply when the slots
    share a single pool.
    """

    def test_without_parallel_context_is_whole(self):
        self.assertEqual(vllm_health.effective_ctx("--ctx-size 524288", "llamacpp"), 524288)

    def test_parallel_alone_divides_context(self):
        # Without --kv-unified, llama.cpp partitions: each slot has its slice.
        self.assertEqual(
            vllm_health.effective_ctx("--ctx-size 1048576 --parallel 4", "llamacpp"), 262144)

    def test_kv_unified_cancels_division(self):
        self.assertEqual(
            vllm_health.effective_ctx("--ctx-size 524288 --parallel 6 --kv-unified",
                                      "llamacpp"), 524288)

    def test_no_kv_unified_restores_division(self):
        self.assertEqual(
            vllm_health.effective_ctx("--ctx-size 524288 --parallel 2 --no-kv-unified",
                                      "llamacpp"), 262144)

    def test_kv_unified_per_slot_declares_window(self):
        """llama.cpp 0.5.0: the DECLARED per-session cap is authoritative.

        Measured on 2026-09-25 (Flash-Next): with `--ctx-size 1048576
        --parallel 4 --kv-unified --kv-unified-per-slot 262144` the engine
        serves 262144 per session — that is what it announces on /props. The
        « unified » rule above would have announced 1048576, five times the
        real prompt limit, discovered only at the failure of the request.
        """
        self.assertEqual(
            vllm_health.effective_ctx(
                "--ctx-size 1048576 --parallel 4 --kv-unified "
                "--kv-unified-per-slot 262144", "llamacpp"), 262144)
        e, s_ = vllm_health.ctx_split(
            "--ctx-size 1048576 --parallel 4 --kv-unified "
            "--kv-unified-per-slot 262144 --n-predict 65536", "llamacpp")
        self.assertEqual((e, s_), (196608, 65536))

    def test_kv_unified_per_slot_without_ctx_size(self):
        """The cap suffices on its own: the engine then sizes the pool.

        Without `--ctx-size`, the default formula finds nothing and fell
        back on 32 768 — a declared cap must answer before it.
        """
        self.assertEqual(
            vllm_health.effective_ctx(
                "--parallel 4 --kv-unified --kv-unified-per-slot 131072", "llamacpp"), 131072)

    def test_displayed_split_follows(self):
        """256k in input AND 256k in output, what the operator asked for."""
        e, s_ = vllm_health.ctx_split(
            "--ctx-size 524288 --parallel 6 --kv-unified --n-predict 262144", "llamacpp")
        self.assertEqual((e, s_), (262144, 262144))

    def test_vllm_not_affected(self):
        self.assertEqual(vllm_health.effective_ctx("--max-model-len 32768", "vllm"), 32768)


class AggregateRateTest(unittest.TestCase):
    """`n_decode_total` counts DECODE STEPS, not tokens.

    llama.cpp batches the slots: a step produces one token per active slot.
    The dashboard thus displayed the throughput divided by the number of
    sessions — 17,4 tok/s where clients received 69,5.
    """

    def _metrics(self, decode, processing):
        return ("llamacpp:tokens_predicted_total 1000\n"
                "llamacpp:tokens_predicted_seconds_total 30\n"
                "llamacpp:predicted_tokens_seconds 0\n"
                "llamacpp:requests_deferred 0\n"
                "llamacpp:n_decode_total %s\n"
                "llamacpp:requests_processing %s\n" % (decode, processing))

    def _measure(self, steps_per_second, sessions):
        vllm_health._llama_tps.update(t=0.0, dec=None)
        _health('llamacpp', self._metrics(1000, sessions))
        time.sleep(1.05)
        d = _health('llamacpp', self._metrics(1000 + steps_per_second * 1.05, sessions))
        return d['tps']

    def test_single_session_unchanged(self):
        self.assertTrue(30 < self._measure(34, 1) < 38)

    def test_four_sessions_multiply_rate(self):
        """4 sessions at ~17 steps/s = ~69 tok/s delivered, not 17."""
        tps = self._measure(17, 4)
        self.assertTrue(60 < tps < 76, tps)

    def test_idle_rate_is_zero(self):
        vllm_health._llama_tps.update(t=0.0, dec=None)
        _health('llamacpp', self._metrics(1000, 0))
        time.sleep(1.05)
        self.assertEqual(_health('llamacpp', self._metrics(1000, 0))['tps'], 0.0)

    def test_generation_end_does_not_drop_to_zero(self):
        """Window straddling the end: tokens produced, no active slot anymore.

        Without the guard the product would be zero and the throughput would blink.
        """
        vllm_health._llama_tps.update(t=0.0, dec=None)
        _health('llamacpp', self._metrics(1000, 1))
        time.sleep(1.05)
        self.assertTrue(_health('llamacpp', self._metrics(1030, 0))['tps'] > 0)


_METRICS_LLAMA_FULL = (
    "llamacpp:tokens_predicted_total 158729\n"
    "llamacpp:tokens_predicted_seconds_total 11921.9\n"
    "llamacpp:n_decode_total 132689\n"
    "llamacpp:prompt_tokens_total 1815000\n"
    "llamacpp:prompt_tokens_seconds 248.062\n"
    "llamacpp:predicted_tokens_seconds 0\n"
    "llamacpp:requests_processing 4\n"
    "llamacpp:requests_deferred 0\n")


class CumulativeCountersTest(unittest.TestCase):
    """What the engine can say about the work already done.

    Instantaneous throughput is not enough: measured on 2026-09-14, 4
    requests in flight, `n_decode_total` advancing by ONE step in 6 s (the
    model was ingesting input contexts) and a prefill at 248 tok/s. The
    screen thus announced « 0 tok/s » while the GPU worked. Hence the exact
    average since startup, and the input counters.
    """

    def test_engine_counters_exposed(self):
        with mock.patch('stats.accumulate_generated_tokens', return_value=999999):
            s = _health('llamacpp', _METRICS_LLAMA_FULL)
        self.assertEqual(s['tokens_generated'], 158729)
        self.assertEqual(s['tokens_prompt'], 1815000)
        self.assertEqual(s['tps_prefill'], 248.1)
        self.assertEqual(s['tokens_generated_total'], 999999)

    def test_average_exact_not_sampled(self):
        """158 729 tokens in 11 921,9 s of generation = 13,3 tok/s.

        The ratio of two engine counters: no approximation tied to the
        portal's probe frequency, unlike the instantaneous throughput.
        """
        s = _health('llamacpp', _METRICS_LLAMA_FULL)
        self.assertEqual(s['tps_avg'], 13.3)

    def test_no_average_on_just_restarted_counter(self):
        """Fresh counter: the average would cover barely a few minutes.

        Measured on 2026-09-14: reset, 47 tokens and 192 s of generation →
        « 0,2 tok/s ». The 300 s threshold avoids displaying this meaningless
        figure; the cumulative total, for its part, stays right.
        """
        fresh = ("llamacpp:tokens_predicted_total 47\n"
                "llamacpp:tokens_predicted_seconds_total 192.443\n"
                "llamacpp:requests_processing 1\n")
        self.assertIsNone(_health('llamacpp', fresh)['tps_avg'])
        above = fresh.replace('192.443', '310.0')
        self.assertEqual(_health('llamacpp', above)['tps_avg'], 0.2)

    def test_vllm_has_no_average_to_invent(self):
        """vLLM publishes no generation seconds: None, not a fake figure."""
        s = _health('vllm', _metrics_vllm())
        self.assertIsNone(s['tps_avg'])
        self.assertEqual(s['tokens_generated'], 1000)

    def test_total_failure_does_not_break_health(self):
        with mock.patch('stats.accumulate_generated_tokens', side_effect=RuntimeError('db')):
            s = _health('llamacpp', _METRICS_LLAMA_FULL)
        self.assertIsNone(s['tokens_generated_total'])
        self.assertEqual(s['tokens_generated'], 158729)


class _FakeCursor2:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _FakeDb:
    """Minimal base: one row read, and the writes memorized to count them."""

    def __init__(self, row=None):
        self.row = row
        self.ecritures = []

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith('SELECT'):
            return _FakeCursor2(self.row)
        self.ecritures.append(params)
        self.row = {'base': params[1], 'last_value': params[2]}
        return _FakeCursor2(None)

    def commit(self):
        pass


class GeneratedTokensTotalTest(unittest.TestCase):
    """« Garder les tokens générés »: the engine counter restarts from zero at
    every relaunch, so the cumulative must not fall with it — and above all
    never count the same launch twice."""

    def _total(self, db, value):
        with mock.patch.object(stats, 'get_db', return_value=db):
            return stats.accumulate_generated_tokens('modele', value)

    def test_first_sample(self):
        self.assertEqual(self._total(_FakeDb(), 100), 100)

    def test_two_samples_same_launch_do_not_double(self):
        db = _FakeDb()
        self._total(db, 100)
        self.assertEqual(self._total(db, 150), 150)

    def test_reset_archives_already_counted_value(self):
        """Counter moving backwards: the engine restarted OR reset its cache to
        zero — measured on 2026-09-14, `tokens_predicted_total` from 158 864
        to 47 with the SAME pid, while `prompt_tokens_total` did not move.
        Both cases call for the same treatment: the work already done stays owed.
        """
        db = _FakeDb()
        self._total(db, 150)
        self.assertEqual(self._total(db, 20), 170)
        self.assertEqual(self._total(db, 30), 180)

    def test_total_never_goes_back(self):
        db = _FakeDb()
        values = [500, 800, 12, 40, 3, 60]
        totals = [self._total(db, v) for v in values]
        self.assertEqual(totals, sorted(totals), totals)

    def test_no_write_when_nothing_moves(self):
        """At rest the probe runs at 1 s: not one SQLite write per second."""
        db = _FakeDb()
        self._total(db, 100)
        self.assertEqual(len(db.ecritures), 1)
        self._total(db, 100)
        self._total(db, 100)
        self.assertEqual(len(db.ecritures), 1)

    def test_without_model_or_value_nothing_tried(self):
        db = _FakeDb()
        with mock.patch.object(stats, 'get_db', return_value=db):
            self.assertIsNone(stats.accumulate_generated_tokens('', 100))
            self.assertIsNone(stats.accumulate_generated_tokens('modele', None))
        self.assertEqual(db.ecritures, [])


_BUSY_SLOTS = [
    {'id': 0, 'id_task': 279, 'is_processing': True,
     'n_prompt_tokens': 22000, 'n_prompt_tokens_processed': 18000},
    {'id': 1, 'id_task': 915, 'is_processing': False, 'n_prompt_tokens': 0,
     'n_prompt_tokens_processed': 0},
    {'id': 2, 'id_task': 1334, 'is_processing': True,
     'n_prompt_tokens': 5000, 'n_prompt_tokens_processed': 5000},
    {'id': 3, 'id_task': 11, 'is_processing': False, 'n_prompt_tokens': 0,
     'n_prompt_tokens_processed': 0},
]


def _with_slots(slots):
    """Makes /slots answer like llama.cpp, leaving /metrics to the given text."""
    def _get(url, **k):
        if url.endswith('/slots'):
            return types.SimpleNamespace(json=lambda: slots)
        return types.SimpleNamespace(text=_METRICS_LLAMA_FULL)
    return _get


class ActiveSlotsTest(unittest.TestCase):
    """During a request, the engine is the ONLY source of activity.

    LiteLLM only writes its row at the end: measured on 2026-09-14, 44
    minutes without a single row while two sessions worked, hence a panel
    announcing « personne » while the GPU ran. These tests freeze what the
    portal can say despite that — and what it refuses to invent.
    """

    def setUp(self):
        vllm_health._slots_tasks.clear()
        vllm_health._vllm_health_cache['t'] = 0.0

    def _activity(self, slots):
        with mock.patch.object(vllm_health.requests, 'get', side_effect=_with_slots(slots)):
            return vllm_health._slots_activity()

    def test_only_processing_sessions_count(self):
        a = self._activity(_BUSY_SLOTS)
        self.assertEqual(a['busy'], 2)
        self.assertEqual(a['total'], 4)

    def test_prompt_ingestion_summed(self):
        """« where is the prompt »: 23 000 of the 27 000 ingested tokens."""
        a = self._activity(_BUSY_SLOTS)
        self.assertEqual(a['prompt_total'], 27000)
        self.assertEqual(a['prompt_processed'], 23000)

    def test_age_tracked_by_task_id(self):
        """llama.cpp gives a task id, not a start time."""
        self._activity(_BUSY_SLOTS)
        self.assertIsNotNone(self._activity(_BUSY_SLOTS)['oldest_s'])
        # Task seen 10 minutes ago: it is the age that must come out.
        vllm_health._slots_tasks[279] = time.time() - 600
        a = self._activity(_BUSY_SLOTS)
        self.assertTrue(598 < a['oldest_s'] < 604, a['oldest_s'])

    def test_finished_task_leaves_tracking(self):
        """Otherwise the task table would grow forever."""
        self._activity(_BUSY_SLOTS)
        self.assertIn(279, vllm_health._slots_tasks)
        self._activity([s for s in _BUSY_SLOTS if s['id'] != 0])
        self.assertNotIn(279, vllm_health._slots_tasks)
        self.assertIn(1334, vllm_health._slots_tasks)

    def test_engine_without_slots_returns_none_not_zero(self):
        """vLLM exposes no /slots, and a probe failure is not « personne »."""
        with mock.patch.object(vllm_health.requests, 'get',
                               side_effect=RuntimeError('injoignable')):
            self.assertIsNone(vllm_health._slots_activity())

    def test_health_carries_slot_activity(self):
        def _get(url, **k):
            if url.endswith('/slots'):
                return types.SimpleNamespace(json=lambda: _BUSY_SLOTS)
            return types.SimpleNamespace(text=_METRICS_LLAMA_FULL)
        line = {'engine': 'llamacpp'}
        fake_db = types.SimpleNamespace(
            execute=lambda *a, **k: types.SimpleNamespace(fetchone=lambda: line))
        vllm_health._vllm_health_cache['t'] = 0.0
        with mock.patch.object(vllm_health, 'get_running_models', return_value=['m']), \
             mock.patch.object(vllm_health, 'get_db', return_value=fake_db), \
             mock.patch.object(vllm_health.requests, 'get', side_effect=_get):
            h = vllm_health.vllm_health()
        self.assertEqual(h['slots']['busy'], 2)


class InflightStatsTest(unittest.TestCase):
    """Reading the IN-FLIGHT requests written by the LiteLLM callback.

    This file is the only OBSERVED attribution: without it, the panel can
    only say what the engine sees (busy sessions) and what SpendLogs has
    already seen (finished requests). It is also, by construction, a file
    that can be missing or unreadable — hence these tests.
    """

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.path = os.path.join(self.folder, 'inflight.db')
        self._avant = stats._INFLIGHT_DB
        stats._INFLIGHT_DB = self.path

    def tearDown(self):
        stats._INFLIGHT_DB = self._avant
        shutil.rmtree(self.folder, ignore_errors=True)

    def _write(self, lines):
        conn = sqlite3.connect(self.path)
        conn.execute('CREATE TABLE IF NOT EXISTS inflight (request_key TEXT PRIMARY KEY, alias TEXT, '
                     'user_id TEXT, model TEXT, started_at REAL NOT NULL)')
        for key, alias, user_id, age in lines:
            conn.execute('INSERT INTO inflight VALUES (?,?,?,?,?)',
                         (key, alias, user_id, 'm', time.time() - age))
        conn.commit()
        conn.close()

    def test_known_alias_gives_account(self):
        with mock.patch.object(stats, '_account_exists', side_effect=lambda n: n == 'alice'):
            self._write([('c1', 'Opencode-alice', '', 12)])
            actives = stats._inflight()
        self.assertEqual(list(actives), ['alice'])
        self.assertTrue(11 < actives['alice'] < 15, actives)

    def test_unknown_alias_guesses_no_name(self):
        """Better to display nothing than an invented name."""
        with mock.patch.object(stats, '_account_exists', return_value=False):
            self._write([('c1', 'Opencode-Omarchy', 'personne-connu', 5)])
            self.assertEqual(stats._inflight(), {})

    def test_user_id_is_fallback(self):
        with mock.patch.object(stats, '_account_exists', side_effect=lambda n: n == 'bob'):
            self._write([('c1', '', 'bob', 3)])
            self.assertEqual(list(stats._inflight()), ['bob'])

    def test_oldest_request_wins(self):
        with mock.patch.object(stats, '_account_exists', side_effect=lambda n: n == 'alice'):
            self._write([('c1', 'alice-1', '', 2), ('c2', 'alice-2', '', 300)])
            self.assertTrue(stats._inflight()['alice'] > 290)

    def test_missing_or_unreadable_file_does_not_raise(self):
        self.assertEqual(stats._inflight(), {})          # absent
        with open(self.path, 'w') as f:
            f.write('ceci n est pas une base sqlite')
        with mock.patch.object(stats, '_account_exists', return_value=True):
            self.assertEqual(stats._inflight(), {})      # illisible


class KnownModelsTest(unittest.TestCase):
    """`get_running_models` — a failed probe is NOT proof of absence.

    Measured 2026-10-02: the engine's /v1/models freezes during a large
    prefill (user prompts of 45k-149k tokens held it unreachable in ~15 s
    slices) and every user read « aucun modèle actif » although the model was
    fine and busy. The last known list is kept for _RM_GRACE_S after the last
    SUCCESSFUL probe; a probe that answers with an empty list still clears it.
    """

    def setUp(self):
        vllm_health._rm_cache.update(t=0.0, v=[], ok=0.0)

    def _probe(self, response=None, error_text=None):
        def _get(*a, **k):
            if error_text:
                raise error_text
            return response
        return mock.patch.object(vllm_health.requests, 'get', side_effect=_get)

    def test_failed_probe_keeps_last_known_list(self):
        rep = mock.Mock(ok=True)
        rep.json.return_value = {'data': [{'id': 'm1'}]}
        with self._probe(response=rep):
            self.assertEqual(vllm_health.get_running_models(), ['m1'])
        vllm_health._rm_cache['t'] = 0.0          # force a new probe
        with self._probe(error_text=ConnectionError('fige pendant le prefill')):
            self.assertEqual(vllm_health.get_running_models(), ['m1'])

    def test_stale_list_eventually_cleared(self):
        vllm_health._rm_cache.update(t=0.0, v=['m1'],
                                     ok=time.time() - vllm_health._RM_GRACE_S - 1)
        with self._probe(error_text=ConnectionError('moteur mort')):
            self.assertEqual(vllm_health.get_running_models(), [])

    def test_truly_empty_answer_clears_list(self):
        vllm_health._rm_cache.update(t=0.0, v=['m1'], ok=time.time())
        rep = mock.Mock(ok=True)
        rep.json.return_value = {'data': []}
        with self._probe(response=rep):
            self.assertEqual(vllm_health.get_running_models(), [])   # preuve d'absence

    def test_failed_probe_not_retried_every_call(self):
        vllm_health._rm_cache.update(t=0.0, v=['m1'], ok=time.time())
        calls = []
        def _get(*a, **k):
            calls.append(1)
            raise ConnectionError('fige')
        with mock.patch.object(vllm_health.requests, 'get', side_effect=_get):
            vllm_health.get_running_models()
            vllm_health.get_running_models()
            vllm_health.get_running_models()
        self.assertEqual(len(calls), 1)          # the 5 s cache protects the frozen engine


class LiveRateTest(unittest.TestCase):
    """`stats.debit_decode_live` — the live decode gauge, for the engines that
    publish NO /metrics (TabbyAPI). The dashboard showed « 0 tok/s » while the
    model was generating, because the SpendLogs figure only lands at the END of
    a request (measured 2026-10-02 with real traffic). The relay now reports
    its progress and the gauge sums the ongoing rates — divided by the DECODE
    time (first token), not by the request's age: measured 2026-10-02, the
    queue + prefill dilution read 12 tok/s against ~19 real. The token count
    itself is calibrated on `usage` (measured 3.44 chars/token on MiMo; chars/4
    read 86 % of the truth).
    """

    def setUp(self):
        import tempfile
        self.path = tempfile.mkdtemp() + '/t.db'
        c = sqlite3.connect(self.path)
        c.execute("""CREATE TABLE inflight_requests (
            id TEXT PRIMARY KEY, username TEXT, started_at REAL,
            tokens INTEGER NOT NULL DEFAULT 0, decode_since REAL)""")
        c.commit(); c.close()

    def _insert(self, rid, tokens, age_s, decode_age_s=None):
        c = sqlite3.connect(self.path)
        c.execute("INSERT INTO inflight_requests VALUES (?,?,?,?,?)",
                  (rid, 'alice', time.time() - age_s, tokens,
                   time.time() - decode_age_s if decode_age_s is not None else None))
        c.commit(); c.close()

    def test_rate_sums_running_rates(self):
        self._insert('a', 100, 30, decode_age_s=10)      # 10 tok/s de DECODAGE
        self._insert('b', 60, 200, decode_age_s=20)      # 3 tok/s
        with mock.patch.object(stats, 'DB_PATH', self.path):
            self.assertAlmostEqual(stats.live_decode_rate(), 13.0, delta=0.5)

    def test_decode_no_longer_divides_by_wait_time(self):
        # 60 s of queue + 10 s of decoding: the request age would dilute it
        # (0.86 tok/s); the right denominator gives 6 tok/s.
        self._insert('a', 60, 70, decode_age_s=10)
        with mock.patch.object(stats, 'DB_PATH', self.path):
            self.assertAlmostEqual(stats.live_decode_rate(), 6.0, delta=0.2)

    def test_no_running_request_says_zero(self):
        with mock.patch.object(stats, 'DB_PATH', self.path):
            self.assertEqual(stats.live_decode_rate(), 0.0)

    def test_brand_new_request_does_not_divide_by_zero(self):
        self._insert('a', 50, 5, decode_age_s=0.2)      # < 1 s : ignoree
        with mock.patch.object(stats, 'DB_PATH', self.path):
            self.assertEqual(stats.live_decode_rate(), 0.0)

    def test_counter_estimates_tokens_with_calibrated_ratio(self):
        self._insert('a', 0, 5)
        with mock.patch.object(stats, 'DB_PATH', self.path), \
             mock.patch.object(stats, 'ratio_chars_per_token', return_value=2.0):
            stats._inflight_tokens('a', 240, time.time() - 5)   # 240 car. / 2 = 120 tok
            self.assertAlmostEqual(stats.live_decode_rate(), 24.0, delta=0.5)  # 120/5
        c = sqlite3.connect(self.path)
        ds = c.execute("SELECT decode_since FROM inflight_requests WHERE id='a'").fetchone()[0]
        c.close()
        self.assertIsNotNone(ds)       # the decode start is kept

    def test_decode_start_kept_first_wins(self):
        self._insert('a', 0, 5)
        first = time.time() - 8
        with mock.patch.object(stats, 'DB_PATH', self.path):
            stats._inflight_tokens('a', 100, first)
            stats._inflight_tokens('a', 200, time.time())   # ne doit pas ecraser
        c = sqlite3.connect(self.path)
        ds = c.execute("SELECT decode_since FROM inflight_requests WHERE id='a'").fetchone()[0]
        c.close()
        self.assertAlmostEqual(ds, first, delta=0.01)

    def test_ratio_calibrates_on_exact_count(self):
        with mock.patch('db.get_setting') as get_s, mock.patch('db.set_setting') as set_s:
            vals = {}
            set_s.side_effect = lambda k, v: vals.__setitem__(k, v)
            get_s.side_effect = lambda k, d=None: vals.get(k, d)
            self.assertEqual(stats.ratio_chars_per_token(), 4.0)      # avant calibration
            stats.record_ratio(344, 100)                          # 3,44 car./token
            self.assertAlmostEqual(stats.ratio_chars_per_token(), 3.44, delta=0.01)
            stats.record_ratio(400, 100)                          # lissage 0.7/0.3
            self.assertAlmostEqual(stats.ratio_chars_per_token(),
                                   round(0.7 * 3.44 + 0.3 * 4.0, 3), delta=0.01)
            stats.record_ratio(10, 2)                             # echantillon trop petit : ignore
            self.assertAlmostEqual(stats.ratio_chars_per_token(),
                                   round(0.7 * 3.44 + 0.3 * 4.0, 3), delta=0.01)

    def test_last_prefill_is_fading_gauge(self):
        with mock.patch('db.set_setting') as set_s, mock.patch('db.get_setting') as get_s:
            vals = {}
            set_s.side_effect = lambda k, v: vals.__setitem__(k, v)
            get_s.side_effect = lambda k, d=None: vals.get(k, d)
            stats.record_prefill(1000, 4.0)         # 250 tok/s
            self.assertAlmostEqual(stats.prefill_last(), 250.0, delta=0.1)
            vals['prefill_rate_at'] = time.time() - 9999
            self.assertIsNone(stats.prefill_last())   # stale -> nothing, never 0

    def test_health_without_metrics_exposes_live_rate(self):
        with mock.patch.object(stats, 'live_decode_rate', return_value=12.5), \
             mock.patch.object(stats, 'prefill_last', return_value=250.0), \
             mock.patch.object(stats, 'flow_from_spendlogs', return_value={}), \
             mock.patch.object(stats, 'measured_ttft', return_value=None), \
             mock.patch.object(stats, 'accumulate_generated_tokens', return_value=1), \
             mock.patch.object(vllm_health, 'get_db') as gdb:
            gdb.return_value.execute.return_value.fetchone.return_value = None
            d = vllm_health._health_without_metrics('m', 'exllamav3')
        self.assertEqual(d['tps'], 12.5)
        self.assertEqual(d['tps_prefill'], 250.0)
        self.assertIs(d['metrics'], False)


if __name__ == '__main__':
    unittest.main()
