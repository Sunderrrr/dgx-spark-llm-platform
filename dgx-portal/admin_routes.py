"""Administration : modeles, sidecars, comptes, annonces, reglages globaux.

Extrait de app.py le 28/08 — dernier gros bloc du monolithe, 48 routes. Il
regroupe deux bannieres qui n'en formaient qu'une en pratique : la gestion des
modeles et sidecars, et celle des comptes.

Sur les endpoints : la vue `admin` devient `admin.admin`, et les 34
url_for('admin') du projet — TOUS situes dans ce bloc, verifie avant de bouger —
sont requalifies en url_for('admin.admin'). Pas d'url_prefix : /admin et toutes
les autres URL restent identiques au caractere pres.
"""
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from flask import (Blueprint, Response, flash, jsonify, redirect, request,
                   session, stream_with_context, url_for)
from werkzeug.security import generate_password_hash

from announcements import _announce_launch, add_announcement
from auth import (USERNAME_RE,
                  _revoke_user_sessions,
                  admin_required, is_admin_username, ldap_lookup_email,
                  login_required)
from config import (ADMIN_EMAIL, KEY_BUDGET, KEY_DURATION, RUNNER_URL,
                    SMTP_HOST, SMTP_PASS, SMTP_USER)
from db import add_notification, get_db, get_setting, log_audit, maintenance_active, set_setting
from litellm_client import (_litellm_user_info, _register_litellm_model,
                            _unregister_litellm_model,
                            litellm_update_user_budget)
from local_users import (_local_group, _local_user_effective_budget,
                         _local_user_is_admin, _parse_budget,
                         _sync_local_user_budget)
from notify import (notify_infra_alert_email, notify_maintenance_email,
                    send_test_email, send_user_email)
from sidecars import (IMAGE_MODEL_IDS, VOICE_REPO_IDS, _HF_ID_RE, _LOG_NOISE_RE,
                      _image_launch, _mem_guard, _music_launch, _ocr_launch,
                      _runner_headers, _sidecar_action, _sidecar_start_json,
                      _sidecar_status, _voice_launch, get_image_model,
                      get_music_model, get_ocr_model, get_voice_model,
                      runner_launch, runner_logs, runner_metrics, runner_status,
                      runner_stop)
from stats import (_active_users, admin_get_ocr_usage,
                   admin_get_user_consumption, admin_get_video_usage,
                   admin_get_voice_usage, user_hourly)
from vllm_health import get_running_models, guess_engine, vllm_health

bp = Blueprint('admin', __name__)

# Statistiques (agregats, classements, utilisateurs actifs) : cf. stats.py.
# Les noms utilises sont importes en tete (voir ci-dessus, lignes 43-45) ; les
# agregats du monolithe (classements, inflight, buckets) ont ete rapatries dans
# leur propre module et ne sont plus utilises ici.

@bp.route('/usage/hourly')
@login_required
def usage_hourly():
    return jsonify(user_hourly(session['username']) or {'has_data': False})

@bp.route('/system/stats')
@login_required
def system_stats():
    data = runner_metrics() or {}
    data['model'] = vllm_health()
    data['running'] = get_running_models()
    if session.get('is_admin'):
        data['runner'] = runner_status()
        data['active_users'] = _active_users()
    return jsonify(data)

@bp.route('/admin/consumption')
@admin_required
def admin_consumption():
    return jsonify({'users': admin_get_user_consumption()})

@bp.route('/admin')
@admin_required
def admin():
    # The page itself is rendered by the Next.js frontend (data via
    # /api/admin) — this endpoint only stays registered because url_for
    # ('admin') is used throughout admin/*.py action routes as a redirect
    # target after a POST (approve/reject/launch/etc.).
    return ('', 204)


