"""Administration — the core of the `admin` blueprint: the dashboard
(/api/admin, platform state, usage), announcements, global settings,
maintenance, e-mail, and the authcheck Traefik asks before the admin vhosts.

The other admin domains live in their own modules and register their routes
on the same `bp` (imported at the bottom of this file): admin_models.py
(models & sidecars), admin_users.py (accounts, groups, budgets) and
admin_config.py (instance configuration, validation). The endpoint names stay
`admin.*`, and no URL has changed.

No url_prefix: all URLs stay identical to the character. The `admin` view
(GET /admin, 204) was REMOVED on 2026-09-17: the action routes all answer
JSON since 2026-09-13, so no `url_for('admin')` remains.
"""
import json
import os
import re
import ipaddress
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import (Blueprint, jsonify, request,
                   session)

from announcements import add_announcement
from auth import (admin_required, is_admin_username, login_required)
from config import KEY_BUDGET, KEY_DURATION
from instance_config import smtp_config
from db import (get_db, get_setting, log_audit, maintenance_active,
                set_setting)
from local_users import (MAX_BUDGET)
from notify import (notify_maintenance_email,
                    send_test_email, sender_address)
from sidecars import (IMAGE_MODEL_IDS, _sidecar_status, asr_load_error, asr_model_name, get_image_model,
                      get_ocr_model, get_voice_model,
                      runner_logs, runner_metrics,
                      runner_status)
from directory import directory_states_for
from owner import is_owner
from stats import (_active_users,
                   admin_get_user_consumption, user_hourly)
from vllm_health import get_running_models, vllm_health

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
            observe = json.loads(get_setting('served_model', '') or '{}')
            if observe.get('name') == name and observe.get('since'):
                uptime = max(0, int(time.time() - float(observe['since'])))
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
    # Only accounts that CAME from the directory: a deleted local account
    # (sources « local », no row left) is not in LDAP by nature, and was
    # counted « absent de l'annuaire » (zoleroy, 2026-10-13).
    names = [r['username'] for r in db.execute(
        "SELECT s.username FROM user_sources s LEFT JOIN local_users l "
        "ON l.username = s.username WHERE l.username IS NULL "
        "AND (s.sources LIKE '%ldap%' OR s.sources LIKE '%sso%')").fetchall()]
    states = directory_states_for(names)
    if states is None:
        return {'readable': False}
    account = {'active': 0, 'disabled': 0, 'absent': 0}
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
        'positive': total_count['p'] or 0,
        'par_utilisateur': [dict(r) for r in by_user],
        'recent': [dict(r) for r in recent],
    })


# The other admin domains register their routes on `bp` when imported: they
# live in their own modules (models & sidecars, accounts & budgets, instance
# configuration) so that no file does everything.
import admin_config  # noqa: E402,F401
import admin_models  # noqa: E402,F401
import admin_users  # noqa: E402,F401
