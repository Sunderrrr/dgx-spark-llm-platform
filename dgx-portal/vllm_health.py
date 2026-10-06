"""vLLM engine probe: served models, health, throughput, context window.

Extracted from app.py on 28/08, from the « OCR » banner which contained no
OCR code — the kind of misplaced boundary that made the monolith hard to
split.

get_running_models came along: it is a vLLM probe, it sat in « Helpers ».
app.py re-imports it, lots of code uses it.

_vllm_health_uncached reads model_configs to know max-num-seqs and the
active model's context window: hence the dependency on get_db.
"""
import os
import re
import time

import requests

from config import VLLM_API
from db import get_db

_rm_cache = {'t': 0.0, 'v': [], 'ok': 0.0}
# How long a FAILED probe keeps advertising the last known model list.
_RM_GRACE_S = 600

def get_running_models():
    """Model(s) served by the engine. Cached ~5 s to avoid hammering
    /v1/models on every page render and every poll (readable vLLM logs).

    A FAILED probe is NOT evidence that nothing runs — the house rule
    « missing data is not proof of absence » applies verbatim here. The
    engine's control plane FREEZES during a large prefill (measured
    2026-10-02: user prompts of 45k-149k tokens, and /v1/models unreachable
    in slices of ~15 s; the biggest held it minutes) — meanwhile every user
    read « aucun modèle actif » and could not chat at all although the model
    was fine and busy. The last known list is therefore kept for
    `_RM_GRACE_S` after the last SUCCESSFUL probe; past that, the engine is
    considered gone and the honest "no model" answer wins. A probe that
    succeeds with an empty list still clears it: that IS proof of absence.
    """
    now = time.time()
    if now - _rm_cache['t'] < 5:
        return _rm_cache['v']
    try:
        r = requests.get(f"{VLLM_API}/models", timeout=3)
        if not getattr(r, 'ok', True):
            raise RuntimeError(f"/v1/models a repondu {getattr(r, 'status_code', '?')}")
        v = [m['id'] for m in r.json().get('data', [])]
    except Exception:
        _rm_cache['t'] = now        # don't hammer a frozen engine every poll
        return list(_rm_cache['v']) if now - _rm_cache['ok'] < _RM_GRACE_S else []
    _rm_cache.update(t=now, v=v, ok=now)
    return v

_VLLM_METRICS_URL = VLLM_API.rsplit('/v1', 1)[0] + '/metrics'
_vllm_tps = {'t': 0.0, 'gen': 0.0}
# llama.cpp: last reading of n_decode_total + its timestamp, to derive an
# INSTANTANEOUS throughput. See the comment at the computation below: it is
# the only counter that advances during generation.
_llama_tps = {'t': 0.0, 'dec': None}

def _prom_sum(text, metric):
    """Sum of a Prometheus metric's samples (exact name, labels ignored)."""
    tot, found = 0.0, False
    for line in text.splitlines():
        if line.startswith(metric) and len(line) > len(metric) and line[len(metric)] in ' {':
            try:
                tot += float(line.rsplit(' ', 1)[1]); found = True
            except (ValueError, IndexError):
                pass
    return tot if found else None

_vllm_health_cache = {'t': 0.0, 'v': None}

# llama.cpp exposes /slots: which slot processes what, which task identifier,
# and how far prompt ingestion is. It is the ONLY activity source available
# DURING a request — LiteLLM only writes its row at the end (measured on
# 2026-09-14: 44 minutes without a single row while two sessions worked),
# and the engine does not know the client's identity. We thus display what
# it really knows rather than « nobody is using the model ».
_SLOTS_URL = VLLM_API.rsplit('/v1', 1)[0] + '/slots'
# llama.cpp does not say SINCE WHEN a task runs, only its identifier: we
# record the instant each identifier appeared. That gives « this session
# has been working for 12 min » and lets one tell a stuck request from a merely
# slow machine.
_slots_taches = {}


