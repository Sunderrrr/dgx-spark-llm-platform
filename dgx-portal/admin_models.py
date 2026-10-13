"""Admin — models and sidecars: the chat-model catalog and its launch/stop,
the media sidecars (OCR, voice, dictation, image, video) with their catalogs,
the runner's logs, and the approval of a user's model request.

Routes of the `admin` blueprint (defined in admin_routes.py): the endpoint
names stay `admin.*` whatever module holds them.
"""
import re
import sqlite3
from datetime import datetime

import requests
from flask import (Response, jsonify, request,
                   session, stream_with_context)

from announcements import add_announcement
from auth import (admin_required, ldap_lookup_email)
from config import PUBLIC_WEB_URL, RUNNER_URL
from db import (add_notification, get_db, log_audit, platform_name)
from litellm_client import (_register_litellm_model,
                            _unregister_litellm_model)
from notify import (notify_infra_alert_email, send_user_email)
from sidecars import (IMAGE_MODEL_IDS, VOICE_REPO_IDS, _LOG_NOISE_RE,
                      _image_launch, _mem_guard, _ocr_launch,
                      _runner_headers, _sidecar_start_json,
                      _sidecar_stop_json, _voice_launch,
                      runner_delete_files, runner_launch, runner_logs, runner_stop)
from vllm_health import get_running_models, guess_engine

from admin_routes import _json_error, _json_ok, bp


@bp.route('/admin/model/launch', methods=['POST'])
@admin_required
def launch_model():
    name = request.form.get('model_name', '').strip()
    db   = get_db()
    cfg  = db.execute("SELECT * FROM model_configs WHERE name=?", (name,)).fetchone()
    if not cfg:
        return jsonify({'ok': False, 'error': "Modèle introuvable."}), 404
    ok, reason, uncertain = runner_launch(cfg['hf_model_id'], cfg['name'], cfg['vllm_args'] or '',
                                         cfg['engine'] or 'vllm')
    if ok:
        # The « model X replaces model Y » announcement no longer goes out
        # here but from _suivre_lancement, when the model is ACTUALLY served:
        # accepting is not serving, and announcing a model that will never start
        # is a lie visible by all users.
        log_audit(session.get('username'), 'model.launch', f"lancement de {name}")
    else:
        log_audit(session.get('username'), 'model.launch_échec',
                  f"lancement de {name} : {reason}"
                  + (" (délai dépassé, issue inconnue)" if uncertain else " (refusé)"))
        # No infra alert on a DOUBT: the launch may still be in progress.
        # A false "launch failure" trains the administrator to ignore
        # alerts — and invites a relaunch, which kills a model being loaded.
        if not uncertain:
            notify_infra_alert_email(
                "Chat model launch failed", f"{name}: {reason or 'launch refused'}")
    # JSON response, not a redirect: `act()` on the frontend side reads {ok, error}
    # and treats any NON-JSON response as a success (its own comment says so).
    # This route redirected, so a refused launch displayed as successful and
    # the administrator saw « nothing happening ». Observed on 04/09: two
    # attempts refused with 400, no trace on screen.
    if ok:
        return _json_ok(f"Lancement de {name} accepté — chargement en cours.")
    return _json_error(reason or "Lancement refusé par le runner.",
                        202 if uncertain else 503, uncertain=uncertain)

@bp.route('/admin/model/stop', methods=['POST'])
@admin_required
def stop_model():
    ok, reason, uncertain = runner_stop()
    log_audit(session.get('username'), 'model.stop',
              "arrêt du modèle de chat" if ok else f"échec de l'arrêt : {reason}")
    if ok:
        return _json_ok("Modèle arrêté.")
    # `incertain` = the runner did not answer within the delay, the stop may
    # still be in progress. We say so as-is rather than claiming a failure:
    # an operator who believes it failed clicks again.
    return _json_error(reason or "Échec de l'arrêt du modèle.", 202 if uncertain else 503,
                        uncertain=uncertain)

@bp.route('/admin/ocr/start', methods=['POST'])
@admin_required
def start_ocr():
    return _sidecar_start_json('ocr')

@bp.route('/admin/ocr/stop', methods=['POST'])
@admin_required
def stop_ocr():
    return _sidecar_stop_json('ocr')

@bp.route('/admin/video/start', methods=['POST'])
@admin_required
def start_video():
    return _sidecar_start_json('video')

@bp.route('/admin/video/stop', methods=['POST'])
@admin_required
def stop_video():
    return _sidecar_stop_json('video')