@bp.route('/api/admin')
@admin_required
def api_admin():
    db = get_db()
    all_reqs    = db.execute("SELECT * FROM model_requests ORDER BY created_at DESC").fetchall()
    model_cfgs  = db.execute("SELECT * FROM model_configs ORDER BY name").fetchall()
    ocr_cfgs    = db.execute("SELECT * FROM ocr_configs ORDER BY name").fetchall()
    voice_cfgs  = db.execute("SELECT * FROM voice_configs ORDER BY name").fetchall()
    budget_reqs = db.execute("SELECT * FROM budget_requests ORDER BY created_at DESC").fetchall()
    stats = {
        'pending':  sum(1 for r in all_reqs if r['status'] == 'pending'),
        'done':     sum(1 for r in all_reqs if r['status'] == 'done'),
        'rejected': sum(1 for r in all_reqs if r['status'] == 'rejected'),
        'budget_pending': sum(1 for r in budget_reqs if r['status'] == 'pending'),
    }
    # These probes are all independent network round-trips (runner,
    # sidecars, LiteLLM DB). In series, the page waited for their SUM; in
    # parallel it only waits for the slowest. The gunicorn worker is
    # gthread, so these threads cost nothing in particular.
    probes = {
        'running_models': get_running_models,
        'spend_data': admin_get_user_consumption,
        'ocr_status': lambda: _sidecar_status('ocr'),
        'ocr_model_name': get_ocr_model,
        'video_status': lambda: _sidecar_status('video'),
        'voice_status': lambda: _sidecar_status('voice'),
        'voice_model_name': get_voice_model,
        'asr_status': lambda: _sidecar_status('asr'),
        'image_status': lambda: _sidecar_status('image'),
        'image_model_name': lambda: get_image_model(),
        'music_status': lambda: _sidecar_status('music'),
        'music_model_name': lambda: get_music_model(),
        'v_status': runner_status,
        'init_logs': lambda: runner_logs(120),
    }
    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futures = {k: pool.submit(fn) for k, fn in probes.items()}
        # One probe raising (e.g. the runner is down and a dependency times out)
        # must not 500 the whole admin page. Degrade that probe to a benign
        # default the frontend can render instead of the whole page failing.
        probed = {}
        for k, future in futures.items():
            try:
                probed[k] = future.result()
            except Exception:
                if k in ('running_models', 'spend_data', 'init_logs'):
                    probed[k] = []
                elif k in ('v_status',) or k.endswith('_status'):
                    probed[k] = {'running': False}
                else:
                    probed[k] = None

    return jsonify({
        'requests': [dict(r) for r in all_reqs],
        'stats': stats,
        'ocr_usage': admin_get_ocr_usage(),
        'video_usage': admin_get_video_usage(),
        'voice_usage': admin_get_voice_usage(),
        'maintenance_mode': maintenance_active(),
        'model_cfgs': [dict(r) for r in model_cfgs],
        'ocr_cfgs': [dict(r) for r in ocr_cfgs],
        'voice_cfgs': [dict(r) for r in voice_cfgs],
        'image_model_ids': sorted(IMAGE_MODEL_IDS),
        'budget_reqs': [dict(r) for r in budget_reqs],
        'default_key_budget': get_setting('default_key_budget', KEY_BUDGET),
        'default_key_duration': get_setting('default_key_duration', KEY_DURATION),
        **probed,
    })


