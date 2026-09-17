"""Pilotage du runner et des sidecars : etat, lancement, arret, journaux, sondes.

Extrait de app.py le 28/08, depuis la banniere « Helpers ».

Les SONDES de disponibilite (get_ocr_model, get_voice_model, asr_is_up,
image_ready, get_image_model, music_ready, get_music_model) vivent ici et non
dans les blueprints correspondants. Je les y avais d'abord mises, a tort : un
sidecar se sonde independamment de ses routes, et les laisser la-bas creait une
dependance CROISEE (sidecars a besoin des sondes, les routes ont besoin de
_sidecar_proc_status). En les regroupant ici, tout redevient a sens unique.

`_log` remplace `app.logger` : c'est le meme objet (logging.getLogger('app')),
verifie. Cf. litellm_client.py.
"""
import json
import logging
import os
import re
import threading
import time

import requests
from flask import current_app, jsonify, session

from announcements import _announce_launch
from comfyui_client import comfyui_is_up
from config import (ASR_URL, IMAGE_URL, MUSIC_URL, OCR_URL, RUNNER_TOKEN,
                    RUNNER_URL, VOICE_URL)
from db import log_audit, set_setting
from litellm_client import _point_auto_model, _register_litellm_model
from vllm_health import get_running_models

_log = logging.getLogger('app')

# Motifs de « delai depasse » : le runner n'a pas repondu dans le temps imparti.
# Ce n'est PAS un echec — l'operation est probablement en cours. L'appelant ne
# doit donc ni alerter l'infra, ni inviter l'operateur a recliquer (relancer un
# modele en cours de chargement le tue : regle d'or).
_MOTIF_DELAI_LANCEMENT = (
    "Le runner n'a pas répondu dans le délai imparti (150 s) : le lancement est "
    "peut-être toujours en cours. Vérifie l'état du modèle AVANT de relancer — "
    "relancer tuerait un modèle en cours de chargement.")
_MOTIF_DELAI_ARRET = (
    "Le runner n'a pas répondu dans le délai imparti (45 s) : l'arrêt est "
    "peut-être toujours en cours. Vérifie l'état du modèle avant de réessayer.")

# Launchable voice variants. Must stay aligned with runner.py's
# allowlists (_VOICE_REPO_IDS / _VOICE_QWEN_IDS), which revalidate on their side.
VOICE_REPO_IDS = (
    'Qwen3-TTS-12Hz-1.7B-Base', 'Qwen3-TTS-12Hz-0.6B-Base',
    'chatterbox-multilingual', 'chatterbox-turbo', 'chatterbox',
)




_ocr_model_cache = {'t': 0.0, 'v': None}

def get_ocr_model():
    """Model served by the OCR container (baidu/Unlimited-OCR), a separate vLLM
    with its own /v1/models — never mixed with get_running_models() on which
    other routes (stop/relaunch from admin) depend to target only
    the main chat model.
    """
    now = time.time()
    if now - _ocr_model_cache['t'] < 5:
        return _ocr_model_cache['v']
    v = None
    # Do NOT attempt the HTTP call if the container isn't running: the sidecar
    # network silently DROPs packets to an absent service, so
    # requests would wait the full timeout (~3 s) — that's what dragged down the
    # admin page when OCR was stopped. Process state is cached for 5 s.
    if _sidecar_proc_status('ocr') == 'running':
        try:
            r = requests.get(f"{OCR_URL}/models", timeout=3)
            if r.ok:
                data = r.json().get('data', [])
                if data:
                    v = data[0]['id']
        except Exception:
            pass
    _ocr_model_cache.update(t=now, v=v)
    return v

_voice_model_cache = {'t': 0.0, 'v': None}

