"""Sonde du moteur vLLM : modeles servis, sante, debit, fenetre de contexte.

Extrait de app.py le 28/08, depuis la banniere « OCR » qui ne contenait aucun
code OCR — c'est ce genre de frontiere mal placee qui rendait le monolithe
difficile a decouper.

get_running_models est venu avec : c'est une sonde vLLM, elle etait rangee dans
« Helpers ». app.py la reimporte, beaucoup de code s'en sert.

_vllm_health_uncached lit model_configs pour connaitre max-num-seqs et la
fenetre de contexte du modele actif : d'ou la dependance a get_db.
"""
import os
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
# llama.cpp : dernier releve de n_decode_total + son horodatage, pour en tirer un
# debit INSTANTANE. Voir le commentaire du calcul plus bas : c'est le seul
# compteur qui avance pendant la generation.
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

# llama.cpp expose /slots : quel slot traite, quel identifiant de tache, et ou en
# est l'ingestion du prompt. C'est la SEULE source d'activite disponible PENDANT
# une requete — LiteLLM n'ecrit sa ligne qu'a la fin (mesure du 2026-09-14 : 44
# minutes sans la moindre ligne alors que deux sessions travaillaient), et le
# moteur, lui, ne connait pas l'identite du client. On affiche donc ce qu'il sait
# vraiment plutot que « personne n'utilise le modele ».
_SLOTS_URL = VLLM_API.rsplit('/v1', 1)[0] + '/slots'
# llama.cpp ne dit pas DEPUIS QUAND une tache tourne, seulement son identifiant :
# on retient l'instant ou chaque identifiant est apparu. Cela donne « cette
# session travaille depuis 12 min » et permet de distinguer une requete bloquee
# d'une machine simplement lente.
_slots_taches = {}