@bp.route('/admin/ocr/catalog/add', methods=['POST'])
@admin_required
def add_ocr_cfg():
    name  = re.sub(r'[^a-zA-Z0-9_-]', '-', request.form.get('name', '').strip())[:40]
    hf_id = request.form.get('hf_model_id', '').strip()
    args  = request.form.get('vllm_args', '').strip()
    if not name or not hf_id:
        return _json_error("Nom et HF model ID requis.")
    db = get_db()
    try:
        db.execute("INSERT INTO ocr_configs (name, hf_model_id, vllm_args, added_at) VALUES (?,?,?,?)",
                   (name, hf_id, args, datetime.now().isoformat()))
        db.commit()
    except sqlite3.IntegrityError:
        return _json_error("Un modèle OCR avec ce nom existe déjà.", 409)
    log_audit(session.get('username'), 'ocr.catalog.add', f"{name} ({hf_id})")
    return _json_ok(f"Modèle OCR {name} ajouté au catalogue.")

@bp.route('/admin/ocr/catalog/delete/<int:cid>', methods=['POST'])
@admin_required
def delete_ocr_cfg(cid):
    db = get_db()
    row = db.execute("SELECT name FROM ocr_configs WHERE id=?", (cid,)).fetchone()
    if not row:
        return _json_error("Modèle OCR introuvable.", 404)
    db.execute("DELETE FROM ocr_configs WHERE id=?", (cid,))
    db.commit()
    log_audit(session.get('username'), 'ocr.catalog.delete', row['name'])
    return _json_ok("Modèle OCR supprimé du catalogue.")

@bp.route('/admin/ocr/catalog/launch', methods=['POST'])
@admin_required
def launch_ocr_cfg():
    name = request.form.get('ocr_name', '').strip()
    cfg = get_db().execute("SELECT * FROM ocr_configs WHERE name=?", (name,)).fetchone()
    if not cfg:
        return jsonify({'ok': False, 'error': "Modèle OCR introuvable."}), 404
    # Same memory guard as the simple start: recreating the OCR container
    # with a model allocates just as much memory, and an OOM would kill the chat.
    err = _mem_guard('ocr')
    if err:
        return jsonify({'ok': False, 'error': err}), 507
    ok, detail = _ocr_launch(cfg['hf_model_id'], cfg['vllm_args'] or '')
    log_audit(session.get('username'), 'ocr.launch',
              f"lancement OCR {cfg['hf_model_id']}" if ok else f"échec du lancement OCR : {detail}")
    if not ok:
        notify_infra_alert_email("OCR launch failed", f"{cfg['hf_model_id']}: {detail}")
    return jsonify({'ok': bool(ok), 'error': None if ok else f"Échec de la relance OCR : {detail}"}), (200 if ok else 503)

@bp.route('/admin/voice/start', methods=['POST'])
@admin_required
def start_voice():
    return _sidecar_start_json('voice')

@bp.route('/admin/voice/stop', methods=['POST'])
@admin_required
def stop_voice():
    return _sidecar_stop_json('voice')

@bp.route('/admin/asr/start', methods=['POST'])
@admin_required
def start_asr():
    return _sidecar_start_json('asr')

@bp.route('/admin/asr/stop', methods=['POST'])
@admin_required
def stop_asr():
    return _sidecar_stop_json('asr')

@bp.route('/admin/image/start', methods=['POST'])
@admin_required
def start_image():
    return _sidecar_start_json('image')

@bp.route('/admin/image/stop', methods=['POST'])
@admin_required
def stop_image():
    return _sidecar_stop_json('image')

@bp.route('/admin/image/launch', methods=['POST'])
@admin_required
def launch_image():
    model_id = request.form.get('model_id', '').strip()
    if model_id not in IMAGE_MODEL_IDS:
        return jsonify({'ok': False, 'error': "Modèle image inconnu."}), 400
    # Recreating the image container loads ~35 Go bf16; same guard as a plain
    # start so an OOM never reaches the chat model.
    err = _mem_guard('image')
    if err:
        return jsonify({'ok': False, 'error': err}), 507
    ok, detail = _image_launch(model_id)
    log_audit(session.get('username'), 'image.launch',
              f"lancement image {model_id}" if ok else f"échec du lancement image : {detail}")
    if not ok:
        notify_infra_alert_email("Image model launch failed", f"{model_id}: {detail}")
    return jsonify({'ok': bool(ok), 'error': None if ok else f"Échec de la relance image : {detail}"}), (200 if ok else 503)