def get_voice_model():
    """Chatterbox variant currently loaded by the voice container, probed
    live via /api/model-info (never frozen: the admin can recreate this
    container with another variant, cf. the voice catalog /admin/voice/*).
    Returns the type ('original'|'turbo'|'multilingual') only once
    the model is actually loaded (the 'loaded' field), not just the process up.
    """
    now = time.time()
    if now - _voice_model_cache['t'] < 5:
        return _voice_model_cache['v']
    v = None
    # Same guard as get_ocr_model: no HTTP call if the voice container is
    # stopped (otherwise a full ~3 s timeout, sidecar network in DROP).
    if _sidecar_proc_status('voice') == 'running':
        try:
            r = requests.get(f"{VOICE_URL}/api/model-info", timeout=3)
            if r.ok:
                data = r.json()
                if data.get('loaded'):
                    v = data.get('type')
        except Exception:
            pass
    _voice_model_cache.update(t=now, v=v)
    return v

_asr_info_cache = {'t': 0.0, 'v': {}}

def _asr_info():
    """Réponse de /api/model-info du sidecar ASR, mise en cache 10 s.

    Un seul appel HTTP sert désormais deux besoins (le sidecar est-il prêt, et
    QUEL modèle est chargé) : `/api/admin` est interrogé toutes les 8 s, on ne
    veut pas doubler les requêtes vers un service qu'on réveille.
    """
    now = time.time()
    if now - _asr_info_cache['t'] < 10:
        return _asr_info_cache['v']
    info = {}
    try:
        r = requests.get(f"{ASR_URL}/api/model-info", timeout=3)
        if r.ok:
            info = r.json() or {}
    except Exception:
        info = {}
    _asr_info_cache.update(t=now, v=info)
    return info

def asr_is_up():
    return bool(_asr_info().get('loaded'))

def asr_model_name():
    """Nom du modèle RÉELLEMENT chargé, ou None si le sidecar ne sert rien.

    L'interface annonçait « whisper-large-v3-turbo » en dur : vrai aujourd'hui,
    mensonger dès que le sidecar changera de modèle. On préfère ne rien dire
    (None) plutôt que de répéter une valeur figée.
    """
    info = _asr_info()
    return info.get('type') if info.get('loaded') else None

def image_ready():
    try:
        r = requests.get(f"{IMAGE_URL}/health", timeout=3)
        return bool(r.ok and r.json().get('ready'))
    except Exception:
        return False

def get_image_model():
    try:
        r = requests.get(f"{IMAGE_URL}/model-info", timeout=3)
        if r.ok:
            return r.json().get('model')
    except Exception:
        pass
    return None

def music_ready():
    try:
        r = requests.get(f"{MUSIC_URL}/health", timeout=3)
        return bool(r.ok and r.json().get('ready'))
    except Exception:
        return False

def get_music_model():
    try:
        r = requests.get(f"{MUSIC_URL}/model-info", timeout=3)
        if r.ok:
            return r.json().get('model')
    except Exception:
        pass
    return None

def motif_refus(r):
    """Motif de refus d'une réponse du runner, `''` s'il n'y a rien à lire.

    Le runner répond `{"detail": "..."}` quand il refuse un lancement ; le reste
    du temps son corps n'est pas du JSON. Sept appels recopiaient ce
    `try/except` — une seule définition suffit, et l'échec silencieux y est
    explicite au lieu d'être répété sept fois.
    """
    try:
        return r.json().get('detail', '') or ''
    except Exception:                                    # noqa: BLE001
        return ''


def _runner_headers():
    return {'Authorization': f'Bearer {RUNNER_TOKEN}'}

def runner_status():
    try:
        r = requests.get(f"{RUNNER_URL}/status", headers=_runner_headers(), timeout=3)
        if r.ok:
            st = r.json()
            # The runner only switches to "running" on the log line
            # "Application startup complete", hidden by --uvicorn-log-level
            # warning. We make state reliable by checking vLLM actually serves
            # the model → no more "Starting…" status stuck on screen.
            if st.get('status') == 'starting' and st.get('model') in get_running_models():
                st['status'] = 'running'
            return st
    except Exception:
        pass
    return {'status': 'unreachable', 'model': None, 'pid': None}