def _slots_activite():
    """Ce que le moteur fait maintenant, session par session.

    Retourne None quand le moteur ne publie pas /slots (vLLM) ou ne repond pas :
    l'interface n'affiche alors rien plutot qu'un zero qui voudrait dire
    « personne », alors que c'est « je ne sais pas ».
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
    # Menage : une tache qui n'est plus traitee sort du suivi, sinon la table
    # grossirait sans fin (elle vit en memoire, un redemarrage la vide).
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
        # llama.cpp ne publie AUCUN compteur de requetes. `n_decode_total`
        # comptait des tokens : l'afficher en "requetes servies" annoncait 39 303
        # requetes pour 39 303 tokens generes. Faute de source, on n'affiche rien.
        'requests': None,
        # Presence de cette cle = "ce moteur a son propre calcul de debit"
        # (cf. plus bas). La jauge elle-meme ne sert plus : elle vaut 0 pendant
        # la generation, le debit est tire de n_decode_total.
        'speed':    'llamacpp:predicted_tokens_seconds',
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
    speed_metric = M.get('speed')  # présence = ce moteur publie son propre débit
    if speed_metric:
        # Debit INSTANTANE, agrege sur toutes les sessions.
        #
        # Mesure faite sur ce serveur : pendant une generation en cours,
        # `predicted_tokens_seconds` (jauge) vaut 0 et `tokens_predicted_total`
        # n'avance PAS — llama.cpp ne les met a jour qu'a la fin de la requete.
        # Les lire donnait donc 0 tok/s pendant toute la generation, puis un
        # chiffre fige entre deux : exactement le "compteur statique" constate.
        # `n_decode_total` est le seul a avancer en continu pendant la generation.
        # MAIS il compte des PAS DE DECODAGE, pas des tokens : llama.cpp batche les
        # slots actifs, donc un pas produit un token PAR SLOT actif. Le lire tel quel
        # divisait le debit affiche par le nombre de sessions — mesure du 2026-09-11 :
        # 17,4 affiche pour 69,5 reellement delivres aux clients a 4 sessions.
        # `requests_processing` est le facteur correct, verifie a 1, 2 et 4 sessions
        # (34,0 / 50,5 / 69,5 contre 33,9 / 50,5 / 69,5 reels). Attention :
        # `n_busy_slots_per_decode` a l'air fait pour ca mais c'est une moyenne
        # depuis le demarrage (1,7 en permanence), elle ne convient pas.
        dec = _prom_sum(text, 'llamacpp:n_decode_total') or 0.0
        p_t, p_dec = _llama_tps['t'], _llama_tps['dec']
        if p_dec is not None and now > p_t and dec >= p_dec:
            pas = (dec - p_dec) / (now - p_t)
            # Rien de genere depuis le dernier releve => 0, la verite quand personne
            # n'utilise le modele. Le `max(..., 1)` couvre la fenetre qui chevauche la
            # FIN d'une generation : des tokens ont ete produits alors que le compteur
            # de slots est deja retombe a zero, sans lui on afficherait 0 a tort.
            tps = round(pas * max(running_now, 1), 1) if pas > 0 else 0.0
        else:
            tps = 0.0        # premier releve du process : rien a comparer
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
    # llama.cpp ne publie aucun TTFT dans /metrics. Mais il joint un `timings`
    # PAR REQUETE au dernier fragment SSE, que chat_routes releve au passage :
    # c'est une vraie mesure de bout en bout, pas une extrapolation. On la prefere
    # donc, et faute de mesure on n'affiche RIEN plutot qu'un chiffre calcule sur
    # une autre base — un TTFT rapporte a 1000 tokens n'est pas un TTFT.
    # Repli reserve aux moteurs qui n'ont AUCUNE source de TTFT (llama.cpp). Le
    # declencher sur `ttft_cnt == 0` afficherait, pour un vLLM fraichement lance
    # n'ayant encore rien servi, une mesure heritee du moteur precedent. vLLM
    # publie son propre histogramme : on ne s'en melange pas.
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
    # Compteurs cumulés. llama.cpp publie de quoi calculer une moyenne EXACTE
    # depuis son démarrage ; vLLM ne donne que des totaux, donc pas de moyenne.
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
        # Compteurs cumulés depuis le DÉMARRAGE du moteur (cf. _compteurs_llamacpp).
        'tokens_generated': compteurs['generes'],
        'tokens_generated_total': cumul,
        'tps_moyen': compteurs['tps_moyen'],
        'tokens_prompt': compteurs['entree'],
        'tps_prefill': compteurs['tps_prefill'],
        # Activite en vol, session par session (cf. _slots_activite) : c'est ce
        # qui permet de dire « 2 sessions travaillent depuis 12 min » quand
        # aucune identite n'est encore journalisee.
        'slots': _slots_activite(),
    }


def _compteurs_llamacpp(text, modele, generes):
    """Compteurs cumulés du moteur, et cumul conservé à travers ses relances.

    Trois lectures, toutes exactes parce qu'elles viennent de compteurs du moteur
    et non d'un échantillonnage par le portail :

    - `tokens_predicted_total` : tokens générés depuis le démarrage ;
    - `tokens_predicted_seconds_total` : secondes passées à générer. Leur
      RAPPORT est donc une vraie moyenne de décodage depuis le démarrage
      (158 729 / 11 921,9 = 13,3 tok/s sur ce serveur), stable là où le débit
      instantané saute de 0 à 1,3 puis retombe — c'est ce qui manquait pour
      répondre à « les tokens par seconde décodés » ;
    - `prompt_tokens_total` + la jauge `prompt_tokens_seconds` : le travail
      d'ENTRÉE. C'est ce qui explique l'écran « 0 tok/s » alors que le GPU
      travaille : mesuré le 2026-09-14, 4 requêtes en cours, `n_decode_total`
      qui avance d'UN pas en 6 s, et un prefill à 248 tok/s. Un modèle agentique
      relit des contextes énormes, donc il passe l'essentiel de son temps en
      entrée ; afficher « 0 » sans le dire est exact mais trompeur.
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
            # Seuil de 300 s de génération cumulée — un CHOIX, pas une mesure :
            # sur quelques dizaines de secondes la moyenne ne veut rien dire.
            # Ce n'est pas théorique : mesuré le 2026-09-14, le compteur du
            # moteur est reparti à zéro (cache KV remis à zéro, même pid), et la
            # « moyenne » est tombée à 47 tokens / 192,4 s = 0,2 tok/s. L'afficher
            # serait pire que ne rien afficher, donc l'interface masque la ligne
            # tant que le moteur n'a pas généré cinq minutes depuis sa remise à
            # zéro. Le total cumulé, lui, reste juste dans tous les cas.
            'tps_moyen': round(generes / secondes, 1) if secondes >= 300 else None,
            'entree': int(entree) if entree is not None else None,
            'tps_prefill': round(prefill, 1) if prefill else None,
        }
    except Exception:
        return vides, None
    # Le cumul qui SURVIT à une relance : c'est ce que veut dire « garder les
    # tokens générés ». Un échec de lecture ne doit pas priver d'affichage : on
    # rend le compteur du lancement en cours, sans cumul.
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

    SAUF en cache unifie : les slots partagent alors un reservoir unique et chacun
    peut adresser le contexte ENTIER — diviser sous-estimerait d'autant. llama.cpp
    active ce mode par defaut quand le nombre de slots est automatique, et le
    desactive des qu'on passe --parallel, a moins de redemander --kv-unified.
    Mesure : `--ctx-size 524288 --parallel 6 --kv-unified` annonce
    n_ctx_slot = 524288, alors que la division affichait 87381.

    DEPUIS llama.cpp 0.5.0 (`--kv-unified-per-slot N`) la fenetre par session se
    DECLARE, et c'est alors elle qui fait foi : le moteur plafonne le slot a N et
    dimensionne le reservoir a n_parallel x N. Ni la division ni la regle
    « unifie » ci-dessus ne s'appliquent alors. Mesure du 2026-09-25 sur
    Flash-Next : `--ctx-size 1048576 --parallel 4 --kv-unified
    --kv-unified-per-slot 262144` sert 262144 par session (le moteur l'annonce
    sur /props), la ou la regle « unifie » aurait annonce 1048576 — cinq fois la
    limite reelle d'un prompt, que le client n'aurait decouvert qu'a l'echec.
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

