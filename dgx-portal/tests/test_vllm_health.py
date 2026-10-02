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


def _metrics_vllm(gen="1000", succes=("42", "8"), ttft=("12.5", "50")):
    """Rend un /metrics vLLM realiste : metriques etiquetees, lignes a sommer."""
    lignes = [
        'vllm:generation_tokens_total{model_name="m"} %s' % gen,
        'vllm:num_requests_running{model_name="m"} 2.0',
        'vllm:num_requests_waiting{model_name="m"} 1.0',
        'vllm:request_success_total{finished_reason="stop",model_name="m"} %s' % succes[0],
        'vllm:request_success_total{finished_reason="length",model_name="m"} %s' % succes[1],
        'vllm:time_to_first_token_seconds_sum{model_name="m"} %s' % ttft[0],
        'vllm:time_to_first_token_seconds_count{model_name="m"} %s' % ttft[1],
    ]
    return "\n".join(lignes) + "\n"


_METRICS_LLAMA = ("llamacpp:tokens_predicted_total 39080\n"
                  "llamacpp:tokens_predicted_seconds_total 1235.5\n"
                  "llamacpp:n_decode_total 39303\n"
                  "llamacpp:predicted_tokens_seconds 0\n"
                  "llamacpp:requests_processing 0\n"
                  "llamacpp:requests_deferred 0\n")


def _sante(engine, texte):
    """Calls vllm_health() pretending `engine` serves `texte`."""
    ligne = {'engine': engine}
    faux_db = types.SimpleNamespace(
        execute=lambda *a, **k: types.SimpleNamespace(fetchone=lambda: ligne))
    vllm_health._vllm_health_cache['t'] = 0.0
    with mock.patch.object(vllm_health, 'get_running_models', return_value=['m']), \
         mock.patch.object(vllm_health, 'get_db', return_value=faux_db), \
         mock.patch.object(vllm_health.requests, 'get',
                           return_value=types.SimpleNamespace(text=texte)):
        return vllm_health.vllm_health()


class SanteVllmTest(unittest.TestCase):
    """vLLM publishes everything needed: nothing of its display must move."""

    def setUp(self):
        vllm_health._vllm_tps.update(t=0.0, gen=0.0)

    def test_compteur_de_requetes_somme_les_etiquettes(self):
        d = _sante('vllm', _metrics_vllm())
        self.assertEqual(d['requests'], 50)      # 42 "stop" + 8 "length"
        self.assertEqual(d['running'], 2)
        self.assertEqual(d['waiting'], 1)

    def test_ttft_vient_de_l_histogramme_du_moteur(self):
        d = _sante('vllm', _metrics_vllm())
        self.assertEqual(d['ttft'], 0.25)        # 12.5 s / 50 requetes

    def test_debit_calcule_en_delta_sur_deux_releves(self):
        self.assertIsNone(_sante('vllm', _metrics_vllm("1000"))['tps'])
        time.sleep(1.05)
        tps = _sante('vllm', _metrics_vllm("1300"))['tps']
        self.assertIsNotNone(tps)
        self.assertTrue(250 < tps < 350, tps)    # ~300 tokens en ~1 s

    def test_vllm_sans_trafic_n_herite_pas_du_ttft_d_un_autre_moteur(self):
        """The « measured by the portal » fallback is reserved for llama.cpp.

        On `ttft_cnt == 0` it would display, for a just-launched vLLM, the
        TTFT left by the previous engine.
        """
        with mock.patch('stats.ttft_mesure', return_value=9.99):
            d = _sante('vllm', _metrics_vllm("0", succes=("0", "0"), ttft=("0", "0")))
        self.assertEqual(d['requests'], 0)
        self.assertIsNone(d['ttft'])