def runner_launch(hf_model_id, model_name, vllm_args='', engine='vllm'):
    """Lance un modele. Renvoie (ok, motif, incertain).

    `incertain` distingue un REFUS (le runner a repondu non : rien ne demarre,
    `motif` porte sa raison exacte) d'un DELAI DEPASSE (aucune reponse dans le
    temps imparti, alors que le chargement peut tres bien etre en cours).
    L'appelant ne doit PAS alerter l'infra sur un doute : un faux « echec de
    lancement » habitue l'administrateur a ignorer les alertes et l'invite a
    recliquer — ce qui tue un modele en cours de chargement.

    Timeout long : quand un modele tourne deja, le runner attend que le driver
    rende la memoire unifiee avant de lancer le suivant (anti-OOM). /launch met
    donc 10-60 s en general, et jusqu'a ~100 s dans le pire cas (SIGTERM 30 s +
    SIGKILL 5 s + attente de liberation 60 s + stabilisation). A 90 s, un
    lancement parfaitement normal etait rapporte comme un echec.
    """
    def _once():
        r = requests.post(f"{RUNNER_URL}/launch",
                          headers=_runner_headers(),
                          json={'hf_model_id': hf_model_id, 'model_name': model_name,
                                'vllm_args': vllm_args, 'engine': engine or 'vllm'},
                          timeout=150)
        # Launch accepted → the `auto-model` alias follows the new model.
        if r.ok:
            # Et le modele REEL est re-enregistre : ses limites annoncees a LiteLLM
            # (max_input/max_output, calculees par ctx_split) dependent des args de
            # CE lancement. Sans cela, seul `auto-model` suivait un changement de
            # contexte et le nom reel gardait les anciennes valeurs — constate le
            # 04/09 : auto-model annoncait 737856/262144, le nom reel 196608/65536.
            #
            # Ce routage reste IMMEDIAT (le retirer pendant le chargement ne
            # changerait rien pour un client : l'amont ne repond pas encore, il
            # obtiendrait la meme erreur). Ce qui est differe, c'est l'ANNONCE aux
            # utilisateurs et l'alerte en cas d'echec — voir _suivre_lancement.
            _register_litellm_model(model_name, vllm_args, engine or 'vllm')
            _point_auto_model(model_name, vllm_args, engine or 'vllm')
            _suivre_lancement(model_name)
            return True, '', False
        # Le runner REFUSE avec un motif precis (flag hors liste blanche, moteur
        # absent, GGUF introuvable). Le jeter et n'annoncer qu'un echec generique
        # envoie l'administrateur chercher au mauvais endroit : vu le 04/09, un
        # « flag not allowed: --llama-next » affiche comme « Runner inaccessible ».
        try:
            motif = (r.json() or {}).get('error') or r.text[:200]
        except Exception:                                    # noqa: BLE001
            motif = r.text[:200]
        return False, f"{motif} (HTTP {r.status_code})", False
    try:
        return _once()
    except requests.exceptions.ConnectionError:
        # Runner brièvement injoignable : on retente UNE fois. Une ConnectionError
        # signifie qu'aucun lancement n'a pu partir (on ne risque donc pas de
        # double-spawn). Un timeout, en revanche, peut correspondre à un démarrage
        # déjà en cours → on n'insiste pas (règle d'or : ne pas relancer un modèle
        # qui tourne déjà).
        time.sleep(1)
        try:
            return _once()
        except requests.exceptions.Timeout:
            return False, _MOTIF_DELAI_LANCEMENT, True
        except Exception:
            return False, "runner injoignable", False
    except requests.exceptions.Timeout:
        return False, _MOTIF_DELAI_LANCEMENT, True
    except Exception as e:                                   # noqa: BLE001
        return False, f"runner injoignable ({type(e).__name__})", False

# Fenetre de suivi d'un lancement accepte. Un chargement de ~100 Go peut etre
# long (poids + compilation des kernels FlashInfer au premier demarrage), et le
# watchdog du runner retente jusqu'a 3 fois : il faut laisser le temps aux
# tentatives avant de conclure a l'echec.
_FENETRE_DEMARRAGE_S = 900
_INTERVALLE_SUIVI_S = 5