@bp.route('/admin/model/launch', methods=['POST'])
@admin_required
def launch_model():
    name = request.form.get('model_name', '').strip()
    db   = get_db()
    cfg  = db.execute("SELECT * FROM model_configs WHERE name=?", (name,)).fetchone()
    if not cfg:
        flash("Modèle introuvable.", "danger")
        return redirect(url_for('admin.admin'))
    ok = runner_launch(cfg['hf_model_id'], cfg['name'], cfg['vllm_args'] or '',
                       cfg['engine'] or 'vllm')
    if ok:
        _announce_launch(cfg['name'])
        log_audit(session.get('username'), 'model.launch', f"lancement de {name}")
    else:
        notify_infra_alert_email(
            "Chat model launch failed",
            f"{name}: the runner did not accept the launch (unreachable or unavailable).")
        log_audit(session.get('username'), 'model.launch_échec', f"lancement refusé de {name}")
    flash(f"Lancement de {name} en cours…" if ok else "Runner inaccessible (ou moteur indisponible).",
          "success" if ok else "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/api/announcements')
@login_required
def api_announcements():
    db = get_db()
    row = db.execute("SELECT last_seen_id FROM announcement_state WHERE username=?",
                     (session['username'],)).fetchone()
    seen = row['last_seen_id'] if row else 0
    rows = db.execute(
        "SELECT id, kind, a, b, created_at FROM announcements WHERE id > ? "
        "ORDER BY id DESC LIMIT 6", (seen,)).fetchall()
    return {'items': [dict(r) for r in rows]}

@bp.route('/api/announcements/seen', methods=['POST'])
@login_required
def api_announcements_seen():
    db = get_db()
    mx = db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM announcements").fetchone()['m']
    db.execute(
        "INSERT INTO announcement_state (username, last_seen_id) VALUES (?, ?) "
        "ON CONFLICT(username) DO UPDATE SET last_seen_id=excluded.last_seen_id",
        (session['username'], mx))
    db.commit()
    return {'ok': True}

@bp.route('/admin/announce', methods=['POST'])
@admin_required
def admin_announce():
    title = request.form.get('title', '').strip()[:120]
    body  = request.form.get('body', '').strip()[:600]
    if not title:
        flash("Titre requis pour l'annonce.", "warning")
        return redirect(url_for('admin.admin'))
    add_announcement('site', title, body)
    log_audit(session.get('username'), 'announce', f"annonce : {title}")
    flash("Annonce publiée — elle s'affichera à l'ouverture du site.", "success")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/model/stop', methods=['POST'])
@admin_required
def stop_model():
    ok = runner_stop()
    log_audit(session.get('username'), 'model.stop',
              "arrêt du modèle de chat" if ok else "échec de l'arrêt du modèle de chat")
    flash("Modèle arrêté." if ok else "Runner vLLM inaccessible.", "success" if ok else "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/ocr/start', methods=['POST'])
@admin_required
def start_ocr():
    return _sidecar_start_json('ocr')

@bp.route('/admin/ocr/stop', methods=['POST'])
@admin_required
def stop_ocr():
    ok = _sidecar_action('ocr', 'stop')
    flash("OCR arrêté." if ok else "Échec de l'arrêt OCR.", "success" if ok else "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/video/start', methods=['POST'])
@admin_required
def start_video():
    return _sidecar_start_json('video')

@bp.route('/admin/video/stop', methods=['POST'])
@admin_required
def stop_video():
    ok = _sidecar_action('video', 'stop')
    flash("Vidéo arrêtée." if ok else "Échec de l'arrêt vidéo.", "success" if ok else "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/ocr/catalog/add', methods=['POST'])
@admin_required
def add_ocr_cfg():
    name  = re.sub(r'[^a-zA-Z0-9_-]', '-', request.form.get('name', '').strip())[:40]
    hf_id = request.form.get('hf_model_id', '').strip()
    args  = request.form.get('vllm_args', '').strip()
    if not name or not hf_id:
        flash("Nom et HF model ID requis.", "warning")
        return redirect(url_for('admin.admin'))
    db = get_db()
    try:
        db.execute("INSERT INTO ocr_configs (name, hf_model_id, vllm_args, added_at) VALUES (?,?,?,?)",
                   (name, hf_id, args, datetime.now().isoformat()))
        db.commit()
        flash(f"Modèle OCR {name} ajouté au catalogue.", "success")
    except sqlite3.IntegrityError:
        flash("Un modèle OCR avec ce nom existe déjà.", "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/ocr/catalog/delete/<int:cid>', methods=['POST'])
@admin_required
def delete_ocr_cfg(cid):
    db = get_db()
    db.execute("DELETE FROM ocr_configs WHERE id=?", (cid,))
    db.commit()
    flash("Modèle OCR supprimé du catalogue.", "success")
    return redirect(url_for('admin.admin'))

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
    return jsonify({'ok': bool(ok), 'error': None if ok else f"Échec de la relance OCR : {detail}"}), (200 if ok else 502)

@bp.route('/admin/voice/start', methods=['POST'])
@admin_required
def start_voice():
    return _sidecar_start_json('voice')

@bp.route('/admin/voice/stop', methods=['POST'])
@admin_required
def stop_voice():
    ok = _sidecar_action('voice', 'stop')
    flash("Voix arrêtée." if ok else "Échec de l'arrêt voix.", "success" if ok else "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/asr/start', methods=['POST'])
@admin_required
def start_asr():
    return _sidecar_start_json('asr')

@bp.route('/admin/asr/stop', methods=['POST'])
@admin_required
def stop_asr():
    ok = _sidecar_action('asr', 'stop')
    flash("Dictée arrêtée." if ok else "Échec de l'arrêt de la dictée.", "success" if ok else "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/image/start', methods=['POST'])
@admin_required
def start_image():
    return _sidecar_start_json('image')

@bp.route('/admin/image/stop', methods=['POST'])
@admin_required
def stop_image():
    ok = _sidecar_action('image', 'stop')
    flash("Génération d'image arrêtée." if ok else "Échec de l'arrêt de l'image.",
          "success" if ok else "danger")
    return redirect(url_for('admin.admin'))

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
    return jsonify({'ok': bool(ok), 'error': None if ok else f"Échec de la relance image : {detail}"}), (200 if ok else 502)

@bp.route('/admin/music/start', methods=['POST'])
@admin_required
def start_music():
    return _sidecar_start_json('music')

@bp.route('/admin/music/stop', methods=['POST'])
@admin_required
def stop_music():
    ok = _sidecar_action('music', 'stop')
    flash("Génération musicale arrêtée." if ok else "Échec de l'arrêt de la musique.",
          "success" if ok else "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/music/launch', methods=['POST'])
@admin_required
def launch_music():
    """Lance un modèle musique (id HF libre, comme l'OCR). Le conteneur télécharge
    le modèle lui-même au démarrage : rien à faire côté shell."""
    model_id = request.form.get('model_id', '').strip()
    if not _HF_ID_RE.fullmatch(model_id):
        return jsonify({'ok': False, 'error': "Identifiant HuggingFace invalide (attendu : org/nom)."}), 400
    err = _mem_guard('music')
    if err:
        return jsonify({'ok': False, 'error': err}), 507
    ok, detail = _music_launch(model_id)
    log_audit(session.get('username'), 'music.launch',
              f"lancement musique {model_id}" if ok else f"échec du lancement musique : {detail}")
    if not ok:
        notify_infra_alert_email("Music model launch failed", f"{model_id}: {detail}")
    return jsonify({'ok': bool(ok), 'error': None if ok else f"Échec de la relance musique : {detail}"}), (200 if ok else 502)

@bp.route('/admin/voice/catalog/add', methods=['POST'])
@admin_required
def add_voice_cfg():
    name    = re.sub(r'[^a-zA-Z0-9_-]', '-', request.form.get('name', '').strip())[:40]
    repo_id = request.form.get('repo_id', '').strip()
    if not name or repo_id not in VOICE_REPO_IDS:
        flash("Nom et variante requis.", "warning")
        return redirect(url_for('admin.admin'))
    db = get_db()
    try:
        db.execute("INSERT INTO voice_configs (name, repo_id, added_at) VALUES (?,?,?)",
                   (name, repo_id, datetime.now().isoformat()))
        db.commit()
        flash(f"Modèle voix {name} ajouté au catalogue.", "success")
    except sqlite3.IntegrityError:
        flash("Un modèle voix avec ce nom existe déjà.", "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/voice/catalog/delete/<int:cid>', methods=['POST'])
@admin_required
def delete_voice_cfg(cid):
    db = get_db()
    db.execute("DELETE FROM voice_configs WHERE id=?", (cid,))
    db.commit()
    flash("Modèle voix supprimé du catalogue.", "success")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/voice/catalog/launch', methods=['POST'])
@admin_required
def launch_voice_cfg():
    name = request.form.get('voice_name', '').strip()
    cfg = get_db().execute("SELECT * FROM voice_configs WHERE name=?", (name,)).fetchone()
    if not cfg:
        flash("Modèle voix introuvable.", "danger")
        return redirect(url_for('admin.admin'))
    ok, detail = _voice_launch(cfg['repo_id'])
    log_audit(session.get('username'), 'voice.launch',
              f"relance voix {cfg['repo_id']}" if ok else f"échec relance voix : {detail}")
    if not ok:
        notify_infra_alert_email("Voice model launch failed", f"{name}: {detail}")
    flash(f"Relance voix avec {name} en cours…" if ok else f"Échec de la relance voix : {detail}",
          "success" if ok else "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/model/add', methods=['POST'])
@admin_required
def add_model_cfg():
    name   = re.sub(r'[^a-zA-Z0-9_-]', '-', request.form.get('name', '').strip())[:40]
    hf_id  = request.form.get('hf_model_id', '').strip()
    args   = request.form.get('vllm_args', '').strip()
    engine = request.form.get('engine', 'vllm').strip().lower()
    if engine not in ('vllm', 'llamacpp', 'ds4'):
        engine = 'vllm'
    if not name or not hf_id:
        flash("Nom et HF model ID requis.", "warning")
        return redirect(url_for('admin.admin'))
    db = get_db()
    try:
        db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                   "VALUES (?,?,?,?,?)",
                   (name, hf_id, args, engine, datetime.now().isoformat()))
        db.commit()
        add_announcement('model_add', name)
        ok = _register_litellm_model(name, args, engine)
        flash(f"Modèle {name} ajouté ({engine}) et routé par LiteLLM." if ok
              else f"Modèle {name} ajouté (⚠ enregistrement LiteLLM échoué).", "success" if ok else "warning")
    except sqlite3.IntegrityError:
        flash("Un modèle avec ce nom existe déjà.", "danger")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/model/edit/<int:mid>', methods=['POST'])
@admin_required
def edit_model_cfg(mid):
    args = request.form.get('vllm_args', '').strip()
    db = get_db()
    db.execute("UPDATE model_configs SET vllm_args=? WHERE id=?", (args, mid))
    db.commit()
    row = db.execute("SELECT name, engine FROM model_configs WHERE id=?", (mid,)).fetchone()
    if row:
        _register_litellm_model(row['name'], args, row['engine'] or 'vllm')
    flash("Args du modèle mis à jour (routage LiteLLM rafraîchi).", "success")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/model/delete/<int:mid>', methods=['POST'])
@admin_required
def delete_model_cfg(mid):
    db = get_db()
    row = db.execute("SELECT name FROM model_configs WHERE id=?", (mid,)).fetchone()
    db.execute("DELETE FROM model_configs WHERE id=?", (mid,))
    db.commit()
    if row:
        _unregister_litellm_model(row['name'])
    flash("Modèle supprimé (retiré de LiteLLM).", "success")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/settings', methods=['POST'])
@admin_required
def update_settings():
    budget   = request.form.get('default_key_budget', '').strip()
    duration = request.form.get('default_key_duration', '').strip()
    try:
        budget_val = float(budget)
        if budget_val <= 0:
            raise ValueError
    except ValueError:
        flash("Le nombre de tokens par défaut doit être un nombre positif.", "warning")
        return redirect(url_for('admin.admin'))
    if not re.match(r'^\d+[smhd]$', duration):
        flash("Durée invalide (ex: 1d, 7d, 30d, 12h).", "warning")
        return redirect(url_for('admin.admin'))
    set_setting('default_key_budget', budget_val)
    set_setting('default_key_duration', duration)
    flash(f"Limite globale mise à jour : {budget_val:,.0f} tokens / {duration}.".replace(',', ' '), "success")
    return redirect(url_for('admin.admin'))

# ── Local user management (admin) ───────────────────────────────────────────

@bp.route('/api/admin/users')
@admin_required
def api_admin_users():
    """UNIFIED view of all known accounts, with their source(s):
      - local  : account managed here (local_users table, edit actions)
      - ldap   : has already logged in via LDAP
      - sso    : has already logged in via SSO/Authentik
    An account can carry several sources at once (e.g. ldap + sso). Accounts
    that have used the platform (LiteLLM keys/budget) but whose login we haven't
    yet observed since this addition appear as "external".
    """
    db = get_db()
    managed = {u['username']: u for u in db.execute("SELECT * FROM local_users").fetchall()}
    recorded = {r['username']: r for r in db.execute("SELECT * FROM user_sources").fetchall()}
    spend = {s['username']: s for s in (admin_get_user_consumption() or [])}

    names = set(managed) | set(recorded) | set(spend)
    out = []
    for name in sorted(names):
        srcs = set()
        if name in managed:
            srcs.add('local')
        if name in recorded:
            srcs |= {s for s in (recorded[name]['sources'] or '').split(',') if s}
        # Used the platform but no source observed → external (LDAP/SSO).
        if not srcs and name in spend:
            srcs.add('externe')
        mu = managed.get(name)
        fullname = (mu['fullname'] if mu else None) or (recorded[name]['fullname'] if name in recorded else None)
        sp = spend.get(name)
        rs = recorded.get(name)
        last_source = rs['last_source'] if rs else None
        last_is_admin = rs['last_is_admin'] if rs else None
        local_admin = _local_user_is_admin(mu) if mu else None
        # Effective role: the directory (SSO/LDAP) wins over the local record.
        # A local account that logs in via SSO gets the SSO groups' admin right
        # (last_source='sso'), not its local_users flag.
        if mu is not None:
            if last_source in ('sso', 'ldap') and last_is_admin is not None:
                effective_admin = bool(last_is_admin)
                role_source = last_source
            else:
                effective_admin = local_admin
                role_source = 'local'
        else:
            # External account: the directory is the only source of rights.
            effective_admin = bool(last_is_admin) if last_is_admin is not None else None
            role_source = last_source if last_source in ('sso', 'ldap') else 'externe'
        out.append({
            'username': name,
            'fullname': fullname,
            'sources': sorted(srcs),
            'managed': bool(mu),
            'id': mu['id'] if mu else None,
            'group_name': mu['group_name'] if mu else None,
            'enabled': mu['enabled'] if mu else 1,
            'is_admin': mu['is_admin'] if mu else None,
            'effective_admin': effective_admin,
            'role_source': role_source,
            'last_source': last_source,
            'effective_budget': _local_user_effective_budget(mu) if mu else (sp['max_budget'] if sp else None),
            'unlimited': (sp['unlimited'] if sp else False),
            'spend': (sp['spend'] if sp else 0),
            'key_count': (sp['key_count'] if sp else 0),
            'last_seen': recorded[name]['last_seen'] if name in recorded else None,
        })
    groups = db.execute("SELECT name, max_budget, is_admin FROM user_groups ORDER BY name").fetchall()
    return jsonify({'users': out, 'groups': [dict(g) for g in groups],
                    'default_budget': float(get_setting('default_key_budget', KEY_BUDGET))})

@bp.route('/admin/audit')
@admin_required
def admin_audit():
    """Journal d'audit (admin) : les derniers événements sensibles, filtrables
    par utilisateur (?username=). Lecture antéchronologique, plafonnée."""
    username = (request.args.get('username') or '').strip()
    db = get_db()
    if username:
        rows = db.execute(
            "SELECT id, username, action, detail, created_at FROM audit_log WHERE username=? "
            "ORDER BY id DESC LIMIT 100", (username,)).fetchall()
    else:
        rows = db.execute(
            "SELECT id, username, action, detail, created_at FROM audit_log "
            "ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/admin/users/create', methods=['POST'])
@admin_required
def admin_users_create():
    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '')
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Identifiant invalide (a-z, 0-9, . _ - , max 64)."}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': "Mot de passe : 8 caractères minimum."}), 400
    db = get_db()
    if db.execute("SELECT 1 FROM local_users WHERE username=?", (username,)).fetchone():
        return jsonify({'ok': False, 'error': "Cet utilisateur existe déjà."}), 409
    group = (request.form.get('group', '').strip() or None)
    if group and not _local_group(group):
        return jsonify({'ok': False, 'error': "Groupe inconnu."}), 400
    budget, err = _parse_budget(request.form.get('max_budget'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    is_admin = request.form.get('is_admin') in ('1', 'true', 'on')
    fullname = request.form.get('fullname', '').strip()[:120] or None
    db.execute(
        "INSERT INTO local_users (username, password_hash, fullname, is_admin, group_name, max_budget, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,1,?)",
        (username, generate_password_hash(password), fullname, int(is_admin), group, budget,
         datetime.now().isoformat()))
    db.commit()
    row = db.execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    _sync_local_user_budget(username, row)
    log_audit(session.get('username'), 'user.create',
              f"création de {username}" + (f" (groupe {group})" if group else ""))
    return jsonify({'ok': True})

@bp.route('/admin/users/update/<int:uid>', methods=['POST'])
@admin_required
def admin_users_update(uid):
    db = get_db()
    row = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': "Utilisateur introuvable."}), 404
    sets, vals = [], []
    password = request.form.get('password', '')
    if password:
        if len(password) < 8:
            return jsonify({'ok': False, 'error': "Mot de passe : 8 caractères minimum."}), 400
        sets.append("password_hash=?"); vals.append(generate_password_hash(password))
    if 'group' in request.form:
        group = request.form.get('group', '').strip() or None
        if group and not _local_group(group):
            return jsonify({'ok': False, 'error': "Groupe inconnu."}), 400
        sets.append("group_name=?"); vals.append(group)
    if 'max_budget' in request.form:
        budget, err = _parse_budget(request.form.get('max_budget'))
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        sets.append("max_budget=?"); vals.append(budget)
    if 'is_admin' in request.form:
        sets.append("is_admin=?"); vals.append(int(request.form.get('is_admin') in ('1', 'true', 'on')))
    if 'enabled' in request.form:
        sets.append("enabled=?"); vals.append(int(request.form.get('enabled') in ('1', 'true', 'on')))
    if 'fullname' in request.form:
        sets.append("fullname=?"); vals.append(request.form.get('fullname', '').strip()[:120] or None)
    if sets:
        db.execute(f"UPDATE local_users SET {', '.join(sets)} WHERE id=?", (*vals, uid))
        db.commit()
    updated = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    # Verrouiller un compte (enabled=0) révoque immédiatement ses sessions :
    # il perd son accès sans attendre l'expiration HTTP.
    if 'enabled' in request.form and not updated['enabled']:
        _revoke_user_sessions(updated['username'])
    _sync_local_user_budget(updated['username'], updated)
    log_audit(session.get('username'), 'user.update', f"mise à jour de {updated['username']}")
    return jsonify({'ok': True})


@bp.route('/admin/users/<username>/revoke-sessions', methods=['POST'])
@admin_required
def admin_revoke_sessions(username):
    """Révoque à volonté toutes les sessions actives d'un compte (même un
    cookie volé devient inutilisable immédiatement)."""
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    _revoke_user_sessions(username)
    return jsonify({'ok': True})

@bp.route('/admin/users/delete/<int:uid>', methods=['POST'])
@admin_required
def admin_users_delete(uid):
    db = get_db()
    name = db.execute("SELECT username FROM local_users WHERE id=?", (uid,)).fetchone()
    db.execute("DELETE FROM local_users WHERE id=?", (uid,))
    db.commit()
    if name:
        log_audit(session.get('username'), 'user.delete', f"suppression de {name['username']}")
    return jsonify({'ok': True})

@bp.route('/admin/groups/create', methods=['POST'])
@admin_required
def admin_groups_create():
    name = request.form.get('name', '').strip()
    if not re.match(r'^[\w .-]{1,40}$', name):
        return jsonify({'ok': False, 'error': "Nom de groupe invalide (max 40)."}), 400
    budget, err = _parse_budget(request.form.get('max_budget'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    is_admin = request.form.get('is_admin') in ('1', 'true', 'on')
    db = get_db()
    db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) VALUES (?,?,?,?) "
               "ON CONFLICT(name) DO UPDATE SET max_budget=excluded.max_budget, is_admin=excluded.is_admin",
               (name, budget, int(is_admin), datetime.now().isoformat()))
    db.commit()
    # Propagates the group's new quota to its members (who have no override).
    for u in db.execute("SELECT * FROM local_users WHERE group_name=? AND max_budget IS NULL", (name,)):
        _sync_local_user_budget(u['username'], u)
    log_audit(session.get('username'), 'group.create', f"groupe {name}")
    return jsonify({'ok': True})

@bp.route('/admin/groups/delete/<name>', methods=['POST'])
@admin_required
def admin_groups_delete(name):
    db = get_db()
    db.execute("UPDATE local_users SET group_name=NULL WHERE group_name=?", (name,))
    db.execute("DELETE FROM user_groups WHERE name=?", (name,))
    db.commit()
    log_audit(session.get('username'), 'group.delete', f"suppression du groupe {name}")
    return jsonify({'ok': True})

@bp.route('/admin/maintenance/toggle', methods=['POST'])
@admin_required
def toggle_maintenance():
    """Toggles maintenance mode. Touches NO model (vLLM/ComfyUI/OCR
    stay up): it only blocks (1) the portal's chat/OCR/video endpoints
    for non-admins (maintenance_block_sse/json above) and (2)
    the external public API via Traefik forwardAuth → /internal/authcheck.
    """
    now_on = not maintenance_active()
    set_setting('maintenance_mode', '1' if now_on else '0')
    add_announcement('maintenance', 'on' if now_on else 'off')
    # Notifie l'opérateur (ADMIN_EMAIL) de la bascule — les admins sont les
    # destinataires, pas les utilisateurs bloqués.
    sent = notify_maintenance_email(now_on, session.get('username', ''),
                                    session.get('fullname', ''))
    flash("Mode maintenance activé." if now_on else "Mode maintenance désactivé.", "success")
    if all([SMTP_HOST, SMTP_USER, SMTP_PASS, ADMIN_EMAIL]) and not sent:
        flash("L'email d'alerte n'a pas pu être envoyé — vérifie la config SMTP.", "warning")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/email/config')
@admin_required
def admin_email_config():
    """Statut de la config email (hôte / user / mot de passe / admin) — ne
    renvoie jamais le mot de passe."""
    configured = bool(all([SMTP_HOST, SMTP_USER, SMTP_PASS, ADMIN_EMAIL]))
    return jsonify({'configured': configured, 'admin_email': ADMIN_EMAIL})

@bp.route('/admin/email/test', methods=['POST'])
@admin_required
def admin_email_test():
    """Envoie un email de test à ADMIN_EMAIL pour valider le SMTP."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS]) or not ADMIN_EMAIL:
        return jsonify({'ok': False, 'configured': False,
                        'error': "SMTP non configuré (renseigne SMTP_HOST / "
                                 "SMTP_USER / SMTP_PASSWORD / ADMIN_EMAIL)."}), 400
    ok = send_test_email()
    return jsonify({'ok': bool(ok), 'configured': True})

@bp.route('/internal/authcheck')
def internal_authcheck():
    """Called by Traefik (forwardAuth middleware on the public `api`
    router), never by the browser: decides whether an external request to
    api.cronos.website passes or gets the maintenance message. Outside
    maintenance mode, always 200 with no check (no cost added to the
    normal path).
    """
    if not maintenance_active():
        return ('', 200)
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.lower().startswith('bearer ') else ''
    row = get_db().execute("SELECT username FROM api_keys WHERE key_value=?", (token,)).fetchone() if token else None
    if row and is_admin_username(row['username']):
        return ('', 200)
    return jsonify({'error': {'message': "Mode maintenance en cours — l'API est "
                              "temporairement indisponible, réessaie plus tard.",
                              'type': 'maintenance_mode'}}), 503

@bp.route('/admin/budget/approve/<int:req_id>', methods=['POST'])
@admin_required
def approve_budget(req_id):
    amount = request.form.get('amount', '').strip()
    db = get_db()
    breq = db.execute("SELECT * FROM budget_requests WHERE id=?", (req_id,)).fetchone()
    if not breq or breq['status'] != 'pending':
        flash("Demande introuvable ou déjà traitée.", "warning")
        return redirect(url_for('admin.admin'))
    try:
        amount_val = float(amount)
        if amount_val <= 0:
            raise ValueError
    except ValueError:
        flash("Le montant à ajouter doit être un nombre positif.", "warning")
        return redirect(url_for('admin.admin'))
    # Budget at the ACCOUNT level: we increment the LiteLLM user's envelope.
    info = _litellm_user_info(breq['username'])
    current_budget = info.get('max_budget') or 0
    new_budget = current_budget + amount_val
    if not litellm_update_user_budget(breq['username'], new_budget):
        flash("Erreur lors de la mise à jour du budget sur LiteLLM.", "danger")
        return redirect(url_for('admin.admin'))
    db.execute(
        "UPDATE budget_requests SET status='approved', granted_amount=?, updated_at=? WHERE id=?",
        (amount_val, datetime.now().isoformat(), req_id)
    )
    db.commit()
    add_notification(breq['username'], 'request',
                     f"Budget accordé : +{amount_val:,.0f} tokens.".replace(',', ' '))
    flash(f"+{amount_val:,.0f} tokens accordés à {breq['fullname']} (nouveau total : {new_budget:,.0f}).".replace(',', ' '), "success")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/budget/reject/<int:req_id>', methods=['POST'])
@admin_required
def reject_budget(req_id):
    db = get_db()
    breq = db.execute("SELECT username FROM budget_requests WHERE id=?", (req_id,)).fetchone()
    db.execute(
        "UPDATE budget_requests SET status='rejected', updated_at=? WHERE id=?",
        (datetime.now().isoformat(), req_id)
    )
    db.commit()
    if breq and breq['username']:
        add_notification(breq['username'], 'request', "Ta demande de budget a été refusée.")
    flash("Demande rejetée.", "success")
    return redirect(url_for('admin.admin'))

@bp.route('/admin/runner/logs')
@admin_required
def admin_runner_logs():
    return jsonify({'logs': runner_logs(200)})

# Sidecar log tabs in the admin Logs viewer (LLM comes from runner_logs/stream
# above; these relay the containerised sidecars + ComfyUI). The portal has no
# docker access — the runner reads them via scoped sudo (see /etc/sudoers.d/
# vllmrunner-logs) and returns the tail as a list of lines.
_SIDECAR_LOG_KINDS = {'ocr', 'voice', 'image', 'video', 'asr', 'music'}

@bp.route('/admin/sidecar-logs/<kind>')
@admin_required
def admin_sidecar_logs(kind):
    if kind not in _SIDECAR_LOG_KINDS:
        return jsonify({'error': 'unknown kind', 'logs': []}), 400
    try:
        r = requests.get(f"{RUNNER_URL}/{kind}/logs", headers=_runner_headers(), timeout=10)
        if r.ok:
            return jsonify({'logs': r.json().get('logs', [])})
        return jsonify({'logs': [], 'error': 'runner error'}), 502
    except Exception:
        return jsonify({'logs': [], 'error': 'runner unreachable'}), 502

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

# Enregistrement des modeles dans LiteLLM : cf. litellm_client.py
from litellm_client import (  # noqa: E402
    _litellm_upsert, _point_auto_model, _register_litellm_model,
    _unregister_litellm_model,
)

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
        flash("Statut invalide.", "danger")
        return redirect(url_for('admin.admin'))
    db = get_db()
    req = db.execute("SELECT username, model_id FROM model_requests WHERE id=?", (req_id,)).fetchone()
    db.execute("UPDATE model_requests SET status=?, updated_at=? WHERE id=?",
               (status, datetime.now().isoformat(), req_id))
    # Notification in-app au demandeur (cloche) — l'email reste le canal de fond.
    if req and req['username']:
        if status == 'done':
            add_notification(req['username'], 'request', f"Ton modèle est disponible : {req['model_id'] or 'demande validée'}.")
        elif status == 'rejected':
            add_notification(req['username'], 'request', "Ta demande de modèle a été refusée.")
    # Approving a request = adding it to the launchable catalog (like seeded models).
    if status == 'done':
        req = db.execute("SELECT username, model_id FROM model_requests WHERE id=?", (req_id,)).fetchone()
        if req and req['model_id']:
            # Notifies the requester by email that their model is available.
            email = ldap_lookup_email(req['username'])
            if email:
                send_user_email(email, "[Cronos] Ton modèle est disponible",
                                f"Bonne nouvelle — le modèle que tu as demandé est validé et "
                                f"disponible sur la plateforme Cronos :\n\n  {req['model_id']}\n\n"
                                f"Tu peux l'utiliser via l'API / le Playground une fois lancé.\n"
                                f"https://dgx.cronos.website/\n")
            name, existed = _add_model_to_catalog(db, req['model_id'])
            cfg = db.execute("SELECT vllm_args, engine FROM model_configs WHERE name=?", (name,)).fetchone()
            ok = _register_litellm_model(name, cfg['vllm_args'] if cfg else DEFAULT_VLLM_ARGS,
                                         (cfg['engine'] if cfg else 'vllm') or 'vllm')
            routed = "" if ok else " (⚠ enregistrement LiteLLM échoué — à vérifier)"
            if existed:
                flash(f"Modèle déjà dans le catalogue sous « {name} ».{routed}", "info")
            else:
                add_announcement('model_add', name)
                flash(f"Modèle « {name} » ajouté au catalogue et routé par LiteLLM — vérifie ses args vLLM puis lance-le.{routed}", "success")
    db.commit()
    return redirect(url_for('admin.admin'))