# Tâches HF proposées par l'interface. Servent d'allow-list : un filtre inconnu
# fait répondre 400 à HF, ce qui n'est PAS une panne de HF et ne doit donc pas
# être présenté comme telle (cf. HfIndisponible). Une tâche VIDE est permise :
# c'est le défaut, cf. search_hf_models_page.
HF_TASKS = ('text-generation', 'text2text-generation', 'conversational',
            'feature-extraction', 'text-to-image', 'text-to-video',
            'image-to-text')

# Jeton Hugging Face, OPTIONNEL, relu à chaque appel (le fichier peut être monté
# après le démarrage du portail). Sans lui l'API publique répond quand même, mais
# deux choses manquent, mesurées le 2026-09-14 :
#   - les dépôts à accès restreint (`gated`) n'apparaissent pas dans la recherche ;
#   - la limite anonyme est de 500 requêtes / 5 min par IP (en-tête
#     `ratelimit-policy` de HF) — une recherche qui interroge HF à chaque frappe
#     peut l'atteindre, et un 429 s'afficherait alors comme un échec de recherche.
# Le jeton vit dans ./secrets/hf_token (0600, git-ignoré) et est monté en LECTURE
# SEULE dans le portail. Il n'est JAMAIS renvoyé par une route ni journalisé :
# seule sa présence l'est (`hf_jeton_present`).
HF_TOKEN_FILE = os.environ.get('CRONOS_HF_TOKEN_FILE', '/run/secrets/hf_token')


def _hf_token():
    """Le jeton HF, ou ''. Jamais renvoyé, jamais écrit dans un journal."""
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
    """Y a-t-il un jeton HF ? L'interface peut le dire, la valeur ne sort pas."""
    return bool(_hf_token())


def _hf_headers():
    jeton = _hf_token()
    return {'Authorization': f'Bearer {jeton}'} if jeton else {}


class HfIndisponible(Exception):
    """Hugging Face n'a pas répondu exploitablement.

    Distinct d'une recherche sans résultat : le portail doit pouvoir le dire.
    Avant, toute exception était avalée et renvoyait `[]`, donc « HF est
    injoignable » s'affichait comme « aucun modèle ne correspond » — l'utilisateur
    concluait que son modèle n'existe pas."""