def _suivre_lancement(model_name, fenetre_s=None):
    """Verifie APRES COUP qu'un lancement accepte a bien abouti, et le dit.

    Le runner repond « starting » des le Popen : accepter n'est pas servir. Sans
    ce suivi, le portail annoncait le nouveau modele aux utilisateurs au moment
    du spawn — donc un modele qui pouvait ne jamais demarrer — et un echec de
    chargement (OOM, poids incomplets, arch non supportee) laissait la
    plateforme SANS modele servi sans que personne ne soit prevenu : le moniteur
    ne sonde pas vLLM (sidecar on-demand), donc « runner debout, aucun modele »
    est invisible pour lui.

    L'annonce aux utilisateurs part donc maintenant quand le modele SERT, et un
    echec produit un email d'infra + une ligne d'audit. Volontairement en fil
    d'arriere-plan : la requete d'admin doit repondre tout de suite (elle est
    tenue par act() cote interface, et le flux de logs du runner montre deja la
    progression au client).
    """
    fenetre = _FENETRE_DEMARRAGE_S if fenetre_s is None else fenetre_s
    annonce, audit = _announce_launch, log_audit
    try:
        ctx = current_app._get_current_object()
    except Exception:                                        # noqa: BLE001
        # Hors contexte de requete (outil de support, script) : on renonce au
        # suivi plutot que d'echouer bruyamment — le lancement, lui, a eu lieu.
        return

    def _boucle():
        fin = time.time() + fenetre
        while time.time() < fin:
            time.sleep(_INTERVALLE_SUIVI_S)
            etat = runner_status()
            nom, statut = etat.get('model'), etat.get('status')
            if nom and nom != model_name and statut in ('running', 'starting'):
                # Un AUTRE modele a ete lance entre-temps : ce n'est plus notre
                # affaire (et un arret volontaire ne doit pas declencher d'alerte).
                return
            if nom == model_name and statut == 'running':
                with ctx.app_context():
                    try:
                        annonce(model_name)
                        audit('systeme', 'model.servi',
                              f"{model_name} chargé et en service")
                        # Le runner n'expose pas d'heure de mise en service : on
                        # enregistre donc CE QUE LE PORTAIL A OBSERVÉ (le modèle
                        # répond à cet instant). Lu par le bandeau « état de la
                        # plateforme » ; absent si le modèle servait déjà avant
                        # cette version, et l'interface affiche alors « — ».
                        set_setting('model_servi', json.dumps(
                            {'nom': model_name, 'depuis': int(time.time())}))
                    except Exception:                        # noqa: BLE001
                        _log.warning("suivi de lancement : annonce impossible", exc_info=True)
                return
            if nom == model_name and statut == 'error':
                break
        with ctx.app_context():
            try:
                audit('systeme', 'model.echec_apres_lancement',
                      f"{model_name} accepté par le runner mais jamais en service "
                      f"dans les {int(fenetre)} s")
                from notify import notify_infra_alert_email
                notify_infra_alert_email(
                    "Chat model never came up",
                    f"{model_name}: le runner a accepté le lancement mais le modèle "
                    f"n'est pas en service après {int(fenetre)} s. La plateforme n'a "
                    f"probablement AUCUN modèle servi — vérifie les logs du runner.")
            except Exception:                                # noqa: BLE001
                _log.warning("suivi de lancement : alerte impossible", exc_info=True)

    threading.Thread(target=_boucle, name='suivi-lancement', daemon=True).start()