def _slots_activite():
    """What the engine is doing right now, session by session.

    Returns None when the engine publishes no /slots (vLLM) or does not
    answer: the UI then shows nothing rather than a zero that would mean
    « nobody », while it actually means « I do not know ».
    """
    try:
        slots = requests.get(_SLOTS_URL, timeout=4).json()
    except Exception:
        return None
    if not isinstance(slots, list):
        return None
    now = time.time()
    actifs = [s for s in slots if s.get('is_processing')]
    vus, plus_ancien, ingere, traite = set(), None, 0, 0
    for s in actifs:
        t = s.get('id_task')
        if t is None:
            continue
        vus.add(t)
        debut = _slots_taches.get(t, now)
        _slots_taches[t] = debut
        age = now - debut
        plus_ancien = age if plus_ancien is None else max(plus_ancien, age)
        ingere += int(s.get('n_prompt_tokens') or 0)
        traite += int(s.get('n_prompt_tokens_processed') or 0)
    # Cleanup: a task no longer processed leaves the tracking, else the table
    # would grow forever (it lives in memory, a restart empties it).
    for t in [t for t in _slots_taches if t not in vus]:
        _slots_taches.pop(t, None)
    return {
        'busy': len(actifs),
        'total': len(slots),
        'plus_ancien_s': round(plus_ancien) if plus_ancien is not None else None,
        'prompt_ingere': ingere,
        'prompt_traite': traite,
    }

def vllm_health():
    """Health of the active model (throughput tok/s, in-flight/queued requests, average TTFT).
    Cached ~4 s → a single /metrics scrape even with multiple polls.
    """
    now = time.time()
    if _vllm_health_cache['v'] is not None and now - _vllm_health_cache['t'] < 1:
        return _vllm_health_cache['v']
    out = _vllm_health_uncached()
    _vllm_health_cache.update(t=now, v=out)
    return out

# Both engines expose /metrics in Prometheus format, but with different
# names. We map both onto the same health dictionary.
_METRIC_NAMES = {
    'vllm': {
        'gen':      'vllm:generation_tokens_total',
        'running':  'vllm:num_requests_running',
        'waiting':  'vllm:num_requests_waiting',
        'requests': 'vllm:request_success_total',
        'ttft_sum': 'vllm:time_to_first_token_seconds_sum',
        'ttft_cnt': 'vllm:time_to_first_token_seconds_count',
    },
    'llamacpp': {
        'gen':      'llamacpp:tokens_predicted_total',
        'running':  'llamacpp:requests_processing',
        'waiting':  'llamacpp:requests_deferred',
        # llama.cpp publishes NO request counter. `n_decode_total`
        # counts tokens: displaying it as "served requests" announced 39 303
        # requests for 39 303 generated tokens. For lack of a source, we show nothing.
        'requests': None,
        # Presence of this key = "this engine has its own throughput computation"
        # (see below). The gauge itself is no longer used: it reads 0 during
        # generation, the throughput comes from n_decode_total.
        'speed':    'llamacpp:predicted_tokens_seconds',
        'ttft_sum': None,   # see below: the TTFT comes from a real measurement
        'ttft_cnt': None,   #   collected by chat_routes, not from /metrics.
    },
}

def _sante_sans_metrics(modele, engine):
    """Health dict for an engine that publishes NO /metrics (TabbyAPI).

    `metrics` stays False so the UI can say the figures do not come from the
    engine; what can be derived is derived from LiteLLM's SpendLogs
    (`stats.flux_depuis_spendlogs`): request count, generated/input tokens and
    a decode average as a RATIO OF SUMS. `tokens_generated` (since engine
    startup) stays None — nothing counts that — while `tokens_generated_total`
    keeps its cross-relaunch role via `cumuler_tokens_generes`. The TTFT is the
    one actually measured by chat_routes (same source as llama.cpp).
    """
    from stats import (cumuler_tokens_generes, debit_decode_live,
                       flux_depuis_spendlogs, prefill_dernier, ttft_mesure)
    flux = flux_depuis_spendlogs() or {}
    max_seqs = ctx_in = ctx_out = None
    try:
        row = get_db().execute("SELECT vllm_args FROM model_configs WHERE name=?",
                               (modele,)).fetchone()
        if row:
            max_seqs = max_seqs_of(row['vllm_args'], engine)
            ctx_in, ctx_out = ctx_split(row['vllm_args'], engine)
    except Exception:
        pass
    generes = flux.get('generes')
    cumul = None
    try:
        if generes is not None:
            cumul = cumuler_tokens_generes(modele, generes)
    except Exception:
        cumul = None
    return {
        'up': True,
        'model': modele,
        'engine': engine,
        'metrics': False,
        'running': None,
        'waiting': None,
        'max_seqs': max_seqs,
        'ctx_in': ctx_in,
        'ctx_out': ctx_out,
        # Live decode rate of the IN-FLIGHT requests (see
        # stats.debit_decode_live): the SpendLogs figure only lands at the end
        # of a request and read « 0 tok/s » while the model was generating.
        'tps': debit_decode_live(),
        'ttft': ttft_mesure(),
        'requests': flux.get('requetes'),
        'tokens_generated': None,
        'tokens_generated_total': cumul,
        'tps_moyen': flux.get('tps_moyen'),
        'tokens_prompt': flux.get('entree'),
        'tps_prefill': prefill_dernier(),
        'slots': _slots_activite(),
    }


