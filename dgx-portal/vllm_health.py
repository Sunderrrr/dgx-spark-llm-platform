"""Sonde du moteur vLLM : modeles servis, sante, debit, fenetre de contexte.

Extrait de app.py le 28/08, depuis la banniere « OCR » qui ne contenait aucun
code OCR — c'est ce genre de frontiere mal placee qui rendait le monolithe
difficile a decouper.

get_running_models est venu avec : c'est une sonde vLLM, elle etait rangee dans
« Helpers ». app.py la reimporte, beaucoup de code s'en sert.

_vllm_health_uncached lit model_configs pour connaitre max-num-seqs et la
fenetre de contexte du modele actif : d'ou la dependance a get_db.
"""
import re
import time

import requests

from config import VLLM_API
from db import get_db

_rm_cache = {'t': 0.0, 'v': []}

def get_running_models():
    """Model(s) served by vLLM. Cached ~5 s to avoid hammering
    /v1/models on every page render and every poll (readable vLLM logs).
    """
    now = time.time()
    if now - _rm_cache['t'] < 5:
        return _rm_cache['v']
    v = []
    try:
        r = requests.get(f"{VLLM_API}/models", timeout=3)
        if r.ok:
            v = [m['id'] for m in r.json().get('data', [])]
    except Exception:
        pass
    _rm_cache.update(t=now, v=v)
    return v

_VLLM_METRICS_URL = VLLM_API.rsplit('/v1', 1)[0] + '/metrics'
_vllm_tps = {'t': 0.0, 'gen': 0.0}
# llama.cpp : dernier releve des deux compteurs cumules, pour en tirer un debit
# GLISSANT. Le ratio cumule brut est une moyenne depuis le demarrage : au bout de
# quelques heures il ne bouge plus (fige a 36 tok/s) et n'informe plus de rien.
_llama_tps = {'tok': None, 'sec': None, 'last': None}

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
        'requests': 'llamacpp:n_decode_total',
        # llama.cpp exposes its generation speed directly; we use it
        # as-is instead of a tokens/wall-clock delta, which strongly overestimates
        # (it divides a batch of tokens by a short scrape
        # interval → "57 tok/s" where the engine actually does 8.5).
        'speed':    'llamacpp:predicted_tokens_seconds',
        # Compteurs CUMULES : la jauge `speed` se fige au repos, ces deux-la non.
        # Leur rapport donne le debit moyen reel, affichable en permanence.
        'gen_sec':  'llamacpp:tokens_predicted_seconds_total',
        'ttft_sum': None,   # cf. plus bas : le TTFT vient d'une mesure reelle
        'ttft_cnt': None,   #   relevee par chat_routes, pas de /metrics.
    },
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
        text = requests.get(_VLLM_METRICS_URL, timeout=4).text
    except Exception:
        return {'up': True, 'model': running[0], 'engine': engine, 'metrics': False}
    M = _METRIC_NAMES.get(engine, _METRIC_NAMES['vllm'])
    gen = _prom_sum(text, M['gen']) or 0.0
    now = time.time()
    running_now = int(_prom_sum(text, M['running']) or 0)
    tps = None
    # If the engine publishes its own speed (llama.cpp), we take it directly.
    speed_metric = M.get('speed')
    if speed_metric:
        # Debit de la FENETRE ecoulee : (tokens produits) / (temps passe a les
        # produire) depuis le releve precedent. Les deux compteurs n'avancent que
        # pendant une generation, donc le ratio reste un vrai debit meme si la
        # fenetre contient surtout du repos — contrairement a un delta/temps mural.
        tot_tok = _prom_sum(text, M['gen']) or 0.0
        tot_sec = (_prom_sum(text, M.get('gen_sec')) or 0.0) if M.get('gen_sec') else 0.0
        p_tok, p_sec = _llama_tps['tok'], _llama_tps['sec']
        if p_tok is not None and tot_tok >= p_tok and tot_sec > p_sec:
            _llama_tps['last'] = round((tot_tok - p_tok) / (tot_sec - p_sec), 1)
        _llama_tps.update(tok=tot_tok, sec=tot_sec)

        if running_now > 0:
            # predicted_tokens_seconds is a GAUGE that KEEPS the speed of the
            # last generation: at rest it would stay frozen ("stuck at 8").
            # We therefore only show it if there is actually a generation in progress.
            v = _prom_sum(text, speed_metric)
            tps = round(v, 1) if v else (_llama_tps['last'] or 0.0)
        elif _llama_tps['last'] is not None:
            # Au repos : debit de la derniere generation observee, pas 0 (les
            # generations sont courtes, on ne tombait quasiment jamais dessus).
            tps = _llama_tps['last']
        else:
            # Premier releve : rien a comparer, moyenne cumulee en attendant.
            tps = round(tot_tok / tot_sec, 1) if tot_sec else 0.0
    else:
        # vLLM: no instantaneous speed metric → cumulative delta/time.
        if _vllm_tps['t'] and now > _vllm_tps['t'] and gen >= _vllm_tps['gen']:
            tps = round((gen - _vllm_tps['gen']) / (now - _vllm_tps['t']), 1)
    _vllm_tps.update(t=now, gen=gen)
    ttft_sum = _prom_sum(text, M['ttft_sum']) if M.get('ttft_sum') else 0.0
    ttft_cnt = _prom_sum(text, M['ttft_cnt']) if M.get('ttft_cnt') else 0.0
    ttft_sum = ttft_sum or 0.0
    ttft_cnt = ttft_cnt or 0.0
    # llama.cpp ne publie aucun TTFT dans /metrics. Mais il joint un `timings`
    # PAR REQUETE au dernier fragment SSE, que chat_routes releve au passage :
    # c'est une vraie mesure de bout en bout, pas une extrapolation. On la prefere
    # donc, et faute de mesure on n'affiche RIEN plutot qu'un chiffre calcule sur
    # une autre base — un TTFT rapporte a 1000 tokens n'est pas un TTFT.
    ttft_mesure_s = None
    if not ttft_cnt:
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
        'requests': int(_prom_sum(text, M['requests']) or 0),
    }

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
_CTX_FLAG  = {'vllm': 'max-model-len', 'llamacpp': 'ctx-size', 'ds4': 'ctx'}
_SEQS_FLAG = {'vllm': 'max-num-seqs',  'llamacpp': 'parallel'}

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
    # llama.cpp sert 4 slots quand --parallel est absent (verifie sur le moteur
    # lui-meme : « n_slots = 4 »). Sans ce repli, le panneau de sante affichait
    # « 0 / — » au lieu de « 0 / 4 » des qu'un modele etait lance sans ce drapeau.
    if n is None and (engine or 'vllm') == 'llamacpp':
        return 4
    return n