def runner_stop():
    """Arrete le modele servi. Renvoie (ok, motif, incertain).

    Timeout porte a 45 s : /stop SIGTERM le process, attend jusqu'a 30 s, puis
    SIGKILL (5 s) et efface last_model.json. A 5 s, un arret parfaitement normal
    etait annonce « Runner vLLM inaccessible » alors qu'il etait en cours.
    """
    try:
        r = requests.post(f"{RUNNER_URL}/stop", headers=_runner_headers(), timeout=45)
        if r.ok:
            return True, '', False
        return False, f"le runner a refusé l'arrêt (HTTP {r.status_code})", False
    except requests.exceptions.Timeout:
        return False, _MOTIF_DELAI_ARRET, True
    except Exception as e:                                   # noqa: BLE001
        return False, f"runner injoignable ({type(e).__name__})", False

_sidecar_proc_cache = {}

def _sidecar_proc_status(kind):
    """kind ∈ {'ocr', 'video', 'voice', 'asr'} — raw PROCESS/CONTAINER state (docker inspect /
    systemctl is-active), via vllm-runner (scoped sudo privileges on the host,
    see /etc/sudoers.d/vllmrunner-services): dgx-portal itself has no
    docker/systemd access, neither here nor elsewhere. Does NOT say whether the service already
    answers requests — cf. _sidecar_status().

    Result cached 5 s: each call triggers on the runner side a `sudo`
    then a `docker inspect`/`systemctl is-active`, and the `systemctl` alone
    cost 1.5 s on this machine. The admin probes all four sidecars and
    refreshes every 8 s, so without the cache the page spent most of
    its time in there.
    """
    now = time.time()
    hit = _sidecar_proc_cache.get(kind)
    if hit and now - hit[0] < 5:
        return hit[1]
    v = 'unreachable'
    try:
        r = requests.get(f"{RUNNER_URL}/{kind}/status", headers=_runner_headers(), timeout=5)
        if r.ok:
            v = r.json().get('status', 'unknown')
    except Exception:
        pass
    _sidecar_proc_cache[kind] = (now, v)
    return v

def _sidecar_status(kind):
    """Status shown to the admin. A container/service that just started
    stays several tens of seconds (even minutes, large checkpoint) loading
    the model before it answers — during that time, docker/systemd already
    see it as "running", but any generation would fail. Before this
    fix, the admin card showed "Online" as soon as the process
    launched, not when the backend is really usable (reported: the
    status said video was running while it wasn't answering
    yet). So we additionally verify, live, that the service answers:
    get_ocr_model()/comfyui_is_up() hit respectively /v1/models and
    /system_stats, which only answer once loading is finished.

    We test the CONTAINER state first (fast, cached 5 s). If it isn't
    running, no point probing the HTTP service: the check would go into the
    void and wait its timeout (~3 s), which dragged down the whole admin page
    when a sidecar was stopped. The HTTP "does it answer yet?" probe only makes
    sense if the container is up, to tell "starting" from "running".
    """
    proc = _sidecar_proc_status(kind)
    if proc != 'running':
        return proc
    ready = (get_ocr_model() is not None if kind == 'ocr'
             else comfyui_is_up() if kind == 'video'
             else get_voice_model() is not None if kind == 'voice'
             else asr_is_up() if kind == 'asr'
             else image_ready() if kind == 'image'
             else music_ready() if kind == 'music'
             else False)
    return 'running' if ready else 'starting'

def _mem_available_gb():
    """Actually allocatable memory (MemAvailable from /proc/meminfo), in GB.
    On the GB10 memory is unified: this is also the headroom available to
    load a model on the GPU.
    """
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return None

# Approximate memory (GB, margin included) a sidecar must be able to allocate
# to load its model. On unified memory, a sidecar that overflows doesn't
# merely fail: the OOM killer kills the largest process — the chat model —
# and the whole platform goes down. Hence this guard BEFORE starting.
# OCR/voice/dictation load a model then stay stable → threshold = weight + small
# margin. Video (ComfyUI) additionally has memory SPIKES during generation →
# higher threshold to keep a real cushion. The chat model's memory is,
# itself, frozen at launch (KV pre-allocated), so once a sidecar is loaded
# the whole is stable — that's what makes these thresholds reliable.
# 'music' vaut pour le mode par défaut du sidecar : quantification 8 bits des
# deux gros LLM (~24 Go en bf16 → ~13 Go). Surchargeable par env si on repasse
# le sidecar en pleine précision (MUSIC_QUANT=none) ou en 4 bits.
_SIDECAR_MEM_NEED_GB = {'ocr': 20, 'video': 28, 'voice': 15, 'asr': 5, 'image': 40,
                        'music': int(os.environ.get('MUSIC_MEM_NEED_GB', 15))}