@bp.route('/admin/voice/catalog/add', methods=['POST'])
@admin_required
def add_voice_cfg():
    name    = re.sub(r'[^a-zA-Z0-9_-]', '-', request.form.get('name', '').strip())[:40]
    repo_id = request.form.get('repo_id', '').strip()
    if not name or repo_id not in VOICE_REPO_IDS:
        return _json_error("Nom et variante requis (variante hors liste autorisée).")
    db = get_db()
    try:
        db.execute("INSERT INTO voice_configs (name, repo_id, added_at) VALUES (?,?,?)",
                   (name, repo_id, datetime.now().isoformat()))
        db.commit()
    except sqlite3.IntegrityError:
        return _json_error("Un modèle voix avec ce nom existe déjà.", 409)
    log_audit(session.get('username'), 'voice.catalog.add', f"{name} ({repo_id})")
    return _json_ok(f"Modèle voix {name} ajouté au catalogue.")

@bp.route('/admin/voice/catalog/delete/<int:cid>', methods=['POST'])
@admin_required
def delete_voice_cfg(cid):
    db = get_db()
    row = db.execute("SELECT name FROM voice_configs WHERE id=?", (cid,)).fetchone()
    if not row:
        return _json_error("Modèle voix introuvable.", 404)
    db.execute("DELETE FROM voice_configs WHERE id=?", (cid,))
    db.commit()
    log_audit(session.get('username'), 'voice.catalog.delete', row['name'])
    return _json_ok("Modèle voix supprimé du catalogue.")

@bp.route('/admin/voice/catalog/launch', methods=['POST'])
@admin_required
def launch_voice_cfg():
    name = request.form.get('voice_name', '').strip()
    cfg = get_db().execute("SELECT * FROM voice_configs WHERE name=?", (name,)).fetchone()
    if not cfg:
        return _json_error("Modèle voix introuvable.", 404)
    # Same memory guard as OCR and image: recreating the voice
    # container reloads a model (~15 Go), and this route was the ONLY one
    # not to set it — on unified memory, an OOM kills the biggest RSS,
    # i.e. the served chat model.
    err = _mem_guard('voice')
    if err:
        return _json_error(err, 507)
    ok, detail = _voice_launch(cfg['repo_id'])
    log_audit(session.get('username'), 'voice.launch',
              f"relance voix {cfg['repo_id']}" if ok else f"échec relance voix : {detail}")
    if not ok:
        notify_infra_alert_email("Voice model launch failed", f"{name}: {detail}")
    if ok:
        return _json_ok(f"Relance voix avec {name} en cours…")
    return _json_error(f"Échec de la relance voix : {detail}", 503)

@bp.route('/admin/model/add', methods=['POST'])
@admin_required
def add_model_cfg():
    name   = re.sub(r'[^a-zA-Z0-9_-]', '-', request.form.get('name', '').strip())[:40]
    hf_id  = request.form.get('hf_model_id', '').strip()
    args   = request.form.get('vllm_args', '').strip()
    engine = request.form.get('engine', 'vllm').strip().lower()
    if engine not in ('vllm', 'llamacpp', 'ds4', 'exllamav3'):
        engine = 'vllm'
    if not name or not hf_id:
        return _json_error("Nom et HF model ID requis.")
    # No shape validation on hf_id here, deliberately: `local:<name>`
    # (local GGUF) is a legitimate identifier, which the « org/nom » regex of
    # an HF id would refuse. The runner keeps its own shape guard
    # before building argv.
    db = get_db()
    try:
        db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                   "VALUES (?,?,?,?,?)",
                   (name, hf_id, args, engine, datetime.now().isoformat()))
        db.commit()
    except sqlite3.IntegrityError:
        return _json_error("Un modèle avec ce nom existe déjà.", 409)
    add_announcement('model_add', name)
    ok = _register_litellm_model(name, args, engine)
    log_audit(session.get('username'), 'model.add',
              f"{name} ({engine}, {hf_id})" + ('' if ok else " — enregistrement LiteLLM ÉCHOUÉ"))
    if ok:
        return _json_ok(f"Modèle {name} ajouté ({engine}) et routé par LiteLLM.")
    return _json_ok(f"Modèle {name} ajouté ({engine}).",
                    warning="Enregistrement LiteLLM échoué : le modèle n'est pas routé.")