def _vllm_health_uncached():
    running = get_running_models()
    if not running:
        return {'up': False, 'model': None}
    engine = 'vllm'
    try:
        row = get_db().execute("SELECT engine FROM model_configs WHERE name=?",
                               (running[0],)).fetchone()
        if row and row['engine']:
            engine = row['engine']
    except Exception:
        pass
    try:
        reponse = requests.get(_VLLM_METRICS_URL, timeout=4)
        # No raise_for_status: the /metrics probe is best-effort and its fakes
        # (tests) only carry `text`. TabbyAPI answers 404 with a body — that is
        # the "no /metrics" case, not a transport error.
        if getattr(reponse, 'status_code', 200) >= 400:
            return _sante_sans_metrics(running[0], engine)
        text = reponse.text
    except Exception:
        # Unreachable engine: fall back to the LiteLLM SpendLogs rather than
        # leaving the dashboard at "—".
        return _sante_sans_metrics(running[0], engine)
    M = _METRIC_NAMES.get(engine, _METRIC_NAMES['vllm'])
    gen = _prom_sum(text, M['gen']) or 0.0
    now = time.time()
    running_now = int(_prom_sum(text, M['running']) or 0)
    tps = None
    # If the engine publishes its own speed (llama.cpp), we take it directly.
    speed_metric = M.get('speed')  # presence = this engine publishes its own throughput
    if speed_metric:
        # INSTANTANEOUS throughput, aggregated over all sessions.
        #
        # Measured on this server: during an ongoing generation,
        # `predicted_tokens_seconds` (gauge) reads 0 and `tokens_predicted_total`
        # does NOT advance — llama.cpp only updates them at the end of the request.
        # Reading them thus gave 0 tok/s during the whole generation, then a
        # frozen figure in between: exactly the observed "static counter".
        # `n_decode_total` is the only one advancing continuously during generation.
        # BUT it counts DECODE STEPS, not tokens: llama.cpp batches the active
        # slots, so a step yields one token PER active slot. Reading it as-is
        # divided the displayed throughput by the number of sessions — measurement
        # of 2026-09-11: 17,4 displayed for 69,5 actually delivered to clients at 4
        # sessions. `requests_processing` is the correct factor, verified at 1, 2
        # and 4 sessions (34,0 / 50,5 / 69,5 against 33,9 / 50,5 / 69,5 real).
        # Beware: `n_busy_slots_per_decode` looks made for this but is an average
        # since startup (1,7 constantly), it does not fit.
        dec = _prom_sum(text, 'llamacpp:n_decode_total') or 0.0
        p_t, p_dec = _llama_tps['t'], _llama_tps['dec']
        if p_dec is not None and now > p_t and dec >= p_dec:
            pas = (dec - p_dec) / (now - p_t)
            # Nothing generated since the last reading => 0, the truth when nobody
            # uses the model. The `max(..., 1)` covers the window overlapping the END
            # of a generation: tokens were produced while the slot counter already
            # fell back to zero, without it we would wrongly display 0.
            tps = round(pas * max(running_now, 1), 1) if pas > 0 else 0.0
        else:
            tps = 0.0        # first reading of the process: nothing to compare
        _llama_tps.update(t=now, dec=dec)
    else:
        # vLLM: no instantaneous speed metric → cumulative delta/time.
        if _vllm_tps['t'] and now > _vllm_tps['t'] and gen >= _vllm_tps['gen']:
            tps = round((gen - _vllm_tps['gen']) / (now - _vllm_tps['t']), 1)
    _vllm_tps.update(t=now, gen=gen)
    ttft_sum = _prom_sum(text, M['ttft_sum']) if M.get('ttft_sum') else 0.0
    ttft_cnt = _prom_sum(text, M['ttft_cnt']) if M.get('ttft_cnt') else 0.0
    ttft_sum = ttft_sum or 0.0
    ttft_cnt = ttft_cnt or 0.0
    # llama.cpp publishes no TTFT in /metrics. But it attaches a `timings`
    # PER REQUEST to the last SSE fragment, which chat_routes picks up along
    # the way: a real end-to-end measurement, not an extrapolation. We prefer
    # it thus, and for lack of one we display NOTHING rather than a figure
    # computed on another basis — a TTFT reported at 1000 tokens is no TTFT.
    # Fallback reserved for engines with NO TTFT source (llama.cpp). Triggering
    # on `ttft_cnt == 0` would display, for a freshly launched vLLM that has
    # served nothing yet, a measurement inherited from the previous engine.
    # vLLM publishes its own histogram: we do not mix the two.
    ttft_mesure_s = None
    if M.get('ttft_cnt') is None:
        from stats import ttft_mesure
        ttft_mesure_s = ttft_mesure()
    # Concurrent generation slots of the active model (--max-num-seqs / --parallel)
    # → "X / N sessions busy" on the home page.
    max_seqs = None
    ctx_in = ctx_out = None
    try:
        row = get_db().execute("SELECT vllm_args FROM model_configs WHERE name=?",
                               (running[0],)).fetchone()
        if row:
            max_seqs = max_seqs_of(row['vllm_args'], engine)
            ctx_in, ctx_out = ctx_split(row['vllm_args'], engine)
    except Exception:
        pass
    # Cumulative counters. llama.cpp publishes enough to compute an EXACT
    # average since startup; vLLM only gives totals, hence no average.
    if engine == 'llamacpp':
        compteurs, cumul = _compteurs_llamacpp(text, running[0], gen)
    else:
        entree_v = _prom_sum(text, 'vllm:prompt_tokens_total')
        compteurs = {'generes': int(gen) if gen else 0, 'tps_moyen': None,
                     'entree': int(entree_v) if entree_v is not None else None,
                     'tps_prefill': None}
        try:
            from stats import cumuler_tokens_generes
            cumul = cumuler_tokens_generes(running[0], compteurs['generes'])
        except Exception:
            cumul = None
    return {
        'up': True,
        'model': running[0],
        'engine': engine,
        'metrics': True,
        'running': int(_prom_sum(text, M['running']) or 0),
        'waiting': int(_prom_sum(text, M['waiting']) or 0),
        'max_seqs': max_seqs,
        'ctx_in': ctx_in,
        'ctx_out': ctx_out,
        'tps': round(tps, 1) if tps is not None else None,
        'ttft': round(ttft_sum / ttft_cnt, 2) if ttft_cnt else ttft_mesure_s,
        'requests': (int(_prom_sum(text, M['requests']) or 0)
                     if M.get('requests') else None),
        # Cumulative counters since the engine STARTUP (see _compteurs_llamacpp).
        'tokens_generated': compteurs['generes'],
        'tokens_generated_total': cumul,
        'tps_moyen': compteurs['tps_moyen'],
        'tokens_prompt': compteurs['entree'],
        'tps_prefill': compteurs['tps_prefill'],
        # In-flight activity, session by session (see _slots_activite): this is
        # what allows saying « 2 sessions have been working for 12 min » when no
        # identity is logged yet.
        # vLLM publishes no /slots: the counters say HOW MANY sessions work and
        # how many the engine accepts — « je ne vois pas le nombre de session
        # dispo » was this hole. Fewer details than llama.cpp (no age, no
        # ingestion), but the number the reader wants is there.
        'slots': _slots_activite() or (
            {'busy': running_now, 'total': max_seqs, 'plus_ancien_s': None,
             'prompt_ingere': 0, 'prompt_traite': 0}
            if max_seqs else None),
    }


