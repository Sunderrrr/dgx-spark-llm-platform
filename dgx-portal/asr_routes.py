"""Dictation (ASR): audio transcription.

Extracted from app.py on 28/08, at the same time as voice: these routes lived
under the « Voix » banner although it is a distinct sidecar, on its own
network. That misplaced boundary is what made the section non-extractable.

asr_is_up() is re-imported by app.py: the sidecar dashboard uses it.

ON-DEMAND since this change: the `asr` container (~2.1 GiB of GPU) is started
by the first transcription request and stopped by an idle reaper after 10 min
without one (see « ASR on-demand » in sidecars.py). This route NEVER blocks
waiting for whisper to load — useDictation re-polls it EVERY SECOND
(POLL_MS = 1000) and a held request would hold a gunicorn worker too. While
the model is not loaded it answers a retry-later payload the 1 s polling
tolerates silently; when there is not enough memory to start safely, it
refuses to start and says so.
"""
import requests
from flask import Blueprint, jsonify, request

from auth import login_required
from config import ASR_URL
from sidecars import (asr_appel_en_vol, asr_demarrage_auto, asr_demarrer_veilleur,
                      asr_is_up, asr_load_error, asr_memoire_ok,
                      asr_note_activite, _sidecar_proc_status, motif_refus)
from guards import (_MAX_VOICE_UPLOAD_BYTES, dictation_rate_block,
                    maintenance_block_json)

bp = Blueprint('asr', __name__)


@bp.route('/api/transcribe', methods=['POST'])
@login_required
def api_transcribe():
    """Dictation: mic audio → text. Deliberately self-hosted — the browser's
    SpeechRecognition API would send the voice to Google, which
    would defeat the whole point of the platform.
    """
    blocked = maintenance_block_json()
    if blocked:
        return blocked
    # Dictation is an expensive GPU endpoint (up to 180 s per call against the
    # shared ASR model) that has no LiteLLM key/budget, so it needs its own
    # bound — but NOT the media one: the client re-transcribes every second
    # while the user speaks (guards.dictation_rate_block explains the 429 that
    # cost the users 20 s of speech).
    limited = dictation_rate_block()
    if limited:
        return limited
    f = request.files.get('audio')
    if not f or not f.filename:
        return jsonify({'error': "Aucun audio fourni."}), 400
    data = f.read(_MAX_VOICE_UPLOAD_BYTES + 1)
    if len(data) > _MAX_VOICE_UPLOAD_BYTES:
        return jsonify({'error': "Enregistrement trop volumineux (15 Mo max)."}), 400
    language = request.form.get('language', '').strip()[:10]
    # On-demand bookkeeping: every REAL attempt resets the idle clock and arms
    # the reaper thread (cheap file state, idempotent — the endpoint is polled
    # every second).
    asr_note_activite()
    asr_demarrer_veilleur()
    if not asr_is_up():
        etat = _sidecar_proc_status('asr')
        reessai = False          # memory freed after a failed load: CALL below
        if etat in ('running', 'stopped', 'unknown'):
            # The runner answers for the container: we KNOW its state, so no
            # fall-through guess. Never block waiting for the model to load.
            if etat in ('stopped', 'unknown'):
                ok_mem, motif_mem = asr_memoire_ok()
                if not ok_mem:
                    # Honest refusal and NO start: on unified memory a sidecar
                    # that loads with no headroom takes the served chat model
                    # down with it (the OOM killer cannot even see GPU memory).
                    return jsonify({'error': motif_mem, 'demarrage': False}), 503
                # Idempotent by construction: `docker start` on a running
                # container is a no-op, and a successful start throttles the
                # next attempts (sidecars.asr_demarrage_auto) — the 1 s polling
                # must not re-issue one per second while the model loads.
                ok, detail = asr_demarrage_auto()
                if not ok:
                    return jsonify({'error': f"Le service de dictée n'a pas pu démarrer{detail}."}), 503
            else:
                erreur = asr_load_error()
                if erreur and not asr_memoire_ok()[0]:
                    # The container runs but published a load failure (« CUDA
                    # error: out of memory ») and memory is STILL tight: saying
                    # nothing would read as slowness — same lesson as the admin
                    # card. The message carries the remedy (free memory, then
                    # relaunch), which the branch below makes real.
                    return jsonify({'error': f"Le modèle de dictée n'a pas pu se charger : {erreur}"}), 503
                if erreur:
                    # Memory has been freed since the failed load (2026-10-03):
                    # fall THROUGH to the call — the sidecar now retries its load
                    # on demand (asr/server.py `_charger_si_besoin`). The message
                    # promised « relance la dictée »; before, that was impossible
                    # by construction (the load was tried ONCE, at container
                    # startup, and never again).
                    reessai = True
            # Retry-later payload, shaped after what useDictation actually
            # tolerates: a 2xx keeps the 1 s poll silent (anything else is
            # shown as an error), and `text` MUST be a string — the client
            # treats any other type as a failed round. So the starting state
            # rides along an empty text, and the next poll (1 s later) simply
            # finds the sidecar one second closer to ready.
            if not reessai:
                return jsonify({'text': '', 'demarrage': True}), 200
        # Runner unreachable / no container state: attempt the call anyway —
        # the sidecar may still be up, and the historical error contract below
        # (4xx input, 503, 502, 504) applies unchanged.
    try:
        with asr_appel_en_vol():
            r = requests.post(f"{ASR_URL}/transcribe",
                              files={'audio': ('rec.wav', data, 'audio/wav')},
                              data={'language': language}, timeout=180)
        if not r.ok:
            detail = motif_refus(r)
            # The SIDECAR's error code is passed through, no longer overwritten as 502. A
            # recording too short, an unreadable format or a file too large are INPUT
            # ERRORS (400/413): presenting them as a service failure made it look like an
            # outage, and a client that retries automatically replayed a request that
            # could not work anyway. 503 (model not loaded) and 5xx remain genuine
            # failures.
            statut = r.status_code
            if 400 <= statut < 500:
                return jsonify({'error': detail or "Enregistrement refusé."}), statut
            if statut == 503:
                return jsonify({'error': detail or "Modèle de transcription non chargé."}), 503
            return jsonify({'error': detail or "Échec de la transcription."}), 503
        return jsonify({'text': r.json().get('text', '')})
    except requests.exceptions.Timeout:
        return jsonify({'error': "La transcription a mis trop de temps."}), 504
    except Exception:
        return jsonify({'error': "Service de transcription injoignable."}), 503

@bp.route('/api/transcribe/available')
@login_required
def api_transcribe_available():
    """Whether dictation can serve — NOW or ON DEMAND.

    Before on-demand start this was simply `asr_is_up()`: with the sidecar
    stopped, DictateButton rendered nothing (`if (!available) return null`)
    and NOTHING could ever trigger the auto-start — the feature was
    unreachable exactly when it mattered. A stopped-but-present container is
    therefore « available »: the first transcription starts it. An absent
    container or an unreachable runner stays unavailable (the button hides
    rather than promising a service that cannot run).
    """
    if asr_is_up():
        return jsonify({'available': True})
    return jsonify({'available': _sidecar_proc_status('asr') in ('running', 'stopped')})
