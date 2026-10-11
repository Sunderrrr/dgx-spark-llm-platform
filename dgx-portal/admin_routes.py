"""Administration: models, sidecars, accounts, announcements, global settings.

Extracted from app.py on 28/08 — last big block of the monolith, 48 routes. It
groups two banners that formed only one in practice: model and sidecar
management, and account management.

No url_prefix: all URLs stay identical to the character. The `admin` view
(GET /admin, 204) was REMOVED on 2026-09-17: it existed only as the target of
the `url_for('admin')` calls of the action routes, and those routes have all
answered JSON since 2026-09-13 — not a single `url_for('admin')` remains.
Verified live before removing it: <public web origin>/admin
answers `text/html` (it is the Next.js page serving /admin), so the Flask
endpoint was unreachable.
"""
import json
import os
import re
import ipaddress
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from flask import (Blueprint, Response, jsonify, request,
                   session, stream_with_context)
from werkzeug.security import generate_password_hash

from announcements import add_announcement
from auth import (USERNAME_RE,
                  _revoke_user_sessions,
                  admin_required, block_account, complete_session_origin,
                  unblock_account, is_blocked, is_admin_username, ldap_lookup_email,
                  login_required)
from config import KEY_BUDGET, KEY_DURATION, PUBLIC_WEB_URL, RUNNER_URL
from instance_config import smtp_config
from db import (add_notification, get_db, get_setting, log_audit, maintenance_active,
                platform_name, set_setting)
from litellm_client import (_litellm_user_info, _register_litellm_model,
                            _unregister_litellm_model,
                            get_user_keys, litellm_update_user_budget)
from local_users import (MAX_BUDGET, _local_group, _local_user_effective_budget,
                         _local_user_is_admin, _parse_budget,
                         _sync_local_user_budget, password_policy_error)
from notify import (notify_discord_test, notify_infra_alert_email, notify_maintenance_email,
                    send_test_email, send_user_email, sender_address)
from user_lifecycle import (count_data, deprovision_account,
                            notify_password_changed, revoke_account_keys)
from sidecars import (IMAGE_MODEL_IDS, VOICE_REPO_IDS, _HF_ID_RE, _LOG_NOISE_RE,
                      _image_launch, _mem_guard, _music_launch, _ocr_launch,
                      _runner_headers, _sidecar_start_json,
                      _sidecar_status, _sidecar_stop_json, _voice_launch,
                      asr_load_error, asr_model_name, get_image_model,
                      get_music_model, get_ocr_model, get_voice_model,
                      runner_delete_files, runner_launch, runner_logs, runner_metrics,
                      runner_status, runner_stop)
from directory import directory_states_for
from directory_groups import recompute_members
from owner import is_owner, owner
from stats import (_account_activity, _active_users,
                   admin_get_user_consumption, user_hourly)
from vllm_health import get_running_models, guess_engine, vllm_health

bp = Blueprint('admin', __name__)

# Statistics (aggregates, rankings, active users): see stats.py.
# The names used are imported at the top (see above, lines 43-45); the
# monolith's aggregates (rankings, inflight, buckets) were moved to their
# own module and are no longer used here.

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

@bp.route('/api/admin')
@admin_required
def api_admin():
    db = get_db()
    # BOUNDED lists: they grew with the whole history (every request from
    # every account, since forever) and went in full to the browser at every
    # 8 s refresh. The COUNTERS, in contrast, stay exact: they are computed
    # in SQL over the whole table, not over the displayed page.
    all_reqs    = db.execute("SELECT * FROM model_requests ORDER BY created_at DESC LIMIT 200").fetchall()
    model_cfgs  = db.execute("SELECT * FROM model_configs ORDER BY name").fetchall()
    ocr_cfgs    = db.execute("SELECT * FROM ocr_configs ORDER BY name").fetchall()
    voice_cfgs  = db.execute("SELECT * FROM voice_configs ORDER BY name").fetchall()
    budget_reqs = db.execute("SELECT * FROM budget_requests ORDER BY created_at DESC LIMIT 200").fetchall()
    # A SINGLE scan for the three counters of `model_requests`: the admin
    # page calls /api/admin every 8 s, and four COUNT(*) on the same
    # table made it read four times. `SUM(<condition>)` counts exactly
    # like a filtered COUNT; `or 0` covers the empty table (SUM returns NULL
    # where COUNT returned 0).
    account = db.execute(
        "SELECT SUM(status='pending') AS pending, SUM(status='done') AS done, "
        "SUM(status='rejected') AS rejected FROM model_requests").fetchone()
    stats = {
        'pending':  account['pending'] or 0,
        'done':     account['done'] or 0,
        'rejected': account['rejected'] or 0,
        'budget_pending': db.execute("SELECT COUNT(*) AS n FROM budget_requests "
                                     "WHERE status='pending'").fetchone()['n'],
        'requests_shown': len(all_reqs),
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
        # Dictation model ACTUALLY loaded (read from the sidecar): the Dictée
        # tab displayed « whisper-large-v3-turbo » hardcoded — true today, wrong
        # at the first model change. None when nothing is served.
        'asr_model_name': asr_model_name,
        # WHY dictation is unavailable even though the container is
        # running: the sidecar publishes the cause of the load failure
        # (typically a CUDA out of memory). None if there is no failure.
        'asr_load_error': asr_load_error,
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
        'maintenance_mode': maintenance_active(),
        'model_cfgs': [dict(r) for r in model_cfgs],
        'ocr_cfgs': [dict(r) for r in ocr_cfgs],
        'voice_cfgs': [dict(r) for r in voice_cfgs],
        'image_model_ids': sorted(IMAGE_MODEL_IDS),
        'budget_reqs': [dict(r) for r in budget_reqs],
        # TEMPORARY grants in progress: the admin sees what will return to the
        # base cap, and when.
        'budget_grants': [dict(r) for r in db.execute(
            "SELECT * FROM budget_grants WHERE expires_at > ? ORDER BY expires_at",
            (datetime.utcnow().isoformat(),)).fetchall()],
        'default_key_budget': get_setting('default_key_budget', KEY_BUDGET),
        'default_key_duration': get_setting('default_key_duration', KEY_DURATION),
        **probed,
    })


# --- Platform state ---------------------------------------------------------
# What the HOST MONITOR already watches but nobody could READ: its only
# channel is e-mail, so outside an incident the administrator knew neither when
# the last backup happened, nor how much space was left, without opening a
# shell. The two paths are mounted read-only in the container and the
# dumps are 0600 root: we only read NAMES and dates, never dump
# content.
_BACKUP_DIR = os.environ.get('BACKUP_DIR', '/var/backups/dgx')
_MONITOR_STATE = os.environ.get('MONITOR_STATE', '/var/lib/dgx-monitor/state.json')
_BACKUP_THRESHOLD_H = 26      # the dump runs at 03:00: beyond 26 h, it is late


def _disk_state():
    """Space on the data volume — same filesystem as the host."""
    try:
        st = os.statvfs('/app/data')
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        return {'free_gb': round(free / 1024 ** 3, 1),
                'total_gb': round(total / 1024 ** 3, 1),
                'used_pct': round(100 * (total - free) / total, 1) if total else None}
    except Exception:                                            # noqa: BLE001
        return {'free_gb': None, 'total_gb': None, 'used_pct': None}


def _backup_state():
    """Freshness of the portal's latest dump (name + date, never the content).

    The normal source is the **host monitor state**, which runs as root every
    5 min and copies what it measured there: `/var/backups/dgx` is 0700
    root and the dumps are 0600 (they contain the whole database), so the
    container CANNOT list them. We keep direct folder reading as fallback: if
    the mount exists and is readable one day, it serves; otherwise
    `readable: false`, which the UI displays as « illisible » — never
    « tout va bien ».
    """
    try:
        with open(_MONITOR_STATE, encoding='utf-8') as f:
            backup = (json.load(f) or {}).get('backup') or {}
        if backup.get('latest'):
            age = backup.get('age_hours')
            return {'latest': backup['latest'], 'age_hours': age,
                    'fresh': bool(backup.get('up')) if age is not None else None,
                    'count': backup.get('count'), 'readable': True}
    except Exception:                                            # noqa: BLE001
        pass
    try:
        names = [n for n in os.listdir(_BACKUP_DIR)
                if n.startswith('portal-') and n.endswith('.db')]
        if not names:
            return {'latest': None, 'age_hours': None, 'fresh': False,
                    'count': 0, 'readable': True}
        mtimes = {n: os.stat(os.path.join(_BACKUP_DIR, n)).st_mtime for n in names}
        last = max(mtimes, key=mtimes.get)
        age = (time.time() - mtimes[last]) / 3600
        return {'latest': last, 'age_hours': round(age, 1),
                'fresh': age <= _BACKUP_THRESHOLD_H, 'count': len(names), 'readable': True}
    except Exception:                                            # noqa: BLE001
        return {'latest': None, 'age_hours': None, 'fresh': None,
                'count': None, 'readable': False}