def _compteurs_llamacpp(text, modele, generes):
    """Engine cumulative counters, and the cumulative kept across its relaunches.

    Three readings, all exact because they come from engine counters and not
    from a portal-side sampling:

    - `tokens_predicted_total`: tokens generated since startup;
    - `tokens_predicted_seconds_total`: seconds spent generating. Their
      RATIO is thus a real decode average since startup (158 729 / 11 921,9
      = 13,3 tok/s on this server), stable where the instantaneous
      throughput jumps from 0 to 1,3 then falls back — this is what was
      missing to answer « the decoded tokens per second »;
    - `prompt_tokens_total` + the `prompt_tokens_seconds` gauge: the INPUT
      work. This explains the « 0 tok/s » screen while the GPU works:
      measured on 2026-09-14, 4 requests in flight, `n_decode_total`
      advancing by ONE step in 6 s, and a prefill at 248 tok/s. An agentic
      model re-reads huge contexts, so it spends most of its time in input;
      displaying « 0 » without saying so is exact but misleading.
    """
    vides = {'generes': None, 'tps_moyen': None, 'entree': None, 'tps_prefill': None}
    if not modele:
        return vides, None
    try:
        secondes = _prom_sum(text, 'llamacpp:tokens_predicted_seconds_total') or 0.0
        entree = _prom_sum(text, 'llamacpp:prompt_tokens_total')
        prefill = _prom_sum(text, 'llamacpp:prompt_tokens_seconds')
        compteurs = {
            'generes': int(generes) if generes else 0,
            # Threshold of 300 s of cumulative generation — a CHOICE, not a
            # measurement: over a few tens of seconds the average means nothing.
            # This is not theoretical: measured on 2026-09-14, the engine
            # counter went back to zero (KV cache reset, same pid), and the
            # « average » fell to 47 tokens / 192,4 s = 0,2 tok/s. Displaying it
            # would be worse than nothing, so the UI hides the line until the
            # engine has generated five minutes since its reset. The cumulative
            # total, for its part, stays right in every case.
            'tps_moyen': round(generes / secondes, 1) if secondes >= 300 else None,
            'entree': int(entree) if entree is not None else None,
            'tps_prefill': round(prefill, 1) if prefill else None,
        }
    except Exception:
        return vides, None
    # The cumulative that SURVIVES a relaunch: this is what « garder les
    # tokens générés » means. A read failure must not deprive the display: we
    # return the current launch's counter, without cumulative.
    try:
        from stats import cumuler_tokens_generes
        cumul = cumuler_tokens_generes(modele, compteurs['generes'])
    except Exception:
        cumul = None
    return compteurs, cumul