class SanteLlamacppTest(unittest.TestCase):
    """llama.cpp knows less: it must stay silent rather than invent."""

    def test_pas_de_compteur_de_requetes_invente(self):
        self.assertIsNone(_sante('llamacpp', _METRICS_LLAMA)['requests'])

    def test_ttft_repris_de_la_mesure_du_portail(self):
        with mock.patch('stats.ttft_mesure', return_value=0.27):
            self.assertEqual(_sante('llamacpp', _METRICS_LLAMA)['ttft'], 0.27)

    def test_debit_lu_sur_n_decode_total_le_seul_a_avancer(self):
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
        _sante('llamacpp', base + "llamacpp:n_decode_total 1000\n")
        time.sleep(1.05)
        tps = _sante('llamacpp', base + "llamacpp:n_decode_total 1030\n")['tps']
        self.assertTrue(52 < tps < 68, tps)      # 30 steps/s x 2 slots

    def test_debit_retombe_a_zero_quand_personne_ne_genere(self):
        """Counter still between two readings = nobody generates anymore.

        We displayed the last known throughput, which read as a frozen
        counter while the machine did nothing.
        """
        vllm_health._llama_tps.update(t=0.0, dec=None)
        base = ("llamacpp:tokens_predicted_total 39080\n"
                "llamacpp:tokens_predicted_seconds_total 1235.5\n"
                "llamacpp:predicted_tokens_seconds 0\n"
                "llamacpp:requests_deferred 0\n")
        actif = base + "llamacpp:requests_processing 1\n"
        repos = base + "llamacpp:requests_processing 0\n"
        _sante('llamacpp', actif + "llamacpp:n_decode_total 1000\n")
        time.sleep(1.05)
        # a generation happened: non-zero throughput
        self.assertTrue(_sante('llamacpp', actif + "llamacpp:n_decode_total 1030\n")['tps'] > 0)
        time.sleep(1.05)
        # the counter stopped moving: nobody uses the model
        self.assertEqual(_sante('llamacpp', repos + "llamacpp:n_decode_total 1030\n")['tps'], 0.0)