def _monitor_state():
    """Current incidents according to the host monitor's sticky state."""
    try:
        with open(_MONITOR_STATE, encoding='utf-8') as f:
            state = json.load(f)
        down = state.get('down') or []
        if isinstance(down, dict):
            down = [k for k, v in down.items() if v]
        return {'readable': True,
                'active_incidents': sorted(str(i) for i in down),
                # Timestamp WITH its timezone: the container runs in UTC and
                # bare `fromtimestamp` produced a « 21:46:31 » that nothing
                # distinguished from a local time — i.e. 2 h of offset for a
                # French reader. This is the date of the monitor's last SWEEP
                # (it rewrites its state at each pass, every 5 min).
                'since': datetime.fromtimestamp(
                    os.stat(_MONITOR_STATE).st_mtime, timezone.utc).isoformat(timespec='seconds')}
    except Exception:                                            # noqa: BLE001
        return {'readable': False, 'active_incidents': None, 'since': None}


def _model_state():
    """Served model + since when, according to what the PORTAL observed."""
    st = runner_status()
    name, status_value = st.get('model'), st.get('status')
    uptime = None
    if status_value == 'running':
        try:
            observe = json.loads(get_setting('model_servi', '') or '{}')
            if observe.get('nom') == name and observe.get('depuis'):
                uptime = max(0, int(time.time() - float(observe['depuis'])))
        except Exception:                                        # noqa: BLE001
            uptime = None
    return {'name': name, 'status': status_value, 'uptime_s': uptime}


def _portal_state():
    db = get_db()
    size = None
    try:
        path = db.execute("PRAGMA database_list").fetchone()[2]
        size = round(os.path.getsize(path) / 1024 ** 2, 1) if path else None
    except Exception:                                            # noqa: BLE001
        size = None
    now_ts = time.time()
    return {
        'db_mb': size,
        # Live sessions only: revoked ones (logout, admin cut) were counted too
        # — « 247 active sessions » for 229 real ones (2026-10-12).
        'active_sessions': db.execute(
            "SELECT COUNT(*) AS n FROM user_sessions WHERE revoked = 0 AND expires_at > ?",
            (now_ts,)).fetchone()['n'],
        # The people behind them: one browser per machine makes the session
        # count read like an audience it is not.
        'connected_users': db.execute(
            "SELECT COUNT(DISTINCT username) AS n FROM user_sessions "
            "WHERE revoked = 0 AND expires_at > ?", (now_ts,)).fetchone()['n'],
        'local_users': db.execute(
            "SELECT COUNT(*) AS n FROM local_users WHERE enabled=1").fetchone()['n'],
        'active_keys': db.execute(
            "SELECT COUNT(*) AS n FROM api_keys").fetchone()['n'],
        'budget_grants': db.execute(       # expires_at: ISO text, UTC
            "SELECT COUNT(*) AS n FROM budget_grants WHERE expires_at > ?",
            (datetime.utcnow().isoformat(),)).fetchone()['n'],
        'audit_entries': db.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()['n'],
    }



def _memory_state():
    """Free memory (MemAvailable): what decides whether an on-demand sidecar or
    a heavy validation journey can start next to the served model."""
    from sidecars import _mem_available_gb
    v = _mem_available_gb()
    return {'available_gib': None if v is None else round(float(v), 1),
            # Same floor as the validation's heavy journeys and the ASR start.
            'low_gib': 8}


def _directory_state():
    """The directory accounts by state (directory.py snapshot): one disabled
    or gone there still holds API keys here until someone looks."""
    db = get_db()
    names = [r['username'] for r in db.execute(
        "SELECT s.username FROM user_sources s LEFT JOIN local_users l "
        "ON l.username = s.username WHERE l.username IS NULL").fetchall()]
    states = directory_states_for(names)
    if states is None:
        return {'readable': False}
    account = {'actif': 0, 'desactive': 0, 'absent': 0}
    for e in states.values():
        account[e] = account.get(e, 0) + 1
    return {'readable': True, **account}

@bp.route('/admin/platform')
@admin_required
def admin_platform():
    """State of the PLATFORM (disk, backup, incidents, served model).

    Distinct from `/api/health`, which only answers « do the services respond »:
    here we answer the questions asked during an incident — is there space
    left, did last night's backup happen, which incidents has the
    monitor opened, how long has the model been running.
    """
    return jsonify({
        'disk': _disk_state(),
        'backup': _backup_state(),
        'monitor': _monitor_state(),
        'model': _model_state(),
        'portal': _portal_state(),
        'memory': _memory_state(),
        'directory': _directory_state(),
        'maintenance': maintenance_active(),
        'checked_at': int(time.time()),
    })


@bp.route('/admin/model/launch', methods=['POST'])
@admin_required
def launch_model():
    name = request.form.get('model_name', '').strip()
    db   = get_db()
    cfg  = db.execute("SELECT * FROM model_configs WHERE name=?", (name,)).fetchone()
    if not cfg:
        return jsonify({'ok': False, 'error': "Modèle introuvable."}), 404
    ok, reason, incertain = runner_launch(cfg['hf_model_id'], cfg['name'], cfg['vllm_args'] or '',
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
                  + (" (délai dépassé, issue inconnue)" if incertain else " (refusé)"))
        # No infra alert on a DOUBT: the launch may still be in progress.
        # A false "launch failure" trains the administrator to ignore
        # alerts — and invites a relaunch, which kills a model being loaded.
        if not incertain:
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
                        202 if incertain else 503, incertain=incertain)

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

def _json_ok(message=None, **extra):
    """Admin action success: {ok: true} + free fields."""
    body = {'ok': True}
    if message:
        body['message'] = message
    body.update(extra)
    return jsonify(body)


def _json_error(message, code=400, **extra):
    """Admin action refusal: {ok: false, error} + free fields.

    SINGLE contract of all admin actions. About twenty routes
    previously answered `flash(...)` + `redirect(...)`: the flash is rendered by
    NO template (the UI is Next.js; `get_flashed_messages` exists nowhere in the
    repo) and the HTML body of the redirect made the client-side
    `res.json()` fail, whose `catch` concluded « action effectuee ».
    Measured consequence: a refused model stop displayed as successful,
    just like a LiteLLM registration failure or a refused budget. The
    model launch had been fixed this way on 04/09; the fix had never
    been generalized to its neighbours.
    """
    body = {'ok': False, 'error': message}
    body.update(extra)
    return jsonify(body), code


def _owner_protected(username):
    """The OWNER (proprietaire.py) is out of reach of the other admins: no
    deletion, demotion, disabling, password change or block. A JSON refusal,
    or None when the action may proceed."""
    if is_owner(username) and session.get('username') != username:
        return _json_error("Ce compte est le propriétaire de l'instance : seul "
                            "lui-même peut le modifier.", 403)
    return None


@bp.route('/admin/announce', methods=['POST'])
@admin_required
def admin_announce():
    title = request.form.get('title', '').strip()[:120]
    body  = request.form.get('body', '').strip()[:600]
    if not title:
        return _json_error("Titre requis pour l'annonce.")
    add_announcement('site', title, body)
    log_audit(session.get('username'), 'announce', f"annonce : {title}")
    return _json_ok("Annonce publiée — elle s'affichera à l'ouverture du site.")

@bp.route('/admin/model/stop', methods=['POST'])
@admin_required
def stop_model():
    ok, reason, incertain = runner_stop()
    log_audit(session.get('username'), 'model.stop',
              "arrêt du modèle de chat" if ok else f"échec de l'arrêt : {reason}")
    if ok:
        return _json_ok("Modèle arrêté.")
    # `incertain` = the runner did not answer within the delay, the stop may
    # still be in progress. We say so as-is rather than claiming a failure:
    # an operator who believes it failed clicks again.
    return _json_error(reason or "Échec de l'arrêt du modèle.", 202 if incertain else 503,
                        incertain=incertain)

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

@bp.route('/admin/music/start', methods=['POST'])
@admin_required
def start_music():
    return _sidecar_start_json('music')

@bp.route('/admin/music/stop', methods=['POST'])
@admin_required
def stop_music():
    return _sidecar_stop_json('music')

@bp.route('/admin/music/launch', methods=['POST'])
@admin_required
def launch_music():
    """Launches a music model (free HF id, like OCR). The container downloads
    the model itself at startup: nothing to do on the shell side."""
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
    return jsonify({'ok': bool(ok), 'error': None if ok else f"Échec de la relance musique : {detail}"}), (200 if ok else 503)

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
    # Same memory guard as OCR, image and music: recreating the voice
    # container reloads a model (~15 Go), and this route was the ONLY one of the
    # four not to set it — on unified memory, an OOM kills the biggest RSS,
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
    # image and music would refuse. The runner keeps its own shape guard
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