@bp.route('/admin/model/edit/<int:mid>', methods=['POST'])
@admin_required
def edit_model_cfg(mid):
    """Modifies the vLLM/llama.cpp args of a catalog entry.

    The args are NOT validated here: the flag allow-list lives in the runner,
    which checks it anyway at launch (`_BOOL_FLAGS`, `_BIN_FLAGS`). Duplicating
    it here would create a second source of truth that would diverge.
    """
    args = request.form.get('vllm_args', '').strip()
    db = get_db()
    row = db.execute("SELECT name, engine FROM model_configs WHERE id=?", (mid,)).fetchone()
    if not row:
        return _json_error("Modèle introuvable dans le catalogue.", 404)
    db.execute("UPDATE model_configs SET vllm_args=? WHERE id=?", (args, mid))
    db.commit()
    # The registration result was IGNORED and the route announced
    # « routage LiteLLM rafraîchi » unconditionally: yet LiteLLM applies the
    # per-request limits (max_input/max_output via ctx_split) coming from THIS
    # entry — a silent failure therefore left a wrong routing.
    ok = _register_litellm_model(row['name'], args, row['engine'] or 'vllm')
    log_audit(session.get('username'), 'model.edit',
              f"{row['name']} — args mis à jour" + ('' if ok else " — routage LiteLLM ÉCHOUÉ"))
    if ok:
        return _json_ok("Args du modèle mis à jour (routage LiteLLM rafraîchi).")
    return _json_ok("Args du modèle mis à jour.",
                    warning="Rafraîchissement LiteLLM échoué : les limites de contexte "
                            "annoncées par la passerelle peuvent être fausses.")

@bp.route('/admin/model/delete/<int:mid>', methods=['POST'])
@admin_required
def delete_model_cfg(mid):
    """Removes a model from the catalog AND erases its files from disk.

    Before 2026-10-01 only the entry went away: the weights stayed (up to
    100 Go each) with no screen left to find them again.
    The files are kept when ANOTHER entry (chat, OCR) points at the
    same ones: erasing the weights of a model still in the catalog would break it.
    """
    db = get_db()
    row = db.execute("SELECT name, hf_model_id FROM model_configs WHERE id=?", (mid,)).fetchone()
    if not row:
        return _json_error("Modèle introuvable dans le catalogue.", 404)
    name, hf_id = row['name'], row['hf_model_id']
    # The SERVED model: its weights are being read by the engine, and the
    # runner would relaunch at next startup a model with no files. We ask to
    # stop it first rather than leaving a half-done state.
    if name in (get_running_models() or []):
        return _json_error(
            f"{name} est le modèle actuellement servi : arrête-le d'abord, puis "
            f"supprime-le (ses fichiers seront effacés du disque).", 409)
    share = [r['name'] for r in db.execute(
        "SELECT name FROM model_configs WHERE hf_model_id=? AND id<>? "
        "UNION SELECT name FROM ocr_configs WHERE hf_model_id=?", (hf_id, mid, hf_id))]
    db.execute("DELETE FROM model_configs WHERE id=?", (mid,))
    db.commit()
    # The result was ignored: the UI announced « retiré de LiteLLM » even
    # when the entry survived there, and a LiteLLM entry without a catalog row
    # is a routing that no screen can clean up anymore.
    deregistered = _unregister_litellm_model(name)
    # The announcement feed is a "what's new", not a log: keeping
    # « modèle X ajouté » for a model just removed announces to users
    # something that no longer exists. The fact remains recorded
    # in the audit, only the feed is cleaned up.
    removed_items = db.execute("DELETE FROM announcements WHERE kind='model_add' AND a=?",
                         (name,)).rowcount
    db.commit()
    warnings = []
    if share:
        files = f"fichiers conservés (utilisés aussi par {', '.join(share)})"
        warnings.append(f"Fichiers conservés : {', '.join(share)} les utilise aussi.")
    else:
        ok_f, size_bytes, reason = runner_delete_files(hf_id)
        if ok_f:
            size = (f"{size_bytes / 2**30:.1f} Gio" if size_bytes >= 2**30
                      else f"{max(1, round(size_bytes / 2**20))} Mio")
            files = (f"{size} effacés du disque" if size_bytes
                        else "aucun fichier sur le disque")
        else:
            files = f"fichiers NON effacés ({reason})"
            warnings.append(f"Les fichiers n'ont pas pu être effacés : {reason}")
    if not deregistered:
        warnings.append("L'entrée LiteLLM n'a pas pu être retirée : elle survit sans "
                              "ligne de catalogue. Vérifie la passerelle.")
    log_audit(session.get('username'), 'model.delete',
              f"{name} — retiré du catalogue, LiteLLM "
              f"{'dérégistré' if deregistered else 'NON DÉRÉGISTRÉ'}, {files}, "
              f"{removed_items} annonce(s) retirée(s)")
    message = f"{name} supprimé : catalogue, LiteLLM, {files}."
    if warnings:
        return _json_ok(message, warning=" ".join(warnings))
    return _json_ok(message)