def effective_ctx(args, engine='vllm'):
    """Real usable context PER REQUEST (this is what we advertise to the client:
    LiteLLM, OpenCode, the Playground ring).

    Careful with llama.cpp: --ctx-size is the TOTAL context split across the slots,
    so a request only gets ctx-size ÷ --parallel. vLLM/ds4: --max-model-len
    / --ctx are already per request.
    """
    ctx = ctx_of(args, engine)
    if ctx is None:
        return None
    if engine == 'llamacpp':
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
    if engine in ('llamacpp', 'ds4'):
        # --n-predict EST la limite de sortie du moteur quand l'admin la fixe :
        # on l'annonce telle quelle plutot que de la deviner. Sans ce drapeau, on
        # garde l'heuristique prudente (un tiers du slot, plafonne a 64k).
        # Le plafond code en dur ne devrait pas decider a la place du modele :
        # Qwen recommande jusqu'a 262144 tokens de raisonnement et 131072 de
        # reponse finale sur Flash-Next, tres au-dela de ces 64k.
        demande = _arg_int(vllm_args, 'n-predict')
        out_reserve = (demande if demande and 0 < demande < slot
                       else min(65536, slot // 3))
        return max(slot - out_reserve, 1024), out_reserve
    return slot, min(slot // 2, 262144)

_SEARCH_PAGE_SIZE = 48

def search_hf_models(query, task='text-generation', gb10_only=True, skip=0):
    """HF search. By default, restricted to models tagged `gb10` — that is,
    the ones actually tested on DGX Spark. Multiple `filter` = AND on the HF API side.

    Paginated (skip, page of _SEARCH_PAGE_SIZE): the gb10 tag alone already returns
    80+ models for text-generation, invisible beyond the old fixed
    limit of 24 with no way to go further — reported in real use.
    """
    filters = [task] if task else []
    if gb10_only:
        filters.append(GB10_TAG)
    try:
        r = requests.get(
            'https://huggingface.co/api/models',
            params={'search': query, 'filter': filters, 'limit': _SEARCH_PAGE_SIZE,
                    'skip': max(0, int(skip)), 'sort': 'downloads', 'direction': -1},
            timeout=8
        )
        if r.ok:
            out = r.json()
            for m in out:
                m['engine'] = guess_engine(m)
            return out
    except Exception:
        pass
    return []