# HF tag carried by models actually tested on DGX Spark / GB10.
GB10_TAG = 'gb10'

def guess_engine(model):
    """Engine needed to serve this model, deduced from its HF tags.
    GGUF → llama.cpp; safetensors weights (NVFP4/FP8/BF16) → vLLM.
    """
    tags = {t.lower() for t in (model.get('tags') or [])}
    if 'gguf' in tags:
        return 'llamacpp'
    return 'vllm'

# Both engines express context and concurrency with different flags.
_CTX_FLAG  = {'vllm': 'max-model-len', 'llamacpp': 'ctx-size', 'ds4': 'ctx',
              'exllamav3': 'max-seq-len'}
_SEQS_FLAG = {'vllm': 'max-num-seqs',  'llamacpp': 'parallel',
              'exllamav3': 'max-batch-size'}

def _arg_int(args, flag, default=None):
    m = re.search(r'--' + re.escape(flag) + r'\s+(\d+)', args or '')
    return int(m.group(1)) if m else default

def ctx_of(args, engine='vllm'):
    """Configured context window (--max-model-len or --ctx-size)."""
    return _arg_int(args, _CTX_FLAG.get(engine or 'vllm', 'max-model-len'))

def max_seqs_of(args, engine='vllm'):
    """Configured concurrent sessions (--max-num-seqs or --parallel).
    ds4 has no parallelism setting: it allocates a single huge KV cache (1M)
    and serializes requests → 1 session, measured (2 requests = 2× the solo latency).
    """
    if engine == 'ds4':
        return 1
    n = _arg_int(args, _SEQS_FLAG.get(engine or 'vllm', 'max-num-seqs'))
    # llama.cpp serves 4 slots when --parallel is absent (checked on the
    # engine itself: « n_slots = 4 »). Without this fallback, the health panel
    # showed « 0 / — » instead of « 0 / 4 » without that flag.
    if n is None and (engine or 'vllm') == 'llamacpp':
        return 4
    return n