def _hf_page(params, timeout=8):
    """Un appel HF → (données, y a-t-il une page suivante). LÈVE au lieu de renvoyer vide.

    `has_more` vient de l'en-tête `Link` de HF (`rel="next"`), qui fait foi.
    Côté interface, l'ancienne heuristique (« la page est pleine donc il y en a
    d'autres ») affichait un bouton « Charger plus » qui pouvait ne rien donner,
    et surtout en cachait un quand la dernière page était pleine.
    """
    try:
        r = requests.get('https://huggingface.co/api/models', params=params,
                         headers=_hf_headers(), timeout=timeout)
    except requests.RequestException as e:
        raise HfIndisponible(f"HF injoignable ({type(e).__name__})") from e
    if not r.ok:
        # Un jeton présent mais refusé n'est pas une panne de HF : le dire
        # permet de comprendre qu'il faut le remplacer, pas attendre.
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
    """Un appel HF, qui LÈVE au lieu de renvoyer une liste vide (cf. ci-dessus)."""
    return _hf_page(params, timeout)[0]


def search_hf_models_page(query, task=None, gb10_only=False, skip=0):
    """Recherche HF → (modèles, page suivante ?).

    **Aucun filtre par défaut : on cherche dans TOUT Hugging Face.** Deux filtres
    étaient appliqués d'office et rendaient introuvables des modèles qui existent
    (signalé le 2026-09-14, l'opérateur ne trouvait pas « Ornith-1.5 ») :

      - `task` valait `text-generation` : un dépôt taggé seulement
        `image-text-to-text` (`Ornith-1.5`, un Qwen3.5 multimodal) ou pas taggé du
        tout était invisible ;
      - le tag `gb10` ne marque que les modèles testés sur DGX Spark — une poignée
        de dépôts — donc tout le reste disparaissait sans que rien ne le dise.
        C'est un filtre utile, mais un filtre qu'on DEMANDE.

    Le tri reste par téléchargements décroissants : sans requête, la page montre
    donc les modèles les plus utilisés de Hugging Face (ou de la tâche choisie),
    ce qui est la façon la plus honnête d'« explorer le catalogue ».

    `full=true` ajoute `gated` (mesuré : +85 Kio, même temps de réponse). Ce champ
    vaut la dépense : un dépôt gated exige un jeton HF et fait échouer le
    téléchargement APRÈS le clic sur « Lancer » — c'est exactement ce qui est
    arrivé au mmproj de Flash-Next. Le voir dans les résultats évite de le
    découvrir au lancement.
    """
    filters = ([task] if task else []) + ([GB10_TAG] if gb10_only else [])
    params = {'search': query, 'limit': _SEARCH_PAGE_SIZE, 'skip': max(0, int(skip)),
              'sort': 'downloads', 'direction': -1, 'full': 'true'}
    # `filter` vide et `filter` absent ne veulent pas dire la même chose côté HF
    # (plusieurs `filter` = ET) : on omet la clé au lieu d'envoyer une liste vide.
    if filters:
        params['filter'] = filters
    out, has_more = _hf_page(params)
    for m in out:
        m['engine'] = guess_engine(m)
        # `siblings` (la liste des fichiers du dépôt) pesait 58 % de la réponse —
        # mesuré : 51 Kio sur 87 pour 39 modèles — et AUCUN écran ne s'en sert
        # (l'engine se déduit des `tags`, pas des fichiers). Le retirer allège
        # chaque recherche de plus de moitié, sur 48 modèles × ~15 fichiers.
        # `_id` (identifiant interne de Hugging Face) part avec, même raison.
        m.pop('siblings', None)
        m.pop('_id', None)
    return out, has_more


def search_hf_models(query, task=None, gb10_only=False, skip=0):
    """La liste seule — le même appel que `search_hf_models_page`, sans `has_more`."""
    return search_hf_models_page(query, task, gb10_only, skip)[0]


def hf_modele_hors_gb10(query, task=None):
    """Y a-t-il des modèles pour cette recherche SANS le filtre gb10 ?

    Sert à ne pas laisser l'utilisateur devant un « aucun résultat » trompeur :
    quand le filtre GB10 ne donne rien mais que HF en connaît, l'interface peut
    le dire et proposer de décocher le filtre. Renvoie True, False, ou None
    quand on ne sait pas — un doute ne doit pas s'afficher comme un « non ».
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