def _mem_guard(kind):
    """Return an error message if starting `kind` risks an OOM, otherwise None."""
    need = _SIDECAR_MEM_NEED_GB.get(kind)
    if not need:
        return None
    avail = _mem_available_gb()
    if avail is not None and avail < need:
        return (f"Mémoire insuffisante pour démarrer {kind} : {avail:.0f} Go libres, "
                f"~{need} Go requis. Arrête un autre backend, ou réduis le contexte du "
                f"modèle de chat, puis réessaie.")
    return None

def _sidecar_start_json(kind):
    """Start a sidecar with a memory guard, JSON response for the frontend."""
    err = _mem_guard(kind)
    if err:
        return jsonify({'ok': False, 'error': err}), 507
    ok, detail = _sidecar_action(kind, 'start')
    return jsonify({'ok': bool(ok),
                    'error': None if ok else f"Échec du démarrage {kind}.{detail}"}), (200 if ok else 502)

def _sidecar_stop_json(kind):
    """Arret d'un sidecar : meme contrat JSON que le demarrage.

    L'arret repondait par un flash + une redirection, que l'interface Next ne
    rend nulle part : un echec d'arret s'affichait donc comme un succes.
    """
    ok, detail = _sidecar_action(kind, 'stop')
    return jsonify({'ok': bool(ok),
                    'error': None if ok else f"Échec de l'arrêt {kind}.{detail}"}), (200 if ok else 502)

def _sidecar_action(kind, action):
    """(ok, detail) — `detail` porte le motif exact renvoye par le runner.

    Le jeter ne laissait a l'administrateur qu'un « Échec du démarrage ocr. »
    identique pour un runner injoignable, un conteneur absent apres une
    recreation ratee et une erreur transitoire — donc rien d'exploitable sans
    ouvrir un shell sur l'hote.
    """
    ok, detail = False, ''
    try:
        r = requests.post(f"{RUNNER_URL}/{kind}/{action}", headers=_runner_headers(), timeout=30)
        ok = r.ok
        if not ok:
            motif = ''
            try:
                corps = r.json() or {}
                motif = corps.get('detail') or corps.get('error') or ''
            except Exception:                                # noqa: BLE001
                motif = (r.text or '')[:200]
            motif = str(motif).strip()
            detail = f" ({motif})" if motif else f" (HTTP {r.status_code})"
    except Exception as e:                                   # noqa: BLE001
        detail = f" ({type(e).__name__})"
    log_audit(session.get('username'), f'sidecar.{action}',
              f"{kind} : {'OK' if ok else 'échec' + detail}")
    return ok, detail

def _ocr_launch(hf_id, args):
    """Recreate the OCR container with another model (runner.py validates the flags
    against the OCR allowlist before any sudo call, see _OCR_*_FLAGS).
    """
    try:
        r = requests.post(f"{RUNNER_URL}/ocr/launch", headers=_runner_headers(),
                          json={'hf_model_id': hf_id, 'vllm_args': args or ''}, timeout=90)
        return r.ok, motif_refus(r)
    except Exception as e:
        return False, str(e)

def _voice_launch(repo_id):
    """Recreate the voice container with another Chatterbox variant (runner.py
    revalidates repo_id against its own allowlist before any sudo call,
    see _VOICE_REPO_IDS).
    """
    try:
        r = requests.post(f"{RUNNER_URL}/voice/launch", headers=_runner_headers(),
                          json={'repo_id': repo_id}, timeout=90)
        return r.ok, motif_refus(r)
    except Exception as e:
        return False, str(e)