@bp.route('/admin/settings', methods=['POST'])
@admin_required
def update_settings():
    budget   = request.form.get('default_key_budget', '').strip()
    duration = request.form.get('default_key_duration', '').strip()
    try:
        budget_val = float(budget)
        # `not (0 < v <= MAX_BUDGET)` rather than two comparisons: NaN fails
        # every comparison, so it passed the « <= 0 » and « > MAX » tests
        # and ended up written as the global cap. The UPPER bound was missing
        # here, on the portal's WIDEST route — the default cap
        # applies to every account without override nor group (audit of
        # 2026-10-02), and this is exactly the 6,7e12 typo the approval
        # guard had been written to prevent.
        if not (0 < budget_val <= MAX_BUDGET):
            raise ValueError
    except (ValueError, OverflowError):
        return _json_error("Le nombre de tokens par défaut doit être un nombre "
                            f"positif, au plus {MAX_BUDGET:.0e}.")
    if not re.match(r'^\d+[smhd]$', duration):
        return _json_error("Durée invalide (ex: 1d, 7d, 30d, 12h).")
    old = get_setting('default_key_budget', None)
    set_setting('default_key_budget', budget_val)
    set_setting('default_key_duration', duration)
    # The GLOBAL cap was not audited, although it applies to every account
    # without override nor group: it is the widest action of the page.
    log_audit(session.get('username'), 'settings.budget',
              f"plafond global {old} → {budget_val:.0f} tokens / {duration}")
    return _json_ok(f"Limite globale mise à jour : {budget_val:,.0f} tokens / {duration}."
                    .replace(',', ' '))

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
    blocked = {b['username']: b for b in db.execute("SELECT * FROM blocked_users").fetchall()}
    # Chosen avatar (NULL = avatar generated from the nickname): the admin saw
    # initials where the person concerned sees their avatar.
    avatars = {a['username']: a['avatar_id'] for a in
               db.execute("SELECT username, avatar_id FROM user_prefs").fetchall()}
    # Anti-brute-force lockout in progress: the key is composite
    # ('ip|compte' or 'user:compte'), we attach it to the account by the suffix.
    locked = {}
    now = datetime.now().timestamp()
    for r in db.execute("SELECT key, locked_until FROM login_attempts WHERE locked_until > ?",
                        (now,)).fetchall():
        account = r['key'].split('|')[-1].removeprefix('user:')
        locked[account] = max(locked.get(account, 0), int((r['locked_until'] - now) // 60) + 1)

    # The groups table is small and was ALREADY read after the loop
    # (for the response): we load it here and pass it to the helpers, which
    # removes the 2 SELECTs per account of `_local_group`.
    groups = db.execute("SELECT name, max_budget, is_admin, sources FROM user_groups "
                        "ORDER BY name").fetchall()
    groups_by_name = {g['name']: g for g in groups}
    # Directory groups (LDAP memberOf, SSO claim), merged: one name per group.
    directory_groups_by_user = {}
    for r in db.execute("SELECT DISTINCT d.username, d.group_name FROM user_directory_groups d "
                        "JOIN user_groups g ON g.name = d.group_name ORDER BY d.group_name").fetchall():
        directory_groups_by_user.setdefault(r['username'], []).append(r['group_name'])
    names = set(managed) | set(recorded) | set(spend)
    # Accounts the portal does not own (LDAP, SSO): their state lives in the
    # directory. One search for all of them, cached a minute; None when the
    # directory cannot be asked (the UI then shows nothing, it never guesses).
    directory_states = directory_states_for([n for n in names if n not in managed])
    owner_name = owner()
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
        local_row = managed.get(name)
        fullname = (local_row['fullname'] if local_row else None) or (recorded[name]['fullname'] if name in recorded else None)
        sp = spend.get(name)
        rs = recorded.get(name)
        last_source = rs['last_source'] if rs else None
        last_is_admin = rs['last_is_admin'] if rs else None
        local_admin = _local_user_is_admin(local_row, groups_by_name) if local_row else None
        # EFFECTIVE role, as it is actually APPLIED to requests. A single
        # authority, and it is the portal: as soon as a `local_users` row
        # exists, `auth.etat_compte` rules on it alone (`_local_user_is_admin`)
        # and rewrites `session['is_admin']` — whatever `last_source`. The Users
        # page claimed the opposite (« the directory wins »): a local admin account
        # removed from `cn=<groupe-admin>` stayed admin in practice, with no signal
        # for the operator who had just done it. So we display what applies, and
        # the comment says the same thing as auth.py.
        if local_row is not None:
            effective_admin = local_admin
            role_source = 'local'
        else:
            # External account: the directory is the only source of rights.
            effective_admin = bool(last_is_admin) if last_is_admin is not None else None
            role_source = last_source if last_source in ('sso', 'ldap') else 'externe'
        # Management origin for the "Géré" column: an account that has logged in
        # via the directory (SSO/LDAP) is effectively managed by Authentik, even
        # if a historical local_users row still exists (edit actions remain
        # available because the row exists, but its rights/budget are delegated).
        # A directory account stays « managed by the directory » even if a
        # historical local row remains; without a local row, the directory is
        # the only possible authority.
        managed_by = 'repertoire' if (local_row is None or last_source in ('sso', 'ldap')) else 'local'
        if name == owner_name:
            effective_admin = True       # the owner is always an admin (auth.etat_compte)
        out.append({
            'username': name,
            'fullname': fullname,
            'avatar_id': avatars.get(name),
            'sources': sorted(srcs),
            'managed': bool(local_row),
            'managed_by': managed_by,
            'id': local_row['id'] if local_row else None,
            'group_name': local_row['group_name'] if local_row else None,
            'enabled': local_row['enabled'] if local_row else 1,
            'is_admin': local_row['is_admin'] if local_row else None,
            'effective_admin': effective_admin,
            'role_source': role_source,
            'last_source': last_source,
            'effective_budget': (_local_user_effective_budget(local_row, groups_by_name) if local_row
                                 else (sp['max_budget'] if sp else None)),
            'unlimited': (sp['unlimited'] if sp else False),
            'spend': (sp['spend'] if sp else 0),
            'key_count': (sp['key_count'] if sp else 0),
            'last_seen': recorded[name]['last_seen'] if name in recorded else None,
            # Refusal and lockout state: without them, the page cannot
            # distinguish a blocked account from a merely inactive one — yet
            # that is exactly what an admin comes to check.
            'blocked': blocked.get(name) is not None,
            'block_reason': (blocked[name]['reason'] if name in blocked else None),
            'blocked_at': (blocked[name]['blocked_at'] if name in blocked else None),
            'locked_minutes': locked.get(name, 0),
            'directory_status': (directory_states or {}).get(name) if local_row is None else None,
            'directory_groups': directory_groups_by_user.get(name, []),
            'owner': name == owner_name,
        })
    # Members per group: local accounts (their one group) + directory accounts.
    members = {}
    for u in out:
        for g in set(([u['group_name']] if u['group_name'] else []) + u['directory_groups']):
            members[g] = members.get(g, 0) + 1
    return jsonify({'users': out,
                    'groups': [{**dict(g), 'sources': [x for x in (g['sources'] or '').split(',') if x],
                                'members': members.get(g['name'], 0)} for g in groups],
                    'default_budget': float(get_setting('default_key_budget', KEY_BUDGET)),
                    # The quota period (« 7d »): every cap of this page is per period,
                    # not per day as the UI used to say.
                    'budget_duration': get_setting('default_key_duration', KEY_DURATION)})

@bp.route('/admin/audit')
@admin_required
def admin_audit():
    """Audit log (admin): the latest sensitive events, filterable
    by user (?username=). Reverse-chronological read, capped."""
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
    pw_err = password_policy_error(password, username)
    if pw_err:
        return jsonify({'ok': False, 'error': pw_err}), 400
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
    quota_ok = _sync_local_user_budget(username, row)
    log_audit(session.get('username'), 'user.create',
              f"création de {username}" + (f" (groupe {group})" if group else "")
              + ('' if quota_ok else ' — QUOTA NON APPLIQUÉ'))
    # A quota that could not be written on the LiteLLM side is a de-facto
    # unlimited account: say so, instead of letting a complete creation be believed.
    return jsonify({'ok': True} if quota_ok else {
        'ok': True,
        'warning': "Compte créé, mais son quota n'a PAS pu être appliqué sur LiteLLM : "
                   "le compte est sans plafond tant que le budget n'est pas redéfini."})

@bp.route('/admin/users/update/<int:uid>', methods=['POST'])
@admin_required
def admin_users_update(uid):
    db = get_db()
    row = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': "Utilisateur introuvable."}), 404
    refusal = _owner_protected(row['username'])
    if refusal:
        return refusal
    sets, vals = [], []
    password = request.form.get('password', '')
    if password:
        pw_err = password_policy_error(password, row['username'])
        if pw_err:
            return jsonify({'ok': False, 'error': pw_err}), 400
        sets.append("password_hash=?"); vals.append(generate_password_hash(password))
    if 'group' in request.form:
        group = request.form.get('group', '').strip() or None
        if group and not _local_group(group):
            return jsonify({'ok': False, 'error': "Groupe inconnu."}), 400
        # Admin rights come from `local_users.is_admin` OR the group
        # (`_local_user_is_admin`). Changing GROUP is therefore a rights removal
        # like any other, and this branch was the only one of the three not to
        # go through the guard: an admin holding their rights from their group
        # could send it empty and lose the administration (audit of
        # 2026-10-02). We simulate the state AFTER the change, not just
        # `is_admin`.
        new_group = _local_group(group) if group else None
        if not (row['is_admin'] or (new_group and new_group['is_admin'])):
            refusal = _admin_removal_refusal(row, "retirer du groupe administrateur")
            if refusal:
                return _json_error(refusal, 409)
        sets.append("group_name=?"); vals.append(group)
    if 'max_budget' in request.form:
        budget, err = _parse_budget(request.form.get('max_budget'))
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        sets.append("max_budget=?"); vals.append(budget)
    if 'is_admin' in request.form:
        new_admin = int(request.form.get('is_admin') in ('1', 'true', 'on'))
        if not new_admin:
            refusal = _admin_removal_refusal(row, "rétrograder")
            if refusal:
                return _json_error(refusal, 409)
        sets.append("is_admin=?"); vals.append(new_admin)
    if 'enabled' in request.form:
        new_active = int(request.form.get('enabled') in ('1', 'true', 'on'))
        if not new_active:
            refusal = _admin_removal_refusal(row, "désactiver")
            if refusal:
                return _json_error(refusal, 409)
        sets.append("enabled=?"); vals.append(new_active)
    if 'fullname' in request.form:
        sets.append("fullname=?"); vals.append(request.form.get('fullname', '').strip()[:120] or None)
    if sets:
        db.execute(f"UPDATE local_users SET {', '.join(sets)} WHERE id=?", (*vals, uid))
        db.commit()
    updated = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    # Locking an account (enabled=0) immediately revokes its sessions:
    # it loses its access without waiting for HTTP expiration.
    revoked = 0
    keys_failed = 0
    if 'enabled' in request.form and not updated['enabled']:
        revoked = _revoke_user_sessions(updated['username'])
        # Disabling cuts PORTAL access, not API access: LiteLLM validates the
        # keys itself and the portal is not in the path. Without this
        # revocation, a « désactivé » account kept a working key
        # (audit of 2026-10-02) — the same hole as blocking, on the other
        # offboarding path.
        keys_ok, keys_failed, keys_total = revoke_account_keys(
            updated['username'], session.get('username'), 'désactivation du compte')
    if password:
        # Changing the password must close the OTHER sessions: without that,
        # a change motivated by doubt about a stolen cookie protected
        # nothing, the old session continuing to validate.
        revoked = max(revoked, _revoke_user_sessions(updated['username']))
    # The quota resync was UNCONDITIONAL: renaming an account, changing its
    # password or fixing its name rewrote the LiteLLM envelope from
    # local_users — which ERASED a grant in progress, since budget_grants only
    # lives on the LiteLLM side. The reaper then noticed the drift, concluded
    # « the admin stepped in meanwhile » and deleted the row: the account
    # lost its quota with nobody being warned.
    reloaded = request.form.get('enabled') in ('1', 'true', 'on')
    if {'group', 'max_budget'} & set(request.form.keys()) or reloaded:
        quota_ok = _sync_local_user_budget(updated['username'], updated)
    else:
        quota_ok = True
    log_audit(session.get('username'), 'user.update',
              f"mise à jour de {updated['username']}"
              + (f" — mot de passe changé par l'admin, {revoked} session(s) fermée(s)"
                 if password else '')
              + (f" — compte désactivé, {revoked} session(s) fermée(s)"
                 if 'enabled' in request.form and not updated['enabled'] else '')
              + (f" — {keys_failed} clé(s) API NON révoquée(s)" if keys_failed else '')
              + ('' if quota_ok else ' — QUOTA NON APPLIQUÉ'))
    if password:
        notify_password_changed(updated['username'], by_admin=session.get('username'))
    warnings = []
    if not quota_ok:
        warnings.append("le quota n'a PAS pu être appliqué sur LiteLLM : "
                              "redéfinis le budget du compte")
    if keys_failed:
        warnings.append(f"{keys_failed} clé(s) API n'ont PAS pu être révoquées : "
                              "vérifie côté LiteLLM avant de considérer ce compte comme parti")
    if warnings:
        return jsonify({'ok': True,
                        'warning': "Modifications enregistrées, mais " + " ; ".join(warnings) + "."})
    return jsonify({'ok': True})


@bp.route('/admin/users/<username>/revoke-sessions', methods=['POST'])
@admin_required
def admin_revoke_sessions(username):
    """Revokes at will all active sessions of an account (even a
    stolen cookie becomes unusable immediately)."""
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    n = _revoke_user_sessions(username)
    # A revocation is a security action: it is logged like the others.
    # It was not, only disabling was.
    log_audit(session.get('username'), 'session.revoke',
              f"{username} — {n} session(s) révoquée(s)")
    return jsonify({'ok': True, 'revoked': n})

def _body():
    """Request body, form OR JSON.

    The Admin sends form data (postForm) and the new UI JSON (fetch): reading
    only request.form silently ignored a `confirm=DELETE` that was indeed sent,
    and the route answered 409 forever.
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form


def _last_local_admin(username):
    """True if `username` is the last active LOCAL administrator.

    The portal would then have nobody left to administer it locally —
    the directory admins (LDAP/SSO) depend on an external directory, which
    is no reason to cut off one's own hand.

    A BLOCKED account does not count (audit of 2026-10-02): it is refused at
    login even with the right password, so it can no longer administer anything.
    Without this exclusion, blocking the last two local admins one after the
    other passed — each saw the other, still `enabled=1`, as a recourse — and
    the platform ended up with no reachable local administrator. The `enabled=0`
    path, in contrast, already filtered itself out.
    """
    db = get_db()
    for r in db.execute(
            "SELECT * FROM local_users WHERE enabled=1 "
            "AND username NOT IN (SELECT username FROM blocked_users)").fetchall():
        if r['username'] != username and _local_user_is_admin(r):
            return False
    return True


def _local_admins_after(simulation):
    """Counts the active local admins AFTER a simulated removal.

    `simulation` = {username: (is_admin, group_name)} of the accounts whose
    state changes. Needed for the case `_dernier_admin_local` cannot see:
    a group where TWO members hold their admin rights. Taken one by one, each
    sees the other as admin and concludes they may leave; removing both of
    them leaves none.
    """
    db = get_db()
    n = 0
    for r in db.execute(
            "SELECT * FROM local_users WHERE enabled=1 "
            "AND username NOT IN (SELECT username FROM blocked_users)").fetchall():
        state = simulation.get(r['username'])
        if state is None:
            if _local_user_is_admin(r):
                n += 1
            continue
        is_admin, group_name = state
        if is_admin:
            n += 1
            continue
        g = _local_group(group_name) if group_name else None
        if g and g['is_admin']:
            n += 1
    return n


def _admin_removal_refusal(row, action):
    """Reason for refusal when removing the admin rights of `row` is forbidden.

    Two cases, same reason: the portal would become locally unadministrable.
    - disabling or demoting ONESELF escapes any repair: no administration
      session is left at all;
    - removing the LAST local admin leaves the platform to the directory
      admins alone, who depend on an external directory.

    Deletion and blocking have this guard; MODIFICATION did not, although a
    single POST sufficed — and the revalidation of the account state on every
    request makes the loss of access immediate (no need to wait for the cookie
    to expire).
    """
    if not _local_user_is_admin(row):
        return None
    if row['username'] == session.get('username'):
        return (f"Tu ne peux pas te {action} toi-même : tu perdrais immédiatement "
                f"l'accès à l'administration. Demande-le à un autre administrateur.")
    if _local_admins_after({row['username']: (0, None)}) == 0:
        return (f"{row['username']} est le dernier administrateur local actif : le "
                f"portail n'aurait plus personne pour l'administrer en local.")
    return None


@bp.route('/admin/users/delete/<int:uid>', methods=['POST'])
@admin_required
def admin_users_delete(uid):
    """Deletes an account: removes ALL its access, then purges its data.

    Before 2026-09-13 this route only did a DELETE in local_users:
    the account's API keys stayed valid (LiteLLM validates them itself) and
    its browser session survived up to 12 h. Three guards were
    added with the rest:
    - no self-deletion (the admin would cut their own access mid-action);
    - no deletion of the last local administrator;
    - `confirm=DELETE` required: the operation takes away personal data
      (memory, conversations, shares, preferences) and must not start from
      an accidental POST.
    """
    db = get_db()
    row = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': "Utilisateur introuvable."}), 404
    username = row['username']
    me = session.get('username')
    if username == me:
        return jsonify({'ok': False,
                        'error': "Tu ne peux pas supprimer ton propre compte."}), 400
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    if _local_user_is_admin(row) and _last_local_admin(username):
        return jsonify({'ok': False,
                        'error': "Dernier administrateur local : nomme un autre "
                                 "administrateur avant de supprimer celui-ci."}), 400
    data_counts = count_data(username)
    if (_body().get('confirm') or '').strip().upper() != 'DELETE':
        return jsonify({'ok': False, 'needs_confirm': True, 'donnees': data_counts,
                        'error': "Confirmation requise : cette suppression emporte "
                                 "les accès ET les données du compte."}), 409
    # The local row first: it carries the access, and everything after it
    # concerns it (keys, envelope, data) without depending on it.
    db.execute("DELETE FROM local_users WHERE id=?", (uid,))
    db.commit()
    report = deprovision_account(username, me)
    response = {'ok': True, 'purged': report}
    # API access is half the problem: if LiteLLM did not answer, some
    # keys may still work, and the admin must know it right away
    # rather than discovering it in the audit log.
    if report['keys_failed'] or not report['litellm_user']:
        response['warning'] = (
            f"{report['keys_failed']} clé(s) n'ont pas pu être révoquées et/ou "
            "l'enveloppe LiteLLM n'a pas pu être supprimée (service injoignable) : "
            "vérifie les clés de ce compte dans LiteLLM avant de le considérer comme parti.")
    return jsonify(response)


@bp.route('/admin/users/<username>/purge', methods=['POST'])
@admin_required
def admin_user_purge(username):
    """Erases the DATA of an account without a local row (directory account).

    An LDAP/SSO account does not exist in `local_users`: the deletion route,
    which starts from a local id, can therefore do nothing for it — and its
    conversations, memory, preferences and keys stayed in the database
    indefinitely after it left. This route erases them, without touching its
    access: BLOCKING remains the offboarding lever, since a directory
    account can log back in and would then find an empty account.
    """
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    if username == session.get('username'):
        return jsonify({'ok': False,
                        'error': "Tu ne peux pas purger ton propre compte."}), 400
    db = get_db()
    if db.execute("SELECT 1 FROM local_users WHERE username=?", (username,)).fetchone():
        return jsonify({'ok': False,
                        'error': "Ce compte est local : utilise la suppression, "
                                 "qui retire aussi l'accès."}), 400
    if (_body().get('confirm') or '').strip().upper() != 'DELETE':
        return jsonify({'ok': False, 'needs_confirm': True,
                        'donnees': count_data(username),
                        'error': "Confirmation requise : cette opération efface "
                                 "définitivement les données du compte."}), 409
    report = deprovision_account(username, session.get('username'),
                                    action='user.purge')
    return jsonify({'ok': True, 'purged': report})


@bp.route('/admin/users/<username>/block', methods=['POST'])
@admin_required
def admin_user_block(username):
    """Refuses an account at login, whatever its source.

    This is the portal's ONLY lever for an LDAP/SSO account: it has no
    row in local_users, hence no `enabled` to flip, and revoking its
    sessions was pointless — it logged right back in. Blocking is
    reversible and does not touch its local state: unblocking restores the
    account exactly as it was.
    """
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    if username == session.get('username'):
        return jsonify({'ok': False, 'error': "Tu ne peux pas bloquer ton propre compte."}), 400
    if is_blocked(username):
        return jsonify({'ok': True, 'revoked_sessions': 0, 'deja_bloque': True})
    row = get_db().execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    if row is not None and _local_user_is_admin(row) and _last_local_admin(username):
        return jsonify({'ok': False,
                        'error': "Dernier administrateur local : nomme un autre "
                                 "administrateur avant de bloquer celui-ci."}), 400
    reason = (_body().get('reason') or '').strip()[:200] or None
    report = block_account(username, reason, session.get('username'))
    response = {'ok': True,
               'revoked_sessions': report['sessions'],
               'revoked_keys': report['keys_revoked'],
               'keys_total': report['keys']}
    # A revocation failure must NEVER be silent: a key that LiteLLM refused
    # to remove remains a live access to the GPU, and the admin must know it
    # right away (audit of 2026-10-02).
    if report['keys_failed']:
        response['warning'] = (f"{report['keys_failed']} clé(s) API n'ont PAS pu être "
                              f"révoquées — vérifie côté LiteLLM avant de considérer "
                              f"ce compte comme parti.")
    return jsonify(response)


@bp.route('/admin/users/<username>/unblock', methods=['POST'])
@admin_required
def admin_user_unblock(username):
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    n = unblock_account(username, session.get('username'))
    return jsonify({'ok': True, 'debloque': bool(n)})


@bp.route('/admin/users/<username>/detail')
@admin_required
def admin_user_detail(username):
    """Everything the portal knows about an account, in one response.

    The Users page lists; this view answers the questions actually asked in
    front of an account: what does it consume, does it have keys, how many
    memories, where did it log in from, and what has been done to it. Key
    values NEVER leave here: only their alias is returned.
    """
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    db = get_db()
    local_row = db.execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    rs = db.execute("SELECT * FROM user_sources WHERE username=?", (username,)).fetchone()
    bl = db.execute("SELECT * FROM blocked_users WHERE username=?", (username,)).fetchone()
    litellm = _litellm_user_info(username)
    data_counts = count_data(username)
    keys = []
    for k in (get_user_keys(username) or []):
        keys.append({'alias': k.get('key_alias'), 'created_at': k.get('created_at'),
                     'spend': k.get('spend', 0)})
    # The row of the calling sid is its own: if the admin opens their own
    # account, it is filled in like in self-service (session opened before
    # the IP/user-agent columns were added). That of ANOTHER account is never
    # touched — one would not write the admin's IP into someone else's session.
    complete_session_origin()
    current_sid = session.get('sid')
    now = datetime.now().timestamp()
    sessions = []
    for r in db.execute(
            "SELECT sid, auth_at, created_at, expires_at, ip, user_agent FROM user_sessions "
            "WHERE username=? AND revoked=0 AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT 20", (username, now)).fetchall():
        sessions.append({
            # Never the full sid: it is the session cookie's secret.
            'id': (r['sid'] or '')[:12],
            'created_at': r['created_at'], 'expires_at': r['expires_at'],
            'ip': r['ip'], 'user_agent': r['user_agent'],
            'current': bool(current_sid and r['sid'] == current_sid),
        })
    # Actions PERFORMED by this account (audit_log.username is the actor, the
    # target is in `detail`): this is the useful reading for an admin account.
    audit = [{'action': r['action'], 'detail': r['detail'], 'at': r['created_at']}
             for r in db.execute(
                 "SELECT action, detail, created_at FROM audit_log WHERE username=? "
                 "ORDER BY id DESC LIMIT 20", (username,)).fetchall()]
    last_source = rs['last_source'] if rs else None
    if local_row is not None:
        # Same single authority as the list (and as `auth.etat_compte`): the
        # local row rules, the directory is not added to it.
        role = 'admin' if _local_user_is_admin(local_row) else 'user'
    else:
        role = 'admin' if (rs and rs['last_is_admin']) else 'user'
    return jsonify({
        'ok': True,
        'username': username,
        'fullname': (local_row['fullname'] if local_row else None) or (rs['fullname'] if rs else None),
        'sources': (rs['sources'] if rs else '') or '',
        'last_source': last_source,
        'last_seen': rs['last_seen'] if rs else None,
        'local': local_row is not None,
        'enabled': bool(local_row['enabled']) if local_row is not None else True,
        'group': local_row['group_name'] if local_row else None,
        'max_budget': local_row['max_budget'] if local_row else None,
        'effective_budget': _local_user_effective_budget(local_row) if local_row else litellm['max_budget'],
        'role': role,
        'blocked': {'blocked': bl is not None,
                    'reason': bl['reason'] if bl else None,
                    'at': bl['blocked_at'] if bl else None,
                    'by': bl['blocked_by'] if bl else None},
        'litellm': {'exists': bool(litellm.get('exists')),
                    'max_budget': litellm.get('max_budget'),
                    'spend': litellm.get('spend', 0),
                    'budget_duration': litellm.get('budget_reset_at') or None},
        'keys': keys,
        'memory_facts': data_counts['memory_facts'],
        'conversations': data_counts['conversations'],
        'sessions': sessions,
        'audit': audit,
        # The same 6-month daily series as « Mon compte »: which days the
        # account was actually active, whatever its source (local, LDAP, SSO).
        'activity': _account_activity(username),
        # Media usage and model requests: what the « Rechercher un utilisateur »
        # card of the Users tab showed — now in the one place an admin opens.
        'media': {k: dict(zip(('count', 'last'), db.execute(
            f"SELECT COUNT(*), MAX(created_at) FROM {t} WHERE username=?", (username,)).fetchone()))
                  for k, t in (('image', 'image_jobs'), ('music', 'music_jobs'),
                               ('video', 'video_jobs'), ('voice', 'voice_jobs'),
                               ('ocr', 'ocr_jobs'))},
        'model_requests': [dict(r) for r in db.execute(
            "SELECT model_id, status, created_at FROM model_requests WHERE username=? "
            "ORDER BY created_at DESC LIMIT 10", (username,)).fetchall()],
        'directory_status': ((directory_states_for([username]) or {}).get(username)
                             if local_row is None else None),
        'donnees': data_counts,
    })

@bp.route('/admin/groups/create', methods=['POST'])
@admin_required
def admin_groups_create():
    name = request.form.get('name', '').strip()
    # 80: directory groups come down with their own names (« authentik Admins »).
    if not re.match(r'^[\w .-]{1,80}$', name):
        return jsonify({'ok': False, 'error': "Nom de groupe invalide (max 80)."}), 400
    budget, err = _parse_budget(request.form.get('max_budget'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    is_admin = request.form.get('is_admin') in ('1', 'true', 'on')
    db = get_db()
    # Groups merge by name whatever the case (an LDAP « Famille » and an SSO
    # « famille » are one group): editing goes to the existing row.
    existing = db.execute("SELECT name FROM user_groups WHERE lower(name)=lower(?)", (name,)).fetchone()
    if existing:
        name = existing['name']
    # Symmetric case of deletion: editing an existing group to REMOVE its
    # admin right (upsert ON CONFLICT) makes its members lose their rights —
    # without going through deletion. Same guard.
    old = _local_group(name)
    if old and old['is_admin'] and not is_admin:
        members = db.execute("SELECT * FROM local_users WHERE group_name=?", (name,)).fetchall()
        if any(_local_user_is_admin(m) for m in members) and \
                _local_admins_after({m['username']: (0, None) for m in members}) == 0:
            return _json_error(
                f"Retirer le droit d'administration du groupe {name} laisserait le "
                f"portail sans aucun administrateur local. Ajoute un autre "
                f"administrateur local d'abord.", 409)
    db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) VALUES (?,?,?,?) "
               "ON CONFLICT(name) DO UPDATE SET max_budget=excluded.max_budget, is_admin=excluded.is_admin",
               (name, budget, int(is_admin), datetime.now().isoformat()))
    db.commit()
    # Propagates the group's new quota to its members (who have no override).
    for u in db.execute("SELECT * FROM local_users WHERE group_name=? AND max_budget IS NULL", (name,)):
        _sync_local_user_budget(u['username'], u)
    # ...and to its directory members (LDAP/SSO), admin right included.
    recompute_members(name)
    log_audit(session.get('username'), 'group.create', f"groupe {name}")
    return jsonify({'ok': True})

@bp.route('/admin/groups/delete/<name>', methods=['POST'])
@admin_required
def admin_groups_delete(name):
    db = get_db()
    members = db.execute("SELECT * FROM local_users WHERE group_name=?", (name,)).fetchall()
    # A group can CARRY the admin rights of its members
    # (user_groups.is_admin): deleting it can therefore leave the portal with
    # no local administrator at all, exactly like demoting the last one. The
    # group deletion checked nothing.
    group = _local_group(name)
    if group and group['is_admin'] and any(_local_user_is_admin(m) for m in members):
        if _local_admins_after({m['username']: (0, None) for m in members}) == 0:
            return _json_error(
                f"Le groupe {name} porte les droits d'administration de ses membres et "
                f"aucun administrateur local ne subsisterait après sa suppression. "
                f"Ajoute un autre administrateur local d'abord.", 409)
    db.execute("UPDATE local_users SET group_name=NULL WHERE group_name=?", (name,))
    db.execute("DELETE FROM user_groups WHERE name=?", (name,))
    db.commit()
    # Directory members lose what the group granted, at once. (A directory
    # group comes back at its members' next login, without quota or right.)
    recompute_members(name)
    # The members lose the group's quota: their NEW effective cap
    # (personal override, else global default) must be rewritten on the LiteLLM
    # side. Group creation did it, deletion did not — the members therefore
    # silently kept an envelope sometimes more generous than the default, that
    # is, a phantom quota.
    failures = []
    for m in members:
        if m['max_budget'] is not None:
            continue                     # personal cap: nothing to propagate
        fresh = db.execute("SELECT * FROM local_users WHERE username=?",
                           (m['username'],)).fetchone()
        if fresh and not _sync_local_user_budget(m['username'], fresh):
            failures.append(m['username'])
    log_audit(session.get('username'), 'group.delete',
              f"suppression du groupe {name} — {len(members)} membre(s) rebasculé(s) "
              f"sur le défaut" + (f", QUOTA NON APPLIQUÉ pour {', '.join(failures)}" if failures else ""))
    return jsonify({'ok': True, 'membres': len(members), 'echecs_quota': failures})

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
    # Notify the operator (ADMIN_EMAIL) of the toggle — the admins are the
    # recipients, not the blocked users.
    sent = notify_maintenance_email(now_on, session.get('username', ''),
                                    session.get('fullname', ''))
    # The audit was missing on the toggle: yet it is the action that cuts
    # access for everyone, and « who enabled the maintenance at 3 a.m. »
    # has until now had no answer in the database.
    log_audit(session.get('username'), 'maintenance',
              'activé' if now_on else 'désactivé')
    # The « email not sent » flash was rendered by nobody: the SMTP warning
    # documented as visible by the operator was not. It now goes out
    # in the JSON response, hence in the UI banner.
    avert = None
    if smtp_config()['configured'] and smtp_config()['admin_email'] and not sent:
        avert = "L'email d'alerte n'a pas pu être envoyé — vérifie la config SMTP."
    return _json_ok("Mode maintenance activé." if now_on else "Mode maintenance désactivé.",
                    maintenance_mode=now_on, warning=avert)

@bp.route('/admin/email/config')
@admin_required
def admin_email_config():
    """Status of the email config (host / user / password / admin) — never
    returns the password."""
    smtp = smtp_config()
    configured = bool(smtp['configured'] and smtp['admin_email'])
    return jsonify({'configured': configured, 'admin_email': smtp['admin_email'],
                    'sender': sender_address(), 'smtp_host': smtp['host'] or ''})

@bp.route('/admin/email/test', methods=['POST'])
@admin_required
def admin_email_test():
    """Sends a test email to the alert recipient to validate SMTP."""
    if not smtp_config()['configured'] or not smtp_config()['admin_email']:
        return jsonify({'ok': False, 'configured': False,
                        'error': "SMTP non configuré (renseigne-le dans Admin → Config, "
                                 "hôte, utilisateur, mot de passe et destinataire des alertes)."}), 400
    ok = send_test_email()
    return jsonify({'ok': bool(ok), 'configured': True})

@bp.route('/internal/authcheck')
def internal_authcheck():
    """Called by Traefik (forwardAuth middleware on the public `api`
    router), never by the browser: decides whether an external request to
    the public API domain passes or gets the maintenance message. Outside
    maintenance mode, always 200 with no check (no cost added to the
    normal path).
    """
    if not maintenance_active():
        return ('', 200)
    # Only Traefik (the 172.19.0.0/16 bridge) may ask: without this, any peer
    # sharing a network with the portal — the OCR sidecar runs third-party model
    # code on one — turns the 200/503 difference into a KEY-VALIDITY ORACLE
    # (audit 2026-10-06, L1). Answer the maintenance message regardless.
    def _from_proxy():
        try:
            ip = ipaddress.ip_address(request.remote_addr or '')
            return (ip in ipaddress.ip_network('172.19.0.0/16')
                    or ip in ipaddress.ip_network('127.0.0.0/8'))
        except ValueError:
            return False
    if not _from_proxy():
        return jsonify({'error': {'message': "Mode maintenance en cours — l'API est "
                                  "temporairement indisponible, réessaie plus tard.",
                                  'type': 'maintenance_mode'}}), 503
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.lower().startswith('bearer ') else ''
    row = get_db().execute("SELECT username FROM api_keys WHERE key_value=?", (token,)).fetchone() if token else None
    if row and is_admin_username(row['username']):
        return ('', 200)
    return jsonify({'error': {'message': "Mode maintenance en cours — l'API est "
                              "temporairement indisponible, réessaie plus tard.",
                              'type': 'maintenance_mode'}}), 503

@bp.route('/admin/valider', methods=['POST'])
@admin_required
def admin_validate():
    """Runs the full validation pipeline on demand, from the admin.

    « je puisse lancer un test qui teste tout et me dise si tout va bien ou là
    où ça n'a pas marché » — scripts/validate.sh is the whole thing (code gates,
    running service, user journeys, health). It takes minutes, so it runs in a
    thread and the UI polls the state: a synchronous route would time out long
    before the journeys finish.

    ONE run at a time: the journeys use the demo account and a shared
    conversation state, two overlapping runs would fight each other and report
    nonsense.
    """
    with _validator['v']:
        if _validator['running']:
            return jsonify({'ok': False, 'error': "Une vérification est déjà en cours."}), 409
        _validator.update(running=True, start=time.time(), output=[], code=None)

    def _launch():
        import subprocess
        # The pipeline needs Docker and the HOST — a container cannot run it.
        # The portal writes a REQUEST FILE and a systemd .path unit picks it up
        # (`dgx-validation.path` → `dgx-validation.service`): the container
        # never executes anything on the host, which is the whole point.
        try:
            import pathlib
            request_row = pathlib.Path('/run/dgx/validation/demande.txt')
            request_row.parent.mkdir(parents=True, exist_ok=True)
            request_row.write_text(str(time.time()))
            # The run itself is the host's job; we watch its log.
            log = pathlib.Path('/run/dgx/validation/courante.log')
            p = subprocess.Popen(['tail', '-F', str(log)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
            start = time.time()
            for line in p.stdout:
                with _validator['v']:
                    _validator['output'].append(line.rstrip())
                    # The log is a tail, not an ever-growing string: the panel
                    # shows the last lines, and a runaway output must not eat
                    # the portal's memory.
                    if len(_validator['output']) > 400:
                        del _validator['output'][:len(_validator['output']) - 400]
                # `tail -F` NEVER exits — without this the run stays « en cours »
                # forever and the panel refuses the next one (measured 2026-10-10,
                # « une vérification est déjà en cours » alors que rien ne tournait).
                # Stop when the verdict lands, or after a hard time bound.
                # scripts/validate.sh prints « OVERALL VERDICT »; the former
                # French pipeline printed « VERDICT GLOBAL » (older logs).
                if ('OVERALL VERDICT' in line or 'VERDICT GLOBAL' in line
                        or time.time() - start > 1200):
                    p.kill()
                    break
            _validator['code'] = 0 if any('ALL GREEN' in x or 'TOUT EST VERT' in x
                                         for x in _validator['output']) else 1
        except Exception as e:                      # noqa: BLE001
            with _validator['v']:
                _validator['output'].append(f"Erreur: {e}")
                _validator['code'] = 1
        finally:
            with _validator['v']:
                _validator['running'] = False

    threading.Thread(target=_launch, daemon=True).start()
    return jsonify({'ok': True})


@bp.route('/admin/valider/etat')
@admin_required
def admin_validate_state():
    """Progress + the output tail + the verdict, polled by the panel."""
    with _validator['v']:
        return jsonify({
            'ok': True,
            'en_cours': _validator['running'],
            'duree_s': round(time.time() - _validator['start']) if _validator['start'] else 0,
            'code': _validator['code'],
            'sortie': _validator['output'][-120:],
            'reussi': _validator['code'] == 0 if _validator['code'] is not None else None,
        })


_validator = {'running': False, 'start': None, 'output': [], 'code': None,
             'v': threading.Lock()}


@bp.route('/admin/config', methods=['GET', 'POST'])
@admin_required
def admin_config():
    """Instance configuration from the admin: LDAP, SSO, SMTP.

    « je veux que sur l'admin on puisse conf le ldap, le sso, le smtp, bref
    tout … c'est mieux que les vars importantes soient stockées sur l'interface
    web » — .env is the starting point, the database wins, and a secret is only
    ever reported as configured/absent, never shown. The panel also says WHERE
    each value comes from (interface / .env / défaut) — the first question when
    a setting « does not take ».
    """
    from instance_config import CONFIGURABLE, set_value, config_state
    if request.method == 'GET':
        return jsonify({'ok': True, 'config': config_state(),
                        'champs': [{'nom': n, 'libelle': l, 'secret': s}
                                   for n, l, s, _e, _d in CONFIGURABLE],
                        # What to declare at the providers: the URIs this
                        # instance answers on (used when the field is empty).
                        'suggestions': {
                            'oidc_redirect_uri': f'{PUBLIC_WEB_URL}/api/oauth2-redirect',
                            'oidc_post_logout': f'{PUBLIC_WEB_URL}/',
                            'discord_redirect_uri': f'{PUBLIC_WEB_URL}/discord/callback',
                        }})
    # An empty field means « back to the .env value »; a secret is only written
    # when the operator types one, never echoed back.
    modifies = 0
    for name, _label, _secret, _env, _default in CONFIGURABLE:
        if name in request.form:
            set_value(name, request.form.get(name, '').strip())
            modifies += 1
    log_audit(session['username'], 'config.modifiee',
              f'{modifies} valeur(s) — LDAP/SSO/SMTP/sécurité')
    return jsonify({'ok': True, 'modifies': modifies})


def _tester_ldap():
    from ldap3 import NONE, SIMPLE, Connection, Server
    from instance_config import ldap_config
    c = ldap_config()
    if not c['uri']:
        return False, "Aucune URI LDAP renseignée."
    if not (c['bind_dn'] and c['bind_pw']):
        return False, "Le compte de service (DN et mot de passe) est requis."
    try:
        conn = Connection(Server(c['uri'], get_info=NONE, connect_timeout=5),
                          user=c['bind_dn'], password=c['bind_pw'],
                          authentication=SIMPLE, auto_bind=True, receive_timeout=15)
    except Exception as exc:                                     # noqa: BLE001
        return False, f"Connexion ou authentification refusée : {exc}"[:300]
    try:
        conn.search(f"{c['users_dn']},{c['base']}", f"({c['login_attr']}=*)",
                    attributes=[c['login_attr']], size_limit=1000)
        n = len(conn.entries)
    except Exception as exc:                                     # noqa: BLE001
        return False, f"Connecté, mais la recherche des comptes échoue : {exc}"[:300]
    finally:
        try:
            conn.unbind()
        except Exception:                                        # noqa: BLE001
            pass
    if not n:
        return False, (f"Connecté, mais aucun compte sous {c['users_dn']},{c['base']} "
                       f"avec l'attribut {c['login_attr']}.")
    return True, f"Connexion réussie : {n} compte(s) trouvé(s)."


def _tester_sso():
    from instance_config import oidc_config
    from sso_client import _metadata
    c = oidc_config()
    if not c['metadata_url']:
        return False, "Aucune URL de métadonnées renseignée."
    md = _metadata(c['metadata_url'])
    missing = [k for k in ('issuer', 'authorization_endpoint', 'token_endpoint') if not md.get(k)]
    if missing:
        return False, ("Métadonnées illisibles ou incomplètes "
                       f"({', '.join(missing)} absent) : vérifie l'URL.")
    if not (c['client_id'] and c['client_secret']):
        return False, f"Fournisseur joignable ({md['issuer']}), mais client ID et secret sont requis."
    return True, f"Fournisseur joignable : {md['issuer']}."


def _tester_smtp():
    c = smtp_config()
    if not c['configured']:
        return False, "Hôte, utilisateur et mot de passe SMTP sont requis."
    if not c['admin_email']:
        return False, "Renseigne le destinataire des alertes : le test lui est envoyé."
    if send_test_email():
        return True, f"E-mail de test envoyé à {c['admin_email']}."
    return False, "L'envoi a échoué : vérifie l'hôte, le port et les identifiants."


def _tester_discord():
    from instance_config import discord_config
    d = discord_config()
    if not d['webhook']:
        return False, "Aucun webhook renseigné."
    if notify_discord_test():
        return True, "Message de test publié sur le webhook."
    return False, "Le webhook a refusé le message : vérifie son URL."


_TESTS = {'ldap': _tester_ldap, 'sso': _tester_sso, 'smtp': _tester_smtp,
          'discord': _tester_discord}


@bp.route('/admin/config/test/<part>', methods=['POST'])
@admin_required
def admin_config_test(part):
    """« Tester » of a section of Admin → Config, on the SAVED values: LDAP
    binds with the service account and counts the accounts, SSO reads the
    provider's metadata, SMTP sends the test e-mail, Discord posts on the
    webhook. Always 200 with {ok, message}: a failed test is an answer."""
    test = _TESTS.get(part)
    if test is None:
        return _json_error("Section inconnue.", 404)
    ok, message = test()
    log_audit(session.get('username'), 'config.test', f"{part} : {'ok' if ok else 'échec'}")
    return jsonify({'ok': ok, 'message': message})


@bp.route('/admin/branding', methods=['POST'])
@admin_required
def admin_branding():
    """The deployment's identity: name in the sidebar, and the logo.

    « personnaliser en fonction de celui qui le déploie » — the operator names
    their own instance; « DGX » is only the default (db.platform_name).
    The logo is stored as the uploaded data URL: one row, no filesystem to
    police, and the size is bounded like every other upload on this portal.
    """
    name = (request.form.get('platform_name') or '').strip()[:60]
    logo = (request.form.get('platform_logo') or '').strip()
    if name and not re.fullmatch(r'[\w .\-\u00c0-\u024f]{1,60}', name):
        return jsonify({'ok': False, 'error': "Nom invalide."}), 400
    if logo and not re.fullmatch(r'data:image/(png|jpeg|webp|svg\+xml|gif);base64,[A-Za-z0-9+/=]+', logo):
        return jsonify({'ok': False, 'error': "Logo attendu en PNG, JPEG, WebP, SVG ou GIF."}), 400
    if logo and len(logo) > 2 * 1024 * 1024:
        return jsonify({'ok': False, 'error': "Logo trop lourd (2 Mo maximum)."}), 400
    db = get_db()
    if name:
        db.execute("INSERT INTO settings (key, value) VALUES ('platform_name', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name,))
    # '-' is the explicit « remove the logo », like the auth header of an MCP
    # server: an empty field would mean « change nothing ».
    if logo == '-':
        db.execute("DELETE FROM settings WHERE key='platform_logo'")
    elif logo:
        db.execute("INSERT INTO settings (key, value) VALUES ('platform_logo', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (logo,))
    db.commit()
    log_audit(session['username'], 'branding.modifie', f'nom={name or "(inchangé)"} '
              f'logo={"retiré" if logo == "-" else ("posé" if logo else "inchangé")}')
    return jsonify({'ok': True})


@bp.route('/admin/budget/approve/<int:req_id>', methods=['POST'])
@admin_required
def approve_budget(req_id):
    amount = request.form.get('amount', '').strip()
    db = get_db()
    breq = db.execute("SELECT * FROM budget_requests WHERE id=?", (req_id,)).fetchone()
    if not breq or breq['status'] != 'pending':
        return _json_error("Demande introuvable ou déjà traitée.", 404)
    try:
        amount_val = float(amount)
        if amount_val <= 0:
            raise ValueError
    except ValueError:
        return _json_error("Le montant à ajouter doit être un nombre positif.")
    if amount_val > 1_000_000_000_000:
        # Guardrail: an absurd amount (typo « 6666726666666 ») makes the
        # account unlimited de facto — it happened on this server without
        # anyone seeing it. Beyond 1e12 tokens, it is a typing
        # error, we refuse.
        return _json_error("Montant irréaliste (> 1e12 tokens) — vérifie la saisie.")
    # Budget at the ACCOUNT level: we increment the LiteLLM user's envelope.
    info = _litellm_user_info(breq['username'])
    current_budget = info.get('max_budget') or 0
    # The reset window always accompanies the update: without it the amount
    # becomes a lifelong cap, never reset to zero. We align on the global
    # default (weekly since 2026-09-08).
    grant_duration = get_setting('default_key_duration', KEY_DURATION)
    # « durée »: empty = permanent (the old behaviour); N days = the extra is
    # NOT a new lifelong limit — a reaper brings the account back to its base
    # cap at the deadline.
    grant = db.execute(
        "SELECT * FROM budget_grants WHERE username=? AND expires_at > ?",
        (breq['username'], datetime.utcnow().isoformat())).fetchone()
    duration = request.form.get('grant_days', '').strip()
    expires_at = None
    if duration:
        try:
            days = int(duration)
            if not 1 <= days <= 365:
                raise ValueError
        except ValueError:
            return _json_error("Durée de la subvention : nombre de jours entre 1 et 365 "
                                "(vide = permanent).")
        expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat()
    if expires_at:
        # TEMPORARY grant: the base cap is frozen ONCE (at the first
        # boost); a 2nd boost during the period stacks on top of it,
        # the deadline still returns to the SAME base cap.
        base = grant['base_budget'] if grant else current_budget
        new_budget = (grant['current_budget'] if grant else base) + amount_val
        if not litellm_update_user_budget(breq['username'], new_budget,
                                          budget_duration=grant_duration):
            return _json_error("Erreur lors de la mise à jour du budget sur LiteLLM.", 503)
        now_iso = datetime.utcnow().isoformat()
        if grant:
            db.execute(
                "UPDATE budget_grants SET current_budget=?, expires_at=?, updated_at=? WHERE id=?",
                (new_budget, expires_at, now_iso, grant['id']))
        else:
            db.execute(
                "INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (breq['username'], base, new_budget, expires_at, now_iso, now_iso))
    else:
        new_budget = current_budget + amount_val
        if not litellm_update_user_budget(breq['username'], new_budget,
                                          budget_duration=grant_duration):
            return _json_error("Erreur lors de la mise à jour du budget sur LiteLLM.", 503)
        if grant:
            # PERMANENT raise during a grant: it must survive the deadline,
            # so it raises the base cap itself.
            db.execute("UPDATE budget_grants SET base_budget=?, current_budget=?, updated_at=? WHERE id=?",
                       (grant['base_budget'] + amount_val, grant['current_budget'] + amount_val,
                        datetime.utcnow().isoformat(), grant['id']))
    db.execute(
        "UPDATE budget_requests SET status='approved', granted_amount=?, updated_at=? WHERE id=?",
        (amount_val, datetime.now().isoformat(), req_id)
    )
    db.commit()
    add_notification(breq['username'], 'request',
                     f"Budget accordé : +{amount_val:,.0f} tokens.".replace(',', ' '))
    # Granting a budget was NOT audited: yet it is the action with the richest
    # incident history on this machine (the 6,7e12 tokens typo),
    # and « who raised whose quota » must have an answer in the database.
    log_audit(session.get('username'), 'budget.approve',
              f"{breq['username']} +{amount_val:.0f} tokens"
              + (f" (temporaire, retour à {grant['base_budget'] if grant else current_budget:.0f} "
                 f"le {expires_at[:16]})" if expires_at else " (permanent)"))
    _fmt = lambda n: f"{n:,.0f}".replace(',', ' ')  # noqa: E731
    if expires_at:
        return _json_ok(f"+{_fmt(amount_val)} tokens accordés à {breq['fullname']} "
                        f"(total temporaire : {_fmt(new_budget)}, retour à "
                        f"{_fmt(grant['base_budget'] if grant else current_budget)} "
                        f"le {expires_at[:16].replace('T', ' ')} UTC).")
    return _json_ok(f"+{_fmt(amount_val)} tokens accordés à {breq['fullname']} "
                    f"(nouveau total : {_fmt(new_budget)} / {grant_duration}).")


@bp.route('/admin/users/<username>/budget/set', methods=['POST'])
@admin_required
def set_user_budget(username):
    """Redefine an account's cap (EXACT amount, not an addition).

    Allows LOWERING a quota (200M → 50M) as well as raising it. Any temporary
    grant in progress is overwritten: the admin decision stands.

    0 is REFUSED. Measured on this instance: `user/update` with `max_budget: 0`
    does leave 0 in the database (LiteLLM does not interpret it as
    « unlimited »), so the account is really capped at zero tokens — a
    « je mets 0 pour lever la limite » cuts access. Blocking has the same
    effect, reversible and with a recorded reason.
    """
    refusal = _owner_protected(username)
    if refusal:
        return refusal
    db = get_db()
    raw = request.form.get('budget', '').strip()
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        return _json_error(
            "Budget : nombre strictement positif attendu (tokens). Pour couper "
            "l'accès d'un compte, utilise le blocage du compte — réversible et "
            "tracé ; un budget de 0 le plafonne réellement à zéro token.")
    if value > 1_000_000_000_000:
        # Same guardrail as approval: beyond 1e12 tokens it is a typo that
        # makes the account unlimited de facto.
        return _json_error("Montant irréaliste (> 1e12 tokens) — vérifie la saisie.")
    grant_duration = get_setting('default_key_duration', KEY_DURATION)
    if not litellm_update_user_budget(username, value, budget_duration=grant_duration):
        return _json_error("Erreur lors de la mise à jour du budget sur LiteLLM.", 503)
    db.execute("DELETE FROM budget_grants WHERE username=?", (username,))
    # A directory account's group quota no longer overwrites this cap at login.
    db.execute("UPDATE user_sources SET budget_manual=1, budget_auto=NULL WHERE username=?",
               (username,))
    db.commit()
    log_audit(session.get('username', '?'), 'budget_set', f'{username}={value:.0f}')
    _fmt = lambda n: f"{n:,.0f}".replace(',', ' ')  # noqa: E731
    return _json_ok(f"Budget de {username} redéfini à {_fmt(value)} tokens / {grant_duration}.")


def revert_expired_grants():
    """Brings back to their base cap the accounts whose grant has expired.

    Returns the number of accounts brought back. Called by the reaper (portal
    thread, every 60 s) and once at startup: after an outage, the missed
    deadline is caught up within the minute of boot.
    """
    db = get_db()
    now = datetime.utcnow().isoformat()
    rows = db.execute("SELECT * FROM budget_grants WHERE expires_at <= ?", (now,)).fetchall()
    if not rows:
        return 0
    grant_duration = get_setting('default_key_duration', KEY_DURATION)
    brought_back = 0
    for g in rows:
        info = _litellm_user_info(g['username'])
        current = info.get('max_budget')
        if current is None or abs(current - g['current_budget']) < 1:
            # Nobody has touched the cap since the boost: we return to the base.
            if not litellm_update_user_budget(g['username'], g['base_budget'],
                                              budget_duration=grant_duration):
                # The row MUST NOT disappear: it carries the deadline AND the
                # base cap, hence the only reason to retry at the next pass.
                # Deleting it despite the failure turned a TEMPORARY grant into
                # a permanent raise, silently — one minute of unreachable
                # LiteLLM sufficed, and nobody could know anymore that a return
                # was due.
                log_audit('reaper', 'budget.grant_revert_echec',
                          f"{g['username']} : retour au plafond de base non appliqué "
                          f"(LiteLLM injoignable ?), nouvelle tentative au prochain passage")
                continue
            brought_back += 1
        # Otherwise the admin intervened in the meantime: the grant is simply
        # forgotten, we do NOT overwrite their decision.
        db.execute("DELETE FROM budget_grants WHERE id=?", (g['id'],))
    db.commit()
    return brought_back

@bp.route('/admin/budget/reject/<int:req_id>', methods=['POST'])
@admin_required
def reject_budget(req_id):
    db = get_db()
    breq = db.execute("SELECT username FROM budget_requests WHERE id=?", (req_id,)).fetchone()
    if not breq:
        return _json_error("Demande introuvable.", 404)
    db.execute(
        "UPDATE budget_requests SET status='rejected', updated_at=? WHERE id=?",
        (datetime.now().isoformat(), req_id)
    )
    db.commit()
    if breq['username']:
        add_notification(breq['username'], 'request', "Ta demande de budget a été refusée.")
    log_audit(session.get('username'), 'budget.reject', breq['username'] or '?')
    return _json_ok("Demande rejetée.")

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


@bp.route('/admin/support/feedback')
@admin_required
def admin_support_feedback():
    """User feedback on the Support (thumbs up/down + comments).

    Serves to know WHICH replies fail: without it, the Support prompt was only
    fixed by intuition. Aggregate + latest negative feedback (the most useful
    to fix), bounded.
    """
    db = get_db()
    total_count = db.execute("SELECT COUNT(*) c, SUM(CASE WHEN vote>0 THEN 1 ELSE 0 END) p "
                     "FROM support_feedback").fetchone()
    by_user = db.execute(
        "SELECT username, COUNT(*) c, SUM(CASE WHEN vote>0 THEN 1 ELSE 0 END) p "
        "FROM support_feedback GROUP BY username ORDER BY c DESC LIMIT 20").fetchall()
    recent = db.execute(
        "SELECT username, vote, comment, question, answer, model, created_at "
        "FROM support_feedback ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify({
        'total': total_count['c'] or 0,
        'positifs': total_count['p'] or 0,
        'par_utilisateur': [dict(r) for r in by_user],
        'recents': [dict(r) for r in recent],
    })