class ContexteEffectifTest(unittest.TestCase):
    """llama.cpp's `--ctx-size` is a TOTAL... except with a unified cache.

    The dashboard announced 58 254 / 29 127 where the engine served 524 288
    per slot: the division by `--parallel` does not apply when the slots
    share a single pool.
    """

    def test_sans_parallel_le_contexte_est_entier(self):
        self.assertEqual(vllm_health.effective_ctx("--ctx-size 524288", "llamacpp"), 524288)

    def test_parallel_seul_divise_le_contexte(self):
        # Without --kv-unified, llama.cpp partitions: each slot has its slice.
        self.assertEqual(
            vllm_health.effective_ctx("--ctx-size 1048576 --parallel 4", "llamacpp"), 262144)

    def test_kv_unified_annule_la_division(self):
        self.assertEqual(
            vllm_health.effective_ctx("--ctx-size 524288 --parallel 6 --kv-unified",
                                      "llamacpp"), 524288)

    def test_no_kv_unified_retablit_la_division(self):
        self.assertEqual(
            vllm_health.effective_ctx("--ctx-size 524288 --parallel 2 --no-kv-unified",
                                      "llamacpp"), 262144)

    def test_kv_unified_per_slot_declare_la_fenetre(self):
        """llama.cpp 0.5.0: the DECLARED per-session cap is authoritative.

        Measured on 2026-09-25 (Flash-Next): with `--ctx-size 1048576
        --parallel 4 --kv-unified --kv-unified-per-slot 262144` the engine
        serves 262144 per session — that is what it announces on /props. The
        « unifie » rule above would have announced 1048576, five times the
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

    def test_kv_unified_per_slot_sans_ctx_size(self):
        """The cap suffices on its own: the engine then sizes the pool.

        Without `--ctx-size`, the default formula finds nothing and fell
        back on 32 768 — a declared cap must answer before it.
        """
        self.assertEqual(
            vllm_health.effective_ctx(
                "--parallel 4 --kv-unified --kv-unified-per-slot 131072", "llamacpp"), 131072)

    def test_la_repartition_affichee_suit(self):
        """256k in input AND 256k in output, what the operator asked for."""
        e, s_ = vllm_health.ctx_split(
            "--ctx-size 524288 --parallel 6 --kv-unified --n-predict 262144", "llamacpp")
        self.assertEqual((e, s_), (262144, 262144))

    def test_vllm_nest_pas_concerne(self):
        self.assertEqual(vllm_health.effective_ctx("--max-model-len 32768", "vllm"), 32768)


class DebitAgregeTest(unittest.TestCase):
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

    def _mesure(self, pas_par_seconde, sessions):
        vllm_health._llama_tps.update(t=0.0, dec=None)
        _sante('llamacpp', self._metrics(1000, sessions))
        time.sleep(1.05)
        d = _sante('llamacpp', self._metrics(1000 + pas_par_seconde * 1.05, sessions))
        return d['tps']

    def test_une_seule_session_reste_inchangee(self):
        self.assertTrue(30 < self._mesure(34, 1) < 38)

    def test_quatre_sessions_multiplient_le_debit(self):
        """4 sessions at ~17 steps/s = ~69 tok/s delivered, not 17."""
        tps = self._mesure(17, 4)
        self.assertTrue(60 < tps < 76, tps)

    def test_au_repos_le_debit_est_nul(self):
        vllm_health._llama_tps.update(t=0.0, dec=None)
        _sante('llamacpp', self._metrics(1000, 0))
        time.sleep(1.05)
        self.assertEqual(_sante('llamacpp', self._metrics(1000, 0))['tps'], 0.0)

    def test_fin_de_generation_ne_tombe_pas_a_zero(self):
        """Window straddling the end: tokens produced, no active slot anymore.

        Without the guard the product would be zero and the throughput would blink.
        """
        vllm_health._llama_tps.update(t=0.0, dec=None)
        _sante('llamacpp', self._metrics(1000, 1))
        time.sleep(1.05)
        self.assertTrue(_sante('llamacpp', self._metrics(1030, 0))['tps'] > 0)


_METRICS_LLAMA_COMPLET = (
    "llamacpp:tokens_predicted_total 158729\n"
    "llamacpp:tokens_predicted_seconds_total 11921.9\n"
    "llamacpp:n_decode_total 132689\n"
    "llamacpp:prompt_tokens_total 1815000\n"
    "llamacpp:prompt_tokens_seconds 248.062\n"
    "llamacpp:predicted_tokens_seconds 0\n"
    "llamacpp:requests_processing 4\n"
    "llamacpp:requests_deferred 0\n")


class CompteursCumulesTest(unittest.TestCase):
    """What the engine can say about the work already done.

    Instantaneous throughput is not enough: measured on 2026-09-14, 4
    requests in flight, `n_decode_total` advancing by ONE step in 6 s (the
    model was ingesting input contexts) and a prefill at 248 tok/s. The
    screen thus announced « 0 tok/s » while the GPU worked. Hence the exact
    average since startup, and the input counters.
    """

    def test_les_compteurs_du_moteur_sont_exposes(self):
        with mock.patch('stats.cumuler_tokens_generes', return_value=999999):
            s = _sante('llamacpp', _METRICS_LLAMA_COMPLET)
        self.assertEqual(s['tokens_generated'], 158729)
        self.assertEqual(s['tokens_prompt'], 1815000)
        self.assertEqual(s['tps_prefill'], 248.1)
        self.assertEqual(s['tokens_generated_total'], 999999)

    def test_la_moyenne_est_exacte_et_pas_echantillonnee(self):
        """158 729 tokens in 11 921,9 s of generation = 13,3 tok/s.

        The ratio of two engine counters: no approximation tied to the
        portal's probe frequency, unlike the instantaneous throughput.
        """
        s = _sante('llamacpp', _METRICS_LLAMA_COMPLET)
        self.assertEqual(s['tps_moyen'], 13.3)

    def test_pas_de_moyenne_sur_un_compteur_qui_vient_de_repartir(self):
        """Fresh counter: the average would cover barely a few minutes.

        Measured on 2026-09-14: reset, 47 tokens and 192 s of generation →
        « 0,2 tok/s ». The 300 s threshold avoids displaying this meaningless
        figure; the cumulative total, for its part, stays right.
        """
        neuf = ("llamacpp:tokens_predicted_total 47\n"
                "llamacpp:tokens_predicted_seconds_total 192.443\n"
                "llamacpp:requests_processing 1\n")
        self.assertIsNone(_sante('llamacpp', neuf)['tps_moyen'])
        au_dessus = neuf.replace('192.443', '310.0')
        self.assertEqual(_sante('llamacpp', au_dessus)['tps_moyen'], 0.2)

    def test_vllm_na_pas_de_moyenne_a_inventer(self):
        """vLLM publishes no generation seconds: None, not a fake figure."""
        s = _sante('vllm', _metrics_vllm())
        self.assertIsNone(s['tps_moyen'])
        self.assertEqual(s['tokens_generated'], 1000)

    def test_un_echec_du_cumul_ne_casse_pas_la_sante(self):
        with mock.patch('stats.cumuler_tokens_generes', side_effect=RuntimeError('db')):
            s = _sante('llamacpp', _METRICS_LLAMA_COMPLET)
        self.assertIsNone(s['tokens_generated_total'])
        self.assertEqual(s['tokens_generated'], 158729)


class _FauxCurseur:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _FausseDb:
    """Minimal base: one row read, and the writes memorized to count them."""

    def __init__(self, row=None):
        self.row = row
        self.ecritures = []

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith('SELECT'):
            return _FauxCurseur(self.row)
        self.ecritures.append(params)
        self.row = {'base': params[1], 'dernier': params[2]}
        return _FauxCurseur(None)

    def commit(self):
        pass


class CumulTokensGeneresTest(unittest.TestCase):
    """« Garder les tokens générés »: the engine counter restarts from zero at
    every relaunch, so the cumulative must not fall with it — and above all
    never count the same launch twice."""

    def _cumul(self, db, valeur):
        with mock.patch.object(stats, 'get_db', return_value=db):
            return stats.cumuler_tokens_generes('modele', valeur)

    def test_premier_releve(self):
        self.assertEqual(self._cumul(_FausseDb(), 100), 100)

    def test_deux_releves_du_meme_lancement_ne_doublent_pas(self):
        db = _FausseDb()
        self._cumul(db, 100)
        self.assertEqual(self._cumul(db, 150), 150)

    def test_une_remise_a_zero_archive_la_valeur_deja_comptee(self):
        """Counter moving backwards: the engine restarted OR reset its cache to
        zero — measured on 2026-09-14, `tokens_predicted_total` from 158 864
        to 47 with the SAME pid, while `prompt_tokens_total` did not move.
        Both cases call for the same treatment: the work already done stays owed.
        """
        db = _FausseDb()
        self._cumul(db, 150)
        self.assertEqual(self._cumul(db, 20), 170)
        self.assertEqual(self._cumul(db, 30), 180)

    def test_le_cumul_ne_recule_jamais(self):
        db = _FausseDb()
        valeurs = [500, 800, 12, 40, 3, 60]
        cumuls = [self._cumul(db, v) for v in valeurs]
        self.assertEqual(cumuls, sorted(cumuls), cumuls)

    def test_aucune_ecriture_quand_rien_ne_bouge(self):
        """At rest the probe runs at 1 s: not one SQLite write per second."""
        db = _FausseDb()
        self._cumul(db, 100)
        self.assertEqual(len(db.ecritures), 1)
        self._cumul(db, 100)
        self._cumul(db, 100)
        self.assertEqual(len(db.ecritures), 1)

    def test_sans_modele_ni_valeur_rien_n_est_tente(self):
        db = _FausseDb()
        with mock.patch.object(stats, 'get_db', return_value=db):
            self.assertIsNone(stats.cumuler_tokens_generes('', 100))
            self.assertIsNone(stats.cumuler_tokens_generes('modele', None))
        self.assertEqual(db.ecritures, [])


_SLOTS_OCCUPES = [
    {'id': 0, 'id_task': 279, 'is_processing': True,
     'n_prompt_tokens': 22000, 'n_prompt_tokens_processed': 18000},
    {'id': 1, 'id_task': 915, 'is_processing': False, 'n_prompt_tokens': 0,
     'n_prompt_tokens_processed': 0},
    {'id': 2, 'id_task': 1334, 'is_processing': True,
     'n_prompt_tokens': 5000, 'n_prompt_tokens_processed': 5000},
    {'id': 3, 'id_task': 11, 'is_processing': False, 'n_prompt_tokens': 0,
     'n_prompt_tokens_processed': 0},
]


def _avec_slots(slots):
    """Makes /slots answer like llama.cpp, leaving /metrics to the given text."""
    def _get(url, **k):
        if url.endswith('/slots'):
            return types.SimpleNamespace(json=lambda: slots)
        return types.SimpleNamespace(text=_METRICS_LLAMA_COMPLET)
    return _get


class SlotsActifsTest(unittest.TestCase):
    """During a request, the engine is the ONLY source of activity.

    LiteLLM only writes its row at the end: measured on 2026-09-14, 44
    minutes without a single row while two sessions worked, hence a panel
    announcing « personne » while the GPU ran. These tests freeze what the
    portal can say despite that — and what it refuses to invent.
    """

    def setUp(self):
        vllm_health._slots_taches.clear()
        vllm_health._vllm_health_cache['t'] = 0.0

    def _activite(self, slots):
        with mock.patch.object(vllm_health.requests, 'get', side_effect=_avec_slots(slots)):
            return vllm_health._slots_activite()

    def test_seules_les_sessions_en_traitement_comptent(self):
        a = self._activite(_SLOTS_OCCUPES)
        self.assertEqual(a['busy'], 2)
        self.assertEqual(a['total'], 4)

    def test_l_ingestion_du_prompt_est_sommee(self):
        """« ou en est le prompt »: 23 000 of the 27 000 ingested tokens."""
        a = self._activite(_SLOTS_OCCUPES)
        self.assertEqual(a['prompt_ingere'], 27000)
        self.assertEqual(a['prompt_traite'], 23000)

    def test_l_age_est_suivi_par_identifiant_de_tache(self):
        """llama.cpp gives a task id, not a start time."""
        self._activite(_SLOTS_OCCUPES)
        self.assertIsNotNone(self._activite(_SLOTS_OCCUPES)['plus_ancien_s'])
        # Task seen 10 minutes ago: it is the age that must come out.
        vllm_health._slots_taches[279] = time.time() - 600
        a = self._activite(_SLOTS_OCCUPES)
        self.assertTrue(598 < a['plus_ancien_s'] < 604, a['plus_ancien_s'])

    def test_une_tache_terminee_sort_du_suivi(self):
        """Otherwise the task table would grow forever."""
        self._activite(_SLOTS_OCCUPES)
        self.assertIn(279, vllm_health._slots_taches)
        self._activite([s for s in _SLOTS_OCCUPES if s['id'] != 0])
        self.assertNotIn(279, vllm_health._slots_taches)
        self.assertIn(1334, vllm_health._slots_taches)

    def test_moteur_sans_slots_renvoie_none_et_pas_zero(self):
        """vLLM exposes no /slots, and a probe failure is not « personne »."""
        with mock.patch.object(vllm_health.requests, 'get',
                               side_effect=RuntimeError('injoignable')):
            self.assertIsNone(vllm_health._slots_activite())

    def test_la_sante_porte_l_activite_des_slots(self):
        def _get(url, **k):
            if url.endswith('/slots'):
                return types.SimpleNamespace(json=lambda: _SLOTS_OCCUPES)
            return types.SimpleNamespace(text=_METRICS_LLAMA_COMPLET)
        ligne = {'engine': 'llamacpp'}
        faux_db = types.SimpleNamespace(
            execute=lambda *a, **k: types.SimpleNamespace(fetchone=lambda: ligne))
        vllm_health._vllm_health_cache['t'] = 0.0
        with mock.patch.object(vllm_health, 'get_running_models', return_value=['m']), \
             mock.patch.object(vllm_health, 'get_db', return_value=faux_db), \
             mock.patch.object(vllm_health.requests, 'get', side_effect=_get):
            h = vllm_health.vllm_health()
        self.assertEqual(h['slots']['busy'], 2)


class EnVolStatsTest(unittest.TestCase):
    """Reading the IN-FLIGHT requests written by the LiteLLM callback.

    This file is the only OBSERVED attribution: without it, the panel can
    only say what the engine sees (busy sessions) and what SpendLogs has
    already seen (finished requests). It is also, by construction, a file
    that can be missing or unreadable — hence these tests.
    """

    def setUp(self):
        self.dossier = tempfile.mkdtemp()
        self.chemin = os.path.join(self.dossier, 'inflight.db')
        self._avant = stats._EN_VOL_DB
        stats._EN_VOL_DB = self.chemin

    def tearDown(self):
        stats._EN_VOL_DB = self._avant
        shutil.rmtree(self.dossier, ignore_errors=True)

    def _ecrit(self, lignes):
        conn = sqlite3.connect(self.chemin)
        conn.execute('CREATE TABLE IF NOT EXISTS en_vol (cle TEXT PRIMARY KEY, alias TEXT, '
                     'user_id TEXT, modele TEXT, debut REAL NOT NULL)')
        for cle, alias, user_id, age in lignes:
            conn.execute('INSERT INTO en_vol VALUES (?,?,?,?,?)',
                         (cle, alias, user_id, 'm', time.time() - age))
        conn.commit()
        conn.close()

    def test_un_alias_connu_donne_le_compte(self):
        with mock.patch.object(stats, '_compte_existe', side_effect=lambda n: n == 'alice'):
            self._ecrit([('c1', 'Opencode-alice', '', 12)])
            actifs = stats._en_vol()
        self.assertEqual(list(actifs), ['alice'])
        self.assertTrue(11 < actifs['alice'] < 15, actifs)

    def test_un_alias_inconnu_ne_devine_aucun_nom(self):
        """Better to display nothing than an invented name."""
        with mock.patch.object(stats, '_compte_existe', return_value=False):
            self._ecrit([('c1', 'Opencode-Omarchy', 'personne-connu', 5)])
            self.assertEqual(stats._en_vol(), {})

    def test_le_user_id_sert_de_repli(self):
        with mock.patch.object(stats, '_compte_existe', side_effect=lambda n: n == 'bob'):
            self._ecrit([('c1', '', 'bob', 3)])
            self.assertEqual(list(stats._en_vol()), ['bob'])

    def test_la_plus_ancienne_requete_gagne(self):
        with mock.patch.object(stats, '_compte_existe', side_effect=lambda n: n == 'alice'):
            self._ecrit([('c1', 'alice-1', '', 2), ('c2', 'alice-2', '', 300)])
            self.assertTrue(stats._en_vol()['alice'] > 290)

    def test_fichier_absent_ou_illisible_ne_leve_pas(self):
        self.assertEqual(stats._en_vol(), {})          # absent
        with open(self.chemin, 'w') as f:
            f.write('ceci n est pas une base sqlite')
        with mock.patch.object(stats, '_compte_existe', return_value=True):
            self.assertEqual(stats._en_vol(), {})      # illisible


if __name__ == '__main__':
    unittest.main()