# Image generation models the admin may launch (mirrors _VOICE_REPO_IDS): a
# closed allowlist, revalidated by the runner (_IMAGE_MODEL_IDS) before any sudo
# call. Each id maps host-side to a pre-downloaded diffusers dir (image-recreate.sh).
IMAGE_MODEL_IDS = {'black-forest-labs/FLUX.2-klein-4B'}

def _image_launch(model_id):
    """Recreate the image container with another diffusers model (runner.py
    revalidates model_id against its own allowlist before any sudo call).
    """
    try:
        r = requests.post(f"{RUNNER_URL}/image/launch", headers=_runner_headers(),
                          json={'model_id': model_id}, timeout=180)
        return r.ok, motif_refus(r)
    except Exception as e:
        return False, str(e)

# Modèles musique : id HuggingFace libre (comme l'OCR), la forme est validée
# ici ET côté runner avant tout appel sudo.
_HF_ID_RE = re.compile(r'^[A-Za-z0-9][\w.-]{0,60}/[A-Za-z0-9][\w.-]{0,80}$')

def _music_launch(model_id):
    """Recrée le conteneur musique avec un autre modèle HF."""
    try:
        r = requests.post(f"{RUNNER_URL}/music/launch", headers=_runner_headers(),
                          json={'model_id': model_id}, timeout=180)
        return r.ok, motif_refus(r)
    except Exception as e:
        return False, str(e)

# Routine access lines (health/status polls) → noise that drowns the useful logs.
_LOG_NOISE_RE = re.compile(r'"GET /(?:v1/models|metrics|health\S*|version|ping)\b')

def _drop_log_noise(lines):
    return [l for l in lines if not _LOG_NOISE_RE.search(l)]

# Tampon complet du runner. On demande TOUJOURS le maximum, jamais une fenetre
# proportionnelle a n : le tampon est domine par les lignes d'acces de routine
# (sondes /metrics et /v1/models), dans une proportion qui varie avec la cadence
# des sondages et l'activite du modele. Mesure du 28/08, modele au repos : sur
# les 1 000 dernieres lignes brutes, ZERO ligne utile — la fenetre de n*5 = 750
# ne contenait que du bruit, `runner_logs` renvoyait une liste vide et le
# panneau d'administration affichait « ce modele n'est pas demarre » alors qu'il
# tournait. Les premieres lignes utiles n'apparaissaient qu'au-dela de 1 500.
_RUNNER_LOGS_TAMPON = 2000

def runner_logs(n=150):
    try:
        r = requests.get(f"{RUNNER_URL}/logs", headers=_runner_headers(),
                         params={'n': _RUNNER_LOGS_TAMPON}, timeout=5)
        if r.ok:
            return _drop_log_noise(r.json().get('logs', []))[-n:]
        _log.warning("runner_logs : le runner a repondu %s", r.status_code)
    except Exception as e:                                   # noqa: BLE001
        # Sans cette trace, un echec ici est indiscernable d'un modele arrete :
        # c'est exactement ce qui a masque le probleme ci-dessus.
        _log.warning("runner_logs : %s", type(e).__name__)
    return []

_runner_metrics_cache = {'t': 0.0, 'v': None}

def runner_metrics():
    """Host CPU/RAM/GPU metrics from the runner. Expensive on the runner side:
    _cpu_pct() samples /proc/stat and _gpu() spawns nvidia-smi. /api/home calls
    this on every poll, so cache briefly — the "Server state" panel is polled
    every ~5 s and doesn't need sub-3 s freshness. (The 229 ms cost itself is a
    runner-side time.sleep(0.2); see vllm-runner/runner.py _cpu_pct.)
    """
    now = time.time()
    if now - _runner_metrics_cache['t'] < 3:
        return _runner_metrics_cache['v']
    out = None
    try:
        r = requests.get(f"{RUNNER_URL}/metrics", headers=_runner_headers(), timeout=5)
        if r.ok:
            out = r.json()
    except Exception:
        pass
    _runner_metrics_cache.update(t=now, v=out)
    return out