def effective_ctx(args, engine='vllm'):
    """Real usable context PER REQUEST (this is what we advertise to the client:
    LiteLLM, OpenCode, the Playground ring).

    Careful with llama.cpp: --ctx-size is the TOTAL context split across the slots,
    so a request only gets ctx-size ÷ --parallel. vLLM/ds4: --max-model-len
    / --ctx are already per request.

    EXCEPT with a unified cache: the slots then share a single pool and each
    can address the WHOLE context — dividing would underestimate it by that
    much. llama.cpp enables this mode by default when the slot count is
    automatic, and disables it as soon as --parallel is passed, unless
    --kv-unified is asked again. Measurement: `--ctx-size 524288 --parallel 6
    --kv-unified` announces n_ctx_slot = 524288, where the division showed 87381.

    SINCE llama.cpp 0.5.0 (`--kv-unified-per-slot N`) the per-session window
    is DECLARED, and it is then authoritative: the engine caps the slot at N
    and sizes the pool at n_parallel x N. Neither the division nor the
    « unified » rule above applies then. Measured on 2026-09-25 on
    Flash-Next: `--ctx-size 1048576 --parallel 4 --kv-unified
    --kv-unified-per-slot 262144` serves 262144 per session (announced on
    /props), where the « unified » rule would have announced 1048576 — five
    times the real prompt limit, discovered only at failure.
    """
    if engine == 'llamacpp':
        par_slot = _arg_int(args, 'kv-unified-per-slot', 0) or 0
        if par_slot > 0:
            return par_slot
    ctx = ctx_of(args, engine)
    if ctx is None:
        return None
    if engine == 'llamacpp':
        jetons = (args or '').split()
        if '--kv-unified' in jetons and '--no-kv-unified' not in jetons:
            return ctx
        par = _arg_int(args, 'parallel', 1) or 1
        return ctx // par
    return ctx