@bp.route('/admin/runner/logs')
@admin_required
def admin_runner_logs():
    return jsonify({'logs': runner_logs(200)})

# Sidecar log tabs in the admin Logs viewer (LLM comes from runner_logs/stream
# above; these relay the containerised sidecars + ComfyUI). The portal has no
# docker access — the runner reads them via scoped sudo (see /etc/sudoers.d/
# vllmrunner-logs) and returns the tail as a list of lines.
_SIDECAR_LOG_KINDS = {'ocr', 'voice', 'image', 'video', 'asr'}

@bp.route('/admin/sidecar-logs/<kind>')
@admin_required
def admin_sidecar_logs(kind):
    if kind not in _SIDECAR_LOG_KINDS:
        return jsonify({'error': 'unknown kind', 'logs': []}), 400
    try:
        r = requests.get(f"{RUNNER_URL}/{kind}/logs", headers=_runner_headers(), timeout=10)
        if r.ok:
            return jsonify({'logs': r.json().get('logs', [])})
        return jsonify({'logs': [], 'error': 'runner error'}), 503
    except Exception:
        return jsonify({'logs': [], 'error': 'runner unreachable'}), 503

@bp.route('/admin/runner/stream')
@admin_required
def admin_runner_stream():
    # The browser can't talk directly to vllm-runner (port 8001):
    # that port is restricted to the Docker bridge + localhost, and EventSource can't
    # set an Authorization header. dgx-portal, however, is on the bridge and has
    # the token — so we relay the SSE stream here, internally, without ever exposing
    # RUNNER_TOKEN to the browser.
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    try:
        upstream = requests.get(f"{RUNNER_URL}/stream", headers=_runner_headers(),
                                stream=True, timeout=(5, None))
    except Exception:
        upstream = None
    # If the runner is down, degrade to a closed SSE error frame instead of a 500
    # that would be indistinguishable from a broken stream on the client side.
    if upstream is None or not upstream.ok:
        return Response('data: {"error": "runner unreachable"}\n\ndata: [DONE]\n\n',
                        mimetype="text/event-stream", headers=headers)

    def generate():
        buf = ''
        try:
            for chunk in upstream.iter_content(chunk_size=None, decode_unicode=True):
                if not chunk:
                    continue
                buf += chunk
                while '\n\n' in buf:
                    evt, buf = buf.split('\n\n', 1)
                    data_line = next((l for l in evt.split('\n') if l.startswith('data:')), '')
                    if _LOG_NOISE_RE.search(data_line):
                        continue                 # routine access line → we don't display it
                    yield evt + '\n\n'
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers=headers)

# Cautious default vLLM args for a validated model (to tune afterwards).
# max-model-len deliberately conservative (GB10 unified memory → OOM risk
# if we leave the model's native window).
# Tool-calling enabled by default (qwen3_coder parser = Qwen fleet). For a
# non-Qwen model, adjust --tool-call-parser (e.g. hermes) from admin before launching.
DEFAULT_VLLM_ARGS = "--enable-auto-tool-choice --tool-call-parser qwen3_coder --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.7 --max-num-seqs 4"
# llama.cpp: -ngl 999 = the whole model on the GPU; --jinja enables chat
# templates and tool-calling; --parallel = concurrent sessions (equiv. max-num-seqs).
DEFAULT_LLAMA_ARGS = "--ctx-size 32768 --n-gpu-layers 999 --parallel 4 --flash-attn --jinja"

def _model_slug(hf_id):
    base = (hf_id or '').split('/')[-1]
    return (re.sub(r'[^a-zA-Z0-9_-]', '-', base).strip('-').lower()[:40]) or 'modele'

# VLLM_API_BASE / AUTO_MODEL_NAME : cf. config.py
# Name of the virtual model that always routes to the current chat model (re-pointed
# on each launch). Clients wire it once and no longer need to change the
# model name on each switch.
def hf_engine_for(hf_id):
    """Queries the Hub to know whether the model is GGUF (→ llama.cpp) or
    safetensors (→ vLLM). On network failure, we fall back on vLLM.
    """
    # hf_id is interpolated into the URL: we bound it to the Hub "org/name" form
    # so no value can walk the path up (../) or divert the
    # request elsewhere in the HF API.
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', hf_id or ''):
        return 'vllm'
    try:
        r = requests.get(f'https://huggingface.co/api/models/{hf_id}', timeout=6)
        if r.ok:
            return guess_engine(r.json())
    except Exception:
        pass
    return 'vllm'

def _add_model_to_catalog(db, hf_id):
    """Adds a validated model to the launchable catalog (unique name). Returns
    (name, already_present). The engine is deduced from the HF tags.
    """
    row = db.execute("SELECT name FROM model_configs WHERE hf_model_id=?", (hf_id,)).fetchone()
    if row:
        return row['name'], True
    base = _model_slug(hf_id)
    name = base
    n = 2
    while db.execute("SELECT 1 FROM model_configs WHERE name=?", (name,)).fetchone():
        name = f"{base}-{n}"; n += 1
    engine = hf_engine_for(hf_id)
    args = DEFAULT_LLAMA_ARGS if engine == 'llamacpp' else DEFAULT_VLLM_ARGS
    db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
               "VALUES (?,?,?,?,?)",
               (name, hf_id, args, engine, datetime.now().isoformat()))
    return name, False

@bp.route('/admin/update/<int:req_id>', methods=['POST'])
@admin_required
def update_request(req_id):
    status = request.form.get('status')
    if status not in ('pending', 'done', 'rejected'):
        return _json_error("Statut invalide.")
    db = get_db()
    req = db.execute("SELECT username, model_id FROM model_requests WHERE id=?", (req_id,)).fetchone()
    if not req:
        return _json_error("Demande introuvable.", 404)

    name, already, routing_ok = None, False, True
    if status == 'done' and req['model_id']:
        # We REGISTER first, we announce after. The request was marked
        # « Lancé ✓ » and the email « ton modèle est disponible » went out BEFORE
        # knowing whether the LiteLLM registration had succeeded; the failure
        # only appeared in a flash the UI did not render. The requester thus
        # learned they could use a model that was not routed.
        name, already = _add_model_to_catalog(db, req['model_id'])
        cfg = db.execute("SELECT vllm_args, engine FROM model_configs WHERE name=?", (name,)).fetchone()
        routing_ok = _register_litellm_model(
            name, cfg['vllm_args'] if cfg else DEFAULT_VLLM_ARGS,
            (cfg['engine'] if cfg else 'vllm') or 'vllm')
        if not routing_ok:
            db.commit()          # the catalog itself is up to date
            log_audit(session.get('username'), 'request.routage_echec',
                      f"demande #{req_id} ({name}) : catalogue OK, LiteLLM non enregistré")
            return _json_error(
                f"Le modèle « {name} » est dans le catalogue, mais son enregistrement "
                f"LiteLLM a échoué : la demande reste EN ATTENTE et le demandeur n'est "
                f"pas prévenu (il ne pourrait pas l'utiliser). Réessaie.", 503)

    db.execute("UPDATE model_requests SET status=?, updated_at=? WHERE id=?",
               (status, datetime.now().isoformat(), req_id))
    db.commit()

    if req['username']:
        if status == 'done':
            add_notification(req['username'], 'request',
                             f"Ton modèle est disponible : {req['model_id'] or 'demande validée'}.")
        elif status == 'rejected':
            add_notification(req['username'], 'request', "Ta demande de modèle a été refusée.")

    log_audit(session.get('username'), 'request.status',
              f"demande #{req_id} → {status}" + (f" ({name})" if name else ""))

    if status != 'done' or not req['model_id']:
        return _json_ok("Demande mise à jour.")

    # Notifies the requester by email that their model is available.
    email = ldap_lookup_email(req['username'])
    if email:
        instance_name = platform_name()
        send_user_email(email, f"[{instance_name}] Ton modèle est disponible",
                        f"Bonne nouvelle — le modèle que tu as demandé est validé et "
                        f"disponible sur la plateforme {instance_name} :\n\n  {req['model_id']}\n\n"
                        f"Tu peux l'utiliser via l'API / le Playground une fois lancé.\n"
                        f"{PUBLIC_WEB_URL}/\n")
    if already:
        return _json_ok(f"Modèle déjà dans le catalogue sous « {name} ».")
    add_announcement('model_add', name)
    return _json_ok(f"Modèle « {name} » ajouté au catalogue et routé par LiteLLM — "
                    f"vérifie ses args vLLM puis lance-le.")