def ctx_split(vllm_args, engine='vllm'):
    """(input, output) split of the context advertised to clients — single
    source shared by LiteLLM (_register_litellm_model) AND the home page (vllm_health).

    llama.cpp / ds4: the KV slot is shared between prompt and generation, so we
    reserve an output margin capped at 64k. vLLM already separates input/output via
    --max-model-len. Cautious default of 32k if the context isn't declared.
    """
    slot = effective_ctx(vllm_args, engine) or 32768
    if engine in ('llamacpp', 'ds4', 'exllamav3'):
        # --n-predict IS the engine's output limit when the admin sets it: we
        # advertise it as such rather than guess it. Without that flag, we keep
        # the cautious heuristic (a third of the slot, capped at 64k).
        # The hardcoded cap should not decide instead of the model: Qwen
        # recommends up to 262144 reasoning tokens and 131072 final answer
        # tokens on Flash-Next, far beyond these 64k.
        demande = _arg_int(vllm_args, 'n-predict')
        out_reserve = (demande if demande and 0 < demande < slot
                       else min(65536, slot // 3))
        return max(slot - out_reserve, 1024), out_reserve
    return slot, min(slot // 2, 262144)

_SEARCH_PAGE_SIZE = 48

# HF tasks offered by the UI. Serve as an allow-list: an unknown filter
# makes HF answer 400, which is NOT an HF outage and must not be shown as
# one (see HfIndisponible). An EMPTY task is allowed: it is the default,
# see search_hf_models_page.
HF_TASKS = ('text-generation', 'text2text-generation', 'conversational',
            'feature-extraction', 'text-to-image', 'text-to-video',
            'image-to-text')

# Hugging Face token, OPTIONAL, re-read at every call (the file can be
# mounted after the portal starts). Without it the public API still
# answers, but two things are missing, measured on 2026-09-14:
#   - restricted-access (`gated`) repos do not appear in the search;
#   - the anonymous limit is 500 requests / 5 min per IP (HF's
#     `ratelimit-policy` header) — a search querying HF at every keystroke
#     can reach it, and a 429 would then display as a search failure.
# The token lives in ./secrets/hf_token (0600, git-ignored) and is mounted
# READ-ONLY in the portal. It is NEVER returned by a route nor logged:
# only its presence is (`hf_jeton_present`).
HF_TOKEN_FILE = os.environ.get('CRONOS_HF_TOKEN_FILE', '/run/secrets/hf_token')


def _hf_token():
    """The HF token, or ''. Never returned, never written to a log."""
    jeton = (os.environ.get('HF_TOKEN')
             or os.environ.get('HUGGING_FACE_HUB_TOKEN') or '').strip()
    if jeton:
        return jeton
    try:
        with open(HF_TOKEN_FILE, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def hf_jeton_present():
    """Is there an HF token? The UI may say so, the value never leaves."""
    return bool(_hf_token())


def _hf_headers():
    jeton = _hf_token()
    return {'Authorization': f'Bearer {jeton}'} if jeton else {}


class HfIndisponible(Exception):
    """Hugging Face did not answer with anything usable.

    Distinct from a search with no result: the portal must be able to say
    so. Before, every exception was swallowed and returned `[]`, so « HF is
    unreachable » displayed as « no model matches » — the user
    concluded their model does not exist."""


def _hf_page(params, timeout=8):
    """One HF call → (data, is there a next page). RAISES instead of returning empty.

    `has_more` comes from HF's `Link` header (`rel="next"`), which is
    authoritative. On the UI side, the old heuristic (« the page is full so
    there must be others ») showed a « Charger plus » button that could
    give nothing, and above all hid one when the last page was full.
    """
    try:
        r = requests.get('https://huggingface.co/api/models', params=params,
                         headers=_hf_headers(), timeout=timeout)
    except requests.RequestException as e:
        raise HfIndisponible(f"HF injoignable ({type(e).__name__})") from e
    if not r.ok:
        # A present but refused token is not an HF outage: saying so
        # helps understand it must be replaced, not waited on.
        if r.status_code in (401, 403) and _hf_token():
            raise HfIndisponible("jeton Hugging Face refusé")
        raise HfIndisponible(f"HF a répondu {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise HfIndisponible("réponse HF illisible") from e
    try:
        suivant = 'rel="next"' in (r.headers.get('Link') or '')
    except (TypeError, AttributeError):
        suivant = False
    return data, suivant


def _hf_models(params, timeout=8):
    """One HF call, which RAISES instead of returning an empty list (see above)."""
    return _hf_page(params, timeout)[0]


def search_hf_models_page(query, task=None, gb10_only=False, skip=0):
    """HF search → (models, next page?).

    **No default filter: we search ALL of Hugging Face.** Two filters were
    applied automatically and made existing models unfindable (reported on
    2026-09-14, the operator could not find « Ornith-1.5 »):

      - `task` defaulted to `text-generation`: a repo tagged only
        `image-text-to-text` (`Ornith-1.5`, a multimodal Qwen3.5) or not
        tagged at all was invisible;
      - the `gb10` tag only marks models tested on DGX Spark — a handful of
        repos — so everything else disappeared without anything saying so.
        It is a useful filter, but a filter one ASKS for.

    Sorting stays by decreasing downloads: without a query, the page thus
    shows the most used models of Hugging Face (or of the chosen task),
    which is the most honest way to « explore the catalog ».

    `full=true` adds `gated` (measured: +85 Kio, same response time). This
    field is worth the expense: a gated repo requires an HF token and fails
    the download AFTER the click on « Lancer » — exactly what happened to
    the Flash-Next mmproj. Seeing it in the results avoids discovering it
    at launch.
    """
    filters = ([task] if task else []) + ([GB10_TAG] if gb10_only else [])
    params = {'search': query, 'limit': _SEARCH_PAGE_SIZE, 'skip': max(0, int(skip)),
              'sort': 'downloads', 'direction': -1, 'full': 'true'}
    # Empty `filter` and missing `filter` do not mean the same thing on the HF
    # side (several `filter` = AND): we omit the key instead of sending an empty list.
    if filters:
        params['filter'] = filters
    out, has_more = _hf_page(params)
    for m in out:
        m['engine'] = guess_engine(m)
        # `siblings` (the repo file list) weighed 58 % of the response — measured:
        # 51 Kio out of 87 for 39 models — and NO screen uses it (the engine is
        # deduced from the `tags`, not the files). Removing it lightens every
        # search by more than half, on 48 models × ~15 files.
        # `_id` (Hugging Face internal identifier) goes with it, same reason.
        m.pop('siblings', None)
        m.pop('_id', None)
    return out, has_more


def search_hf_models(query, task=None, gb10_only=False, skip=0):
    """The list alone — the same call as `search_hf_models_page`, without `has_more`."""
    return search_hf_models_page(query, task, gb10_only, skip)[0]


def hf_modele_hors_gb10(query, task=None):
    """Are there models for this search WITHOUT the gb10 filter?

    Used to not leave the user in front of a misleading « aucun résultat »:
    when the GB10 filter gives nothing but HF knows some, the UI can say so
    and offer to uncheck the filter. Returns True, False, or None when we
    do not know — a doubt must not display as a « no ».
    """
    if not query:
        return None
    params = {'search': query, 'limit': 1}
    if task:
        params['filter'] = [task]
    try:
        return bool(_hf_models(params))
    except HfIndisponible:
        return None
