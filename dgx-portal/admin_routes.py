"""Administration : modeles, sidecars, comptes, annonces, reglages globaux.

Extrait de app.py le 28/08 — dernier gros bloc du monolithe, 48 routes. Il
regroupe deux bannieres qui n'en formaient qu'une en pratique : la gestion des
modeles et sidecars, et celle des comptes.

Pas d'url_prefix : toutes les URL restent identiques au caractere pres. La vue
`admin` (GET /admin, 204) a ete SUPPRIMEE le 2026-09-17 : elle n'existait plus
que comme cible des `url_for('admin')` des routes d'action, et ces routes
repondent toutes du JSON depuis le 2026-09-13 — plus un seul `url_for('admin')`
n'existe. Verifie en direct avant de la retirer : https://dgx.cronos.website/admin
repond `text/html` (c'est la page Next.js qui sert /admin), donc l'endpoint Flask
etait inatteignable.
"""
import json
import os
import re
import sqlite3
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
                  admin_required, bloquer_compte, completer_origine_session,
                  debloque_compte, est_bloque, is_admin_username, ldap_lookup_email,
                  login_required)
from config import (ADMIN_EMAIL, KEY_BUDGET, KEY_DURATION, RUNNER_URL,
                    SMTP_HOST, SMTP_PASS, SMTP_USER)
from db import add_notification, get_db, get_setting, log_audit, maintenance_active, set_setting
from litellm_client import (_litellm_user_info, _register_litellm_model,
                            _unregister_litellm_model,
                            get_user_keys, litellm_update_user_budget)
from local_users import (_local_group, _local_user_effective_budget,
                         _local_user_is_admin, _parse_budget,
                         _sync_local_user_budget, password_policy_error)
from notify import (notify_infra_alert_email, notify_maintenance_email,
                    send_test_email, send_user_email)
from user_lifecycle import (compter_donnees, deprovisionner_compte,
                            prevenir_mot_de_passe_change)
from sidecars import (IMAGE_MODEL_IDS, VOICE_REPO_IDS, _HF_ID_RE, _LOG_NOISE_RE,
                      _image_launch, _mem_guard, _music_launch, _ocr_launch,
                      _runner_headers, _sidecar_start_json,
                      _sidecar_status, _sidecar_stop_json, _voice_launch,
                      asr_model_name, get_image_model, get_music_model,
                      get_ocr_model, get_voice_model, runner_launch, runner_logs,
                      runner_metrics, runner_status, runner_stop)
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

@bp.route('/api/admin')
@admin_required
def api_admin():
    db = get_db()
    # Listes BORNÉES : elles grandissaient avec tout l'historique (chaque demande
    # de chaque compte, depuis toujours) et partaient entières au navigateur à
    # chaque rafraîchissement de 8 s. Les COMPTEURS, eux, restent exacts : ils se
    # calculent en SQL sur la table entière, pas sur la page affichée.
    all_reqs    = db.execute("SELECT * FROM model_requests ORDER BY created_at DESC LIMIT 200").fetchall()
    model_cfgs  = db.execute("SELECT * FROM model_configs ORDER BY name").fetchall()
    ocr_cfgs    = db.execute("SELECT * FROM ocr_configs ORDER BY name").fetchall()
    voice_cfgs  = db.execute("SELECT * FROM voice_configs ORDER BY name").fetchall()
    budget_reqs = db.execute("SELECT * FROM budget_requests ORDER BY created_at DESC LIMIT 200").fetchall()
    # Un SEUL balayage pour les trois compteurs de `model_requests` : la page
    # admin rappelle /api/admin toutes les 8 s, et quatre COUNT(*) sur la même
    # table la faisaient lire quatre fois. `SUM(<condition>)` compte exactement
    # comme un COUNT filtré ; `or 0` couvre la table vide (SUM rend NULL là où
    # COUNT rendait 0).
    compte = db.execute(
        "SELECT SUM(status='pending') AS pending, SUM(status='done') AS done, "
        "SUM(status='rejected') AS rejected FROM model_requests").fetchone()
    stats = {
        'pending':  compte['pending'] or 0,
        'done':     compte['done'] or 0,
        'rejected': compte['rejected'] or 0,
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
        # Modèle dictée RÉELLEMENT chargé (lu sur le sidecar) : l'onglet Dictée
        # affichait « whisper-large-v3-turbo » en dur — vrai aujourd'hui, faux
        # au premier changement de modèle. None quand rien n'est servi.
        'asr_model_name': asr_model_name,
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
        # Subventions TEMPORAIRES en cours : l'admin voit ce qui reviendra au
        # plafond de base, et quand.
        'budget_grants': [dict(r) for r in db.execute(
            "SELECT * FROM budget_grants WHERE expires_at > ? ORDER BY expires_at",
            (datetime.utcnow().isoformat(),)).fetchall()],
        'default_key_budget': get_setting('default_key_budget', KEY_BUDGET),
        'default_key_duration': get_setting('default_key_duration', KEY_DURATION),
        **probed,
    })


# --- État de la plateforme -------------------------------------------------
# Ce que le MONITEUR HÔTE surveille déjà mais que personne ne pouvait LIRE : son
# seul canal est l'email, donc hors panne l'administrateur ne savait ni quand la
# dernière sauvegarde avait eu lieu, ni combien d'espace restait, sans ouvrir un
# shell. Les deux chemins sont montés en lecture seule dans le conteneur et les
# dumps sont en 0600 root : on ne lit que des NOMS et des dates, jamais un
# contenu de dump.
_BACKUP_DIR = os.environ.get('BACKUP_DIR', '/var/backups/cronos')
_MONITOR_STATE = os.environ.get('MONITOR_STATE', '/var/lib/cronos-monitor/state.json')
_SEUIL_SAUVEGARDE_H = 26      # le dump tourne à 03:00 : au-delà de 26 h, il est en retard


def _etat_disque():
    """Espace du volume de données — même système de fichiers que l'hôte."""
    try:
        st = os.statvfs('/app/data')
        total = st.f_frsize * st.f_blocks
        libre = st.f_frsize * st.f_bavail
        return {'free_gb': round(libre / 1024 ** 3, 1),
                'total_gb': round(total / 1024 ** 3, 1),
                'used_pct': round(100 * (total - libre) / total, 1) if total else None}
    except Exception:                                            # noqa: BLE001
        return {'free_gb': None, 'total_gb': None, 'used_pct': None}


def _etat_sauvegarde():
    """Fraîcheur du dernier dump du portail (nom + date, jamais le contenu).

    La source normale est l'**état du moniteur hôte**, qui tourne en root toutes
    les 5 min et y recopie ce qu'il a mesuré : `/var/backups/cronos` est en 0700
    root et les dumps en 0600 (ils contiennent la base entière), donc le conteneur
    ne peut PAS les lister. On garde la lecture directe du dossier en repli : si
    le montage existe et est lisible un jour, elle sert ; sinon `readable: false`,
    que l'interface affiche « illisible » — jamais « tout va bien ».
    """
    try:
        with open(_MONITOR_STATE, encoding='utf-8') as f:
            sauvegarde = (json.load(f) or {}).get('backup') or {}
        if sauvegarde.get('latest'):
            age = sauvegarde.get('age_hours')
            return {'latest': sauvegarde['latest'], 'age_hours': age,
                    'fresh': bool(sauvegarde.get('up')) if age is not None else None,
                    'count': sauvegarde.get('count'), 'readable': True}
    except Exception:                                            # noqa: BLE001
        pass
    try:
        noms = [n for n in os.listdir(_BACKUP_DIR)
                if n.startswith('portal-') and n.endswith('.db')]
        if not noms:
            return {'latest': None, 'age_hours': None, 'fresh': False,
                    'count': 0, 'readable': True}
        mtimes = {n: os.stat(os.path.join(_BACKUP_DIR, n)).st_mtime for n in noms}
        dernier = max(mtimes, key=mtimes.get)
        age = (time.time() - mtimes[dernier]) / 3600
        return {'latest': dernier, 'age_hours': round(age, 1),
                'fresh': age <= _SEUIL_SAUVEGARDE_H, 'count': len(noms), 'readable': True}
    except Exception:                                            # noqa: BLE001
        return {'latest': None, 'age_hours': None, 'fresh': None,
                'count': None, 'readable': False}


def _etat_moniteur():
    """Incidents en cours selon l'état sticky du moniteur hôte."""
    try:
        with open(_MONITOR_STATE, encoding='utf-8') as f:
            etat = json.load(f)
        down = etat.get('down') or []
        if isinstance(down, dict):
            down = [k for k, v in down.items() if v]
        return {'readable': True,
                'active_incidents': sorted(str(i) for i in down),
                # Horodatage AVEC son fuseau : le conteneur tourne en UTC et
                # `fromtimestamp` seul produisait un « 21:46:31 » que rien ne
                # distinguait d'une heure locale — soit 2 h de décalage pour un
                # lecteur français. C'est la date de la dernière SONDUE du
                # moniteur (il réécrit son état à chaque passage, toutes les 5 min).
                'since': datetime.fromtimestamp(
                    os.stat(_MONITOR_STATE).st_mtime, timezone.utc).isoformat(timespec='seconds')}
    except Exception:                                            # noqa: BLE001
        return {'readable': False, 'active_incidents': None, 'since': None}


def _etat_modele():
    """Modèle servi + depuis quand, d'après ce que le PORTAIL a observé."""
    st = runner_status()
    nom, statut = st.get('model'), st.get('status')
    uptime = None
    if statut == 'running':
        try:
            observe = json.loads(get_setting('model_servi', '') or '{}')
            if observe.get('nom') == nom and observe.get('depuis'):
                uptime = max(0, int(time.time() - float(observe['depuis'])))
        except Exception:                                        # noqa: BLE001
            uptime = None
    return {'name': nom, 'status': statut, 'uptime_s': uptime}


def _etat_portail():
    db = get_db()
    taille = None
    try:
        chemin = db.execute("PRAGMA database_list").fetchone()[2]
        taille = round(os.path.getsize(chemin) / 1024 ** 2, 1) if chemin else None
    except Exception:                                            # noqa: BLE001
        taille = None
    maintenant = datetime.utcnow().isoformat()
    return {
        'db_mb': taille,
        'active_sessions': db.execute(
            "SELECT COUNT(*) AS n FROM user_sessions WHERE expires_at > ?",
            (time.time(),)).fetchone()['n'],
        'local_users': db.execute(
            "SELECT COUNT(*) AS n FROM local_users WHERE enabled=1").fetchone()['n'],
        'active_keys': db.execute(
            "SELECT COUNT(*) AS n FROM api_keys").fetchone()['n'],
        'budget_grants': db.execute(
            "SELECT COUNT(*) AS n FROM budget_grants WHERE expires_at > ?",
            (maintenant,)).fetchone()['n'],
        'audit_entries': db.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()['n'],
    }


@bp.route('/admin/platform')
@admin_required
def admin_platform():
    """État de la PLATEFORME (disque, sauvegarde, incidents, modèle servi).

    Distinct de `/api/health`, qui ne répond que « les services répondent-ils » :
    ici on répond aux questions qu'on se pose pendant un incident — reste-t-il de
    la place, la sauvegarde de cette nuit a-t-elle eu lieu, quels incidents le
    moniteur a-t-il ouverts, depuis quand le modèle tourne-t-il.
    """
    return jsonify({
        'disk': _etat_disque(),
        'backup': _etat_sauvegarde(),
        'monitor': _etat_moniteur(),
        'model': _etat_modele(),
        'portal': _etat_portail(),
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
    ok, motif, incertain = runner_launch(cfg['hf_model_id'], cfg['name'], cfg['vllm_args'] or '',
                                         cfg['engine'] or 'vllm')
    if ok:
        # L'annonce « tel modele remplace tel autre » ne part plus ici mais depuis
        # _suivre_lancement, quand le modele SERT reellement : accepter n'est pas
        # servir, et annoncer un modele qui ne demarrera jamais est un mensonge
        # visible par tous les utilisateurs.
        log_audit(session.get('username'), 'model.launch', f"lancement de {name}")
    else:
        log_audit(session.get('username'), 'model.launch_échec',
                  f"lancement de {name} : {motif}"
                  + (" (délai dépassé, issue inconnue)" if incertain else " (refusé)"))
        # Pas d'alerte infra sur un DOUTE : le lancement est peut-etre en cours.
        # Un faux « echec de lancement » habitue l'administrateur a ignorer les
        # alertes — et l'invite a relancer, ce qui tue un modele en chargement.
        if not incertain:
            notify_infra_alert_email(
                "Chat model launch failed", f"{name}: {motif or 'launch refused'}")
    # Réponse JSON, pas une redirection : `act()` côté frontend lit {ok, error} et
    # traite toute réponse NON-JSON comme un succès (son propre commentaire le dit).
    # Cette route redirigeait, donc un lancement refusé s'affichait comme réussi et
    # l'administrateur ne voyait « rien se passer ». Constaté le 04/09 : deux
    # tentatives refusées en 400, aucune trace à l'écran.
    if ok:
        return _json_ok(f"Lancement de {name} accepté — chargement en cours.")
    return _json_erreur(motif or "Lancement refusé par le runner.",
                        202 if incertain else 502, incertain=incertain)

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
    """Succes d'action d'admin : {ok: true} + champs libres."""
    corps = {'ok': True}
    if message:
        corps['message'] = message
    corps.update(extra)
    return jsonify(corps)


def _json_erreur(message, code=400, **extra):
    """Refus d'action d'admin : {ok: false, error} + champs libres.

    Contrat UNIQUE de toutes les actions d'admin. Une vingtaine de routes
    repondaient auparavant `flash(...)` + `redirect(...)` : le flash n'est rendu
    par AUCUN template (l'interface est Next.js ; `get_flashed_messages` n'existe
    nulle part dans le depot) et le corps HTML de la redirection faisait echouer
    `res.json()` cote client, dont le `catch` concluait « action effectuee ».
    Consequence mesuree : un arret du modele refuse s'affichait comme reussi,
    tout comme un echec d'enregistrement LiteLLM ou un budget refuse. Le
    lancement de modele avait ete corrige ainsi le 04/09 ; le correctif n'avait
    jamais ete generalise a ses voisines.
    """
    corps = {'ok': False, 'error': message}
    corps.update(extra)
    return jsonify(corps), code


@bp.route('/admin/announce', methods=['POST'])
@admin_required
def admin_announce():
    title = request.form.get('title', '').strip()[:120]
    body  = request.form.get('body', '').strip()[:600]
    if not title:
        return _json_erreur("Titre requis pour l'annonce.")
    add_announcement('site', title, body)
    log_audit(session.get('username'), 'announce', f"annonce : {title}")
    return _json_ok("Annonce publiée — elle s'affichera à l'ouverture du site.")

@bp.route('/admin/model/stop', methods=['POST'])
@admin_required
def stop_model():
    ok, motif, incertain = runner_stop()
    log_audit(session.get('username'), 'model.stop',
              "arrêt du modèle de chat" if ok else f"échec de l'arrêt : {motif}")
    if ok:
        return _json_ok("Modèle arrêté.")
    # `incertain` = le runner n'a pas repondu dans le delai, l'arret est
    # peut-etre en cours. On le dit tel quel plutot que d'affirmer un echec :
    # un operateur qui croit a un echec reclique.
    return _json_erreur(motif or "Échec de l'arrêt du modèle.", 202 if incertain else 502,
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
        return _json_erreur("Nom et HF model ID requis.")
    db = get_db()
    try:
        db.execute("INSERT INTO ocr_configs (name, hf_model_id, vllm_args, added_at) VALUES (?,?,?,?)",
                   (name, hf_id, args, datetime.now().isoformat()))
        db.commit()
    except sqlite3.IntegrityError:
        return _json_erreur("Un modèle OCR avec ce nom existe déjà.", 409)
    log_audit(session.get('username'), 'ocr.catalog.add', f"{name} ({hf_id})")
    return _json_ok(f"Modèle OCR {name} ajouté au catalogue.")

@bp.route('/admin/ocr/catalog/delete/<int:cid>', methods=['POST'])
@admin_required
def delete_ocr_cfg(cid):
    db = get_db()
    row = db.execute("SELECT name FROM ocr_configs WHERE id=?", (cid,)).fetchone()
    if not row:
        return _json_erreur("Modèle OCR introuvable.", 404)
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
    return jsonify({'ok': bool(ok), 'error': None if ok else f"Échec de la relance OCR : {detail}"}), (200 if ok else 502)

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
    return jsonify({'ok': bool(ok), 'error': None if ok else f"Échec de la relance image : {detail}"}), (200 if ok else 502)

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
        return _json_erreur("Nom et variante requis (variante hors liste autorisée).")
    db = get_db()
    try:
        db.execute("INSERT INTO voice_configs (name, repo_id, added_at) VALUES (?,?,?)",
                   (name, repo_id, datetime.now().isoformat()))
        db.commit()
    except sqlite3.IntegrityError:
        return _json_erreur("Un modèle voix avec ce nom existe déjà.", 409)
    log_audit(session.get('username'), 'voice.catalog.add', f"{name} ({repo_id})")
    return _json_ok(f"Modèle voix {name} ajouté au catalogue.")

@bp.route('/admin/voice/catalog/delete/<int:cid>', methods=['POST'])
@admin_required
def delete_voice_cfg(cid):
    db = get_db()
    row = db.execute("SELECT name FROM voice_configs WHERE id=?", (cid,)).fetchone()
    if not row:
        return _json_erreur("Modèle voix introuvable.", 404)
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
        return _json_erreur("Modèle voix introuvable.", 404)
    # Même garde mémoire que l'OCR, l'image et la musique : recréer le conteneur
    # voix recharge un modèle (~15 Go), et cette route était la SEULE des quatre
    # à ne pas la poser — sur mémoire unifiée, un OOM tue le plus gros RSS,
    # c'est-à-dire le modèle de chat servi.
    err = _mem_guard('voice')
    if err:
        return _json_erreur(err, 507)
    ok, detail = _voice_launch(cfg['repo_id'])
    log_audit(session.get('username'), 'voice.launch',
              f"relance voix {cfg['repo_id']}" if ok else f"échec relance voix : {detail}")
    if not ok:
        notify_infra_alert_email("Voice model launch failed", f"{name}: {detail}")
    if ok:
        return _json_ok(f"Relance voix avec {name} en cours…")
    return _json_erreur(f"Échec de la relance voix : {detail}", 502)

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
        return _json_erreur("Nom et HF model ID requis.")
    # Pas de validation de forme sur hf_id ici, volontairement : `local:<nom>`
    # (GGUF local) est un identifiant legitime, que la regex « org/nom » de
    # l'image et de la musique refuserait. Le runner garde sa propre garde de
    # forme avant de construire argv.
    db = get_db()
    try:
        db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                   "VALUES (?,?,?,?,?)",
                   (name, hf_id, args, engine, datetime.now().isoformat()))
        db.commit()
    except sqlite3.IntegrityError:
        return _json_erreur("Un modèle avec ce nom existe déjà.", 409)
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
    """Modifie les args vLLM/llama.cpp d'une entrée du catalogue.

    Les args ne sont PAS validés ici : l'allow-list des flags vit dans le runner,
    qui la vérifie de toute façon au lancement (`_BOOL_FLAGS`, `_BIN_FLAGS`). La
    dupliquer ici créerait une seconde source de verité qui divergerait.
    """
    args = request.form.get('vllm_args', '').strip()
    db = get_db()
    row = db.execute("SELECT name, engine FROM model_configs WHERE id=?", (mid,)).fetchone()
    if not row:
        return _json_erreur("Modèle introuvable dans le catalogue.", 404)
    db.execute("UPDATE model_configs SET vllm_args=? WHERE id=?", (args, mid))
    db.commit()
    # Le resultat d'enregistrement etait IGNORÉ et la route annoncait
    # « routage LiteLLM rafraîchi » sans condition : or LiteLLM applique les
    # limites par requete (max_input/max_output via ctx_split) issues de CETTE
    # entree — un echec silencieux laissait donc un routage faux.
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
    db = get_db()
    row = db.execute("SELECT name FROM model_configs WHERE id=?", (mid,)).fetchone()
    if not row:
        return _json_erreur("Modèle introuvable dans le catalogue.", 404)
    nom = row['name']
    # Retirer du catalogue le modele ACTUELLEMENT SERVI n'arrete pas le moteur,
    # mais detruit la configuration qui permet de le relancer (args, moteur) et
    # laisse last_model.json pointer sur une entree disparue — le runner
    # relancerait alors au prochain demarrage un modele que le portail ne sait
    # plus relancer a la main. On demande donc une confirmation explicite.
    if nom in (get_running_models() or []) and request.form.get('confirm') != '1':
        return _json_erreur(
            f"{nom} est le modèle actuellement servi. Le retirer du catalogue ne "
            f"l'arrête pas, mais tu perdras de quoi le relancer. Confirme pour continuer.",
            409, needs_confirm=True)
    db.execute("DELETE FROM model_configs WHERE id=?", (mid,))
    db.commit()
    # Le resultat etait ignore : l'interface annoncait « retiré de LiteLLM » meme
    # quand l'entree y survivait, et une entree LiteLLM sans ligne de catalogue
    # est un routage que plus aucun ecran ne permet de nettoyer.
    deregistre = _unregister_litellm_model(nom)
    # Le fil d'annonces est un « quoi de neuf », pas un journal : garder
    # « modèle X ajouté » pour un modèle qu'on vient de retirer annonce aux
    # utilisateurs quelque chose qui n'existe plus. Le fait reste consigné
    # dans l'audit, seul le fil est nettoyé.
    retires = db.execute("DELETE FROM announcements WHERE kind='model_add' AND a=?",
                         (nom,)).rowcount
    db.commit()
    log_audit(session.get('username'), 'model.delete',
              f"{nom} — retiré du catalogue, LiteLLM "
              f"{'dérégistré' if deregistre else 'NON DÉRÉGISTRÉ'}, "
              f"{retires} annonce(s) retirée(s)")
    if deregistre:
        return _json_ok(f"{nom} supprimé du catalogue et retiré de LiteLLM.")
    return _json_ok(f"{nom} supprimé du catalogue.",
                    warning="L'entrée LiteLLM n'a pas pu être retirée : elle survit sans "
                            "ligne de catalogue. Vérifie la passerelle.")

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
        return _json_erreur("Le nombre de tokens par défaut doit être un nombre positif.")
    if not re.match(r'^\d+[smhd]$', duration):
        return _json_erreur("Durée invalide (ex: 1d, 7d, 30d, 12h).")
    ancien = get_setting('default_key_budget', None)
    set_setting('default_key_budget', budget_val)
    set_setting('default_key_duration', duration)
    # Le plafond GLOBAL n'etait pas audite, alors qu'il s'applique a tout compte
    # sans override ni groupe : c'est l'action la plus large de la page.
    log_audit(session.get('username'), 'settings.budget',
              f"plafond global {ancien} → {budget_val:.0f} tokens / {duration}")
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
    # Avatar choisi (NULL = avatar généré depuis le pseudo) : l'admin voyait des
    # initiales là où l'intéressé voit sa pp.
    avatars = {a['username']: a['avatar_id'] for a in
               db.execute("SELECT username, avatar_id FROM user_prefs").fetchall()}
    # Verrouillage anti-force-brute en cours : la clé est composite
    # ('ip|compte' ou 'user:compte'), on rattache au compte par le suffixe.
    locked = {}
    now = datetime.now().timestamp()
    for r in db.execute("SELECT key, locked_until FROM login_attempts WHERE locked_until > ?",
                        (now,)).fetchall():
        compte = r['key'].split('|')[-1].removeprefix('user:')
        locked[compte] = max(locked.get(compte, 0), int((r['locked_until'] - now) // 60) + 1)

    # La table des groupes est petite et etait DEJA lue apres la boucle
    # (pour la reponse) : on la charge ici et on la passe aux helpers, ce qui
    # supprime les 2 SELECT par compte de `_local_group`.
    groups = db.execute("SELECT name, max_budget, is_admin FROM user_groups "
                        "ORDER BY name").fetchall()
    groupes = {g['name']: g for g in groups}
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
        local_admin = _local_user_is_admin(mu, groupes) if mu else None
        # Rôle EFFECTIF, tel qu'il est réellement APPLIQUÉ aux requêtes. Une seule
        # autorité, et c'est le portail : dès qu'une ligne `local_users` existe,
        # `auth.etat_compte` tranche sur elle seule (`_local_user_is_admin`) et
        # réécrit `session['is_admin']` — quel que soit `last_source`. La page
        # Users affirmait l'inverse (« l'annuaire gagne ») : un compte local admin
        # retiré de `cn=adm_cronos` restait admin à l'usage, sans aucun signal
        # pour l'opérateur qui venait de faire le geste. On affiche donc ce qui
        # s'applique, et le commentaire dit la même chose que auth.py.
        if mu is not None:
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
        # Un compte d'annuaire reste « géré par l'annuaire » même si une ligne
        # locale historique subsiste ; sans ligne locale, l'annuaire est la
        # seule autorité possible.
        managed_by = 'repertoire' if (mu is None or last_source in ('sso', 'ldap')) else 'local'
        out.append({
            'username': name,
            'fullname': fullname,
            'avatar_id': avatars.get(name),
            'sources': sorted(srcs),
            'managed': bool(mu),
            'managed_by': managed_by,
            'id': mu['id'] if mu else None,
            'group_name': mu['group_name'] if mu else None,
            'enabled': mu['enabled'] if mu else 1,
            'is_admin': mu['is_admin'] if mu else None,
            'effective_admin': effective_admin,
            'role_source': role_source,
            'last_source': last_source,
            'effective_budget': (_local_user_effective_budget(mu, groupes) if mu
                                 else (sp['max_budget'] if sp else None)),
            'unlimited': (sp['unlimited'] if sp else False),
            'spend': (sp['spend'] if sp else 0),
            'key_count': (sp['key_count'] if sp else 0),
            'last_seen': recorded[name]['last_seen'] if name in recorded else None,
            # État de refus et verrouillage : sans eux, la page ne peut pas
            # distinguer un compte bloqué d'un compte simplement inactif — or
            # c'est exactement ce qu'un admin vient vérifier.
            'blocked': blocked.get(name) is not None,
            'block_reason': (blocked[name]['reason'] if name in blocked else None),
            'blocked_at': (blocked[name]['blocked_at'] if name in blocked else None),
            'locked_minutes': locked.get(name, 0),
        })
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
    # Un quota qui n'a pas pu être écrit côté LiteLLM, c'est un compte de fait
    # illimité : le dire, au lieu de laisser croire à une création complète.
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
        sets.append("group_name=?"); vals.append(group)
    if 'max_budget' in request.form:
        budget, err = _parse_budget(request.form.get('max_budget'))
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        sets.append("max_budget=?"); vals.append(budget)
    if 'is_admin' in request.form:
        nouveau_admin = int(request.form.get('is_admin') in ('1', 'true', 'on'))
        if not nouveau_admin:
            refus = _refus_retrait_admin(row, "rétrograder")
            if refus:
                return _json_erreur(refus, 409)
        sets.append("is_admin=?"); vals.append(nouveau_admin)
    if 'enabled' in request.form:
        nouvel_actif = int(request.form.get('enabled') in ('1', 'true', 'on'))
        if not nouvel_actif:
            refus = _refus_retrait_admin(row, "désactiver")
            if refus:
                return _json_erreur(refus, 409)
        sets.append("enabled=?"); vals.append(nouvel_actif)
    if 'fullname' in request.form:
        sets.append("fullname=?"); vals.append(request.form.get('fullname', '').strip()[:120] or None)
    if sets:
        db.execute(f"UPDATE local_users SET {', '.join(sets)} WHERE id=?", (*vals, uid))
        db.commit()
    updated = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    # Verrouiller un compte (enabled=0) révoque immédiatement ses sessions :
    # il perd son accès sans attendre l'expiration HTTP.
    revoquees = 0
    if 'enabled' in request.form and not updated['enabled']:
        revoquees = _revoke_user_sessions(updated['username'])
    if password:
        # Changer le mot de passe doit fermer les AUTRES sessions : sans ça,
        # un changement motivé par un doute sur un cookie volé ne protégeait
        # de rien, l'ancienne session continuant de valider.
        revoquees = max(revoquees, _revoke_user_sessions(updated['username']))
    # La resynchronisation du quota etait INCONDITIONNELLE : renommer un compte,
    # changer son mot de passe ou corriger son nom reecrivait l'enveloppe LiteLLM
    # a partir de local_users — ce qui EFFACAIT une subvention en cours, puisque
    # budget_grants ne vit que cote LiteLLM. Le reaper constatait ensuite la
    # derive, concluait « l'admin est intervenu entre-temps » et supprimait la
    # ligne : le compte perdait son quota sans que personne ne soit prevenu.
    recharge = request.form.get('enabled') in ('1', 'true', 'on')
    if {'group', 'max_budget'} & set(request.form.keys()) or recharge:
        quota_ok = _sync_local_user_budget(updated['username'], updated)
    else:
        quota_ok = True
    log_audit(session.get('username'), 'user.update',
              f"mise à jour de {updated['username']}"
              + (f" — mot de passe changé par l'admin, {revoquees} session(s) fermée(s)"
                 if password else '')
              + ('' if quota_ok else ' — QUOTA NON APPLIQUÉ'))
    if password:
        prevenir_mot_de_passe_change(updated['username'], par_admin=session.get('username'))
    return jsonify({'ok': True} if quota_ok else {
        'ok': True,
        'warning': "Modifications enregistrées, mais le quota n'a PAS pu être appliqué "
                   "sur LiteLLM : redéfinis le budget du compte."})


@bp.route('/admin/users/<username>/revoke-sessions', methods=['POST'])
@admin_required
def admin_revoke_sessions(username):
    """Révoque à volonté toutes les sessions actives d'un compte (même un
    cookie volé devient inutilisable immédiatement)."""
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    n = _revoke_user_sessions(username)
    # Une révocation est une action de sécurité : elle se journalise comme les
    # autres. Elle ne l'était pas, seule la désactivation l'était.
    log_audit(session.get('username'), 'session.revoke',
              f"{username} — {n} session(s) révoquée(s)")
    return jsonify({'ok': True, 'revoked': n})

def _corps():
    """Corps de requête, formulaire OU JSON.

    L'Admin envoie du formulaire (postForm) et la nouvelle interface JSON
    (fetch) : lire uniquement request.form faisait ignorer silencieusement un
    `confirm=DELETE` pourtant envoyé, et la route répondait 409 sans fin.
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form


def _dernier_admin_local(username):
    """Vrai si `username` est le dernier administrateur LOCAL actif.

    Le portail n'aurait alors plus personne pour l'administrer en local —
    les admins du répertoire (LDAP/SSO) dépendent d'un annuaire externe, ce
    qui n'est pas une raison pour se couper soi-même la main.
    """
    db = get_db()
    for r in db.execute("SELECT * FROM local_users WHERE enabled=1").fetchall():
        if r['username'] != username and _local_user_is_admin(r):
            return False
    return True


def _admins_locaux_apres(simulation):
    """Compte les admins locaux actifs APRÈS un retrait simulé.

    `simulation` = {username: (is_admin, group_name)} des comptes dont l'état
    change. Nécessaire pour le cas que `_dernier_admin_local` ne peut pas voir :
    un groupe dont DEUX membres tiennent leurs droits d'administration. Pris un
    par un, chacun voit l'autre comme admin et conclut qu'il peut partir ; les
    retirer tous les deux n'en laisse aucun.
    """
    db = get_db()
    n = 0
    for r in db.execute("SELECT * FROM local_users WHERE enabled=1").fetchall():
        etat = simulation.get(r['username'])
        if etat is None:
            if _local_user_is_admin(r):
                n += 1
            continue
        is_admin, group_name = etat
        if is_admin:
            n += 1
            continue
        g = _local_group(group_name) if group_name else None
        if g and g['is_admin']:
            n += 1
    return n


def _refus_retrait_admin(row, action):
    """Motif de refus si retirer les droits d'admin de `row` est interdit.

    Deux cas, même raison : le portail deviendrait inadministrable en local.
    - se désactiver ou se rétrograder SOI-MÊME échappe à toute réparation : plus
      aucune session d'administration ne subsiste ;
    - retirer le DERNIER admin local laisse la plateforme aux seuls admins du
      répertoire, qui dépendent d'un annuaire externe.

    La suppression et le blocage ont cette garde ; la MODIFICATION ne l'avait
    pas, alors qu'un seul POST y suffisait — et la revalidation de l'état du
    compte à chaque requête rend la perte d'accès immédiate (plus besoin
    d'attendre l'expiration du cookie).
    """
    if not _local_user_is_admin(row):
        return None
    if row['username'] == session.get('username'):
        return (f"Tu ne peux pas te {action} toi-même : tu perdrais immédiatement "
                f"l'accès à l'administration. Demande-le à un autre administrateur.")
    if _admins_locaux_apres({row['username']: (0, None)}) == 0:
        return (f"{row['username']} est le dernier administrateur local actif : le "
                f"portail n'aurait plus personne pour l'administrer en local.")
    return None


@bp.route('/admin/users/delete/<int:uid>', methods=['POST'])
@admin_required
def admin_users_delete(uid):
    """Supprime un compte : retire TOUT son accès, puis purge ses données.

    Avant le 2026-09-13 cette route ne faisait qu'un DELETE dans local_users :
    les clés API du compte restaient valides (LiteLLM les valide lui-même) et
    sa session navigateur survivait jusqu'à 12 h. Trois garde-fous sont
    ajoutés avec le reste :
    - pas d'auto-suppression (l'admin se couperait l'accès en pleine action) ;
    - pas de suppression du dernier administrateur local ;
    - `confirm=DELETE` exigé : l'opération emporte les données personnelles
      (mémoire, conversations, partages, préférences) et ne doit pas partir
      d'un POST accidentel.
    """
    db = get_db()
    row = db.execute("SELECT * FROM local_users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': "Utilisateur introuvable."}), 404
    username = row['username']
    moi = session.get('username')
    if username == moi:
        return jsonify({'ok': False,
                        'error': "Tu ne peux pas supprimer ton propre compte."}), 400
    if _local_user_is_admin(row) and _dernier_admin_local(username):
        return jsonify({'ok': False,
                        'error': "Dernier administrateur local : nomme un autre "
                                 "administrateur avant de supprimer celui-ci."}), 400
    donnees = compter_donnees(username)
    if (_corps().get('confirm') or '').strip().upper() != 'DELETE':
        return jsonify({'ok': False, 'needs_confirm': True, 'donnees': donnees,
                        'error': "Confirmation requise : cette suppression emporte "
                                 "les accès ET les données du compte."}), 409
    # La ligne locale d'abord : elle porte l'accès, et tout ce qui suit la
    # concerne (clés, enveloppe, données) sans dépendre d'elle.
    db.execute("DELETE FROM local_users WHERE id=?", (uid,))
    db.commit()
    rapport = deprovisionner_compte(username, moi)
    reponse = {'ok': True, 'purged': rapport}
    # L'accès API est la moitié du problème : si LiteLLM n'a pas répondu, des
    # clés peuvent encore fonctionner, et l'admin doit le savoir tout de suite
    # plutôt que de le découvrir dans le journal d'audit.
    if rapport['keys_failed'] or not rapport['litellm_user']:
        reponse['warning'] = (
            f"{rapport['keys_failed']} clé(s) n'ont pas pu être révoquées et/ou "
            "l'enveloppe LiteLLM n'a pas pu être supprimée (service injoignable) : "
            "vérifie les clés de ce compte dans LiteLLM avant de le considérer comme parti.")
    return jsonify(reponse)


@bp.route('/admin/users/<username>/purge', methods=['POST'])
@admin_required
def admin_user_purge(username):
    """Efface les DONNÉES d'un compte sans ligne locale (compte d'annuaire).

    Un compte LDAP/SSO n'existe pas dans `local_users` : la route de
    suppression, qui part d'un identifiant local, ne peut donc rien pour lui —
    et ses conversations, sa mémoire, ses préférences et ses clés restaient en
    base indéfiniment après son départ. Cette route les efface, sans toucher à
    son accès : le BLOCAGE reste le levier d'offboarding, car un compte
    d'annuaire peut se reconnecter et retrouverait alors un compte vide.
    """
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
    if (_corps().get('confirm') or '').strip().upper() != 'DELETE':
        return jsonify({'ok': False, 'needs_confirm': True,
                        'donnees': compter_donnees(username),
                        'error': "Confirmation requise : cette opération efface "
                                 "définitivement les données du compte."}), 409
    rapport = deprovisionner_compte(username, session.get('username'),
                                    action='user.purge')
    return jsonify({'ok': True, 'purged': rapport})


@bp.route('/admin/users/<username>/block', methods=['POST'])
@admin_required
def admin_user_block(username):
    """Refuse un compte à la connexion, quelle que soit sa source.

    C'est le SEUL levier du portail pour un compte LDAP/SSO : il n'a pas de
    ligne dans local_users, donc pas d'`enabled` à basculer, et révoquer ses
    sessions ne servait à rien — il se reconnectait aussitôt. Le blocage est
    réversible et ne touche pas à son état local : débloquer rend le compte
    exactement tel qu'il était.
    """
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    if username == session.get('username'):
        return jsonify({'ok': False, 'error': "Tu ne peux pas bloquer ton propre compte."}), 400
    if est_bloque(username):
        return jsonify({'ok': True, 'revoked_sessions': 0, 'deja_bloque': True})
    row = get_db().execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    if row is not None and _local_user_is_admin(row) and _dernier_admin_local(username):
        return jsonify({'ok': False,
                        'error': "Dernier administrateur local : nomme un autre "
                                 "administrateur avant de bloquer celui-ci."}), 400
    raison = (_corps().get('reason') or '').strip()[:200] or None
    n = bloquer_compte(username, raison, session.get('username'))
    return jsonify({'ok': True, 'revoked_sessions': n})


@bp.route('/admin/users/<username>/unblock', methods=['POST'])
@admin_required
def admin_user_unblock(username):
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    n = debloque_compte(username, session.get('username'))
    return jsonify({'ok': True, 'debloque': bool(n)})


@bp.route('/admin/users/<username>/detail')
@admin_required
def admin_user_detail(username):
    """Tout ce que le portail sait d'un compte, en une réponse.

    La page Users liste ; cette vue répond aux questions qu'on se pose
    vraiment devant un compte : que consomme-t-il, a-t-il des clés, combien de
    souvenirs, d'où s'est-il connecté, et qu'a-t-on fait de lui. Les valeurs
    des clés ne sortent JAMAIS d'ici : seul leur alias est renvoyé.
    """
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': "Nom d'utilisateur invalide."}), 400
    db = get_db()
    mu = db.execute("SELECT * FROM local_users WHERE username=?", (username,)).fetchone()
    rs = db.execute("SELECT * FROM user_sources WHERE username=?", (username,)).fetchone()
    bl = db.execute("SELECT * FROM blocked_users WHERE username=?", (username,)).fetchone()
    litellm = _litellm_user_info(username)
    donnees = compter_donnees(username)
    cles = []
    for k in (get_user_keys(username) or []):
        cles.append({'alias': k.get('key_alias'), 'created_at': k.get('created_at'),
                     'spend': k.get('spend', 0)})
    # La ligne du sid appelant est la sienne : si l'admin ouvre son propre
    # compte, elle se complète comme en self-service (session ouverte avant
    # l'ajout des colonnes IP/user-agent). Celle d'un AUTRE compte n'est jamais
    # touchée — on n'écrirait pas l'IP de l'admin dans la session d'autrui.
    completer_origine_session()
    sid_courant = session.get('sid')
    now = datetime.now().timestamp()
    sessions = []
    for r in db.execute(
            "SELECT sid, auth_at, created_at, expires_at, ip, user_agent FROM user_sessions "
            "WHERE username=? AND revoked=0 AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT 20", (username, now)).fetchall():
        sessions.append({
            # Jamais le sid complet : c'est le secret du cookie de session.
            'id': (r['sid'] or '')[:12],
            'created_at': r['created_at'], 'expires_at': r['expires_at'],
            'ip': r['ip'], 'user_agent': r['user_agent'],
            'current': bool(sid_courant and r['sid'] == sid_courant),
        })
    # Actions EFFECTUÉES par ce compte (audit_log.username est l'acteur, la
    # cible est dans `detail`) : c'est la lecture utile pour un compte admin.
    audit = [{'action': r['action'], 'detail': r['detail'], 'at': r['created_at']}
             for r in db.execute(
                 "SELECT action, detail, created_at FROM audit_log WHERE username=? "
                 "ORDER BY id DESC LIMIT 20", (username,)).fetchall()]
    last_source = rs['last_source'] if rs else None
    if mu is not None:
        # Même autorité unique que la liste (et que `auth.etat_compte`) : la
        # ligne locale tranche, l'annuaire ne s'y ajoute pas.
        role = 'admin' if _local_user_is_admin(mu) else 'user'
    else:
        role = 'admin' if (rs and rs['last_is_admin']) else 'user'
    return jsonify({
        'ok': True,
        'username': username,
        'fullname': (mu['fullname'] if mu else None) or (rs['fullname'] if rs else None),
        'sources': (rs['sources'] if rs else '') or '',
        'last_source': last_source,
        'last_seen': rs['last_seen'] if rs else None,
        'local': mu is not None,
        'enabled': bool(mu['enabled']) if mu is not None else True,
        'group': mu['group_name'] if mu else None,
        'max_budget': mu['max_budget'] if mu else None,
        'effective_budget': _local_user_effective_budget(mu) if mu else litellm['max_budget'],
        'role': role,
        'blocked': {'blocked': bl is not None,
                    'reason': bl['reason'] if bl else None,
                    'at': bl['blocked_at'] if bl else None,
                    'by': bl['blocked_by'] if bl else None},
        'litellm': {'exists': bool(litellm.get('exists')),
                    'max_budget': litellm.get('max_budget'),
                    'spend': litellm.get('spend', 0),
                    'budget_duration': litellm.get('budget_reset_at') or None},
        'keys': cles,
        'memory_facts': donnees['memory_facts'],
        'conversations': donnees['conversations'],
        'sessions': sessions,
        'audit': audit,
        'donnees': donnees,
    })

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
    # Cas symetrique de la suppression : modifier un groupe existant pour lui
    # RETIRER le droit d'admin (upsert ON CONFLICT) fait perdre leurs droits a
    # ses membres — sans passer par la suppression. Meme garde.
    ancien = _local_group(name)
    if ancien and ancien['is_admin'] and not is_admin:
        membres = db.execute("SELECT * FROM local_users WHERE group_name=?", (name,)).fetchall()
        if any(_local_user_is_admin(m) for m in membres) and \
                _admins_locaux_apres({m['username']: (0, None) for m in membres}) == 0:
            return _json_erreur(
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
    log_audit(session.get('username'), 'group.create', f"groupe {name}")
    return jsonify({'ok': True})

@bp.route('/admin/groups/delete/<name>', methods=['POST'])
@admin_required
def admin_groups_delete(name):
    db = get_db()
    membres = db.execute("SELECT * FROM local_users WHERE group_name=?", (name,)).fetchall()
    # Un groupe peut PORTER les droits d'administration de ses membres
    # (user_groups.is_admin) : le supprimer peut donc laisser le portail sans
    # aucun administrateur local, exactement comme retrograder le dernier. La
    # suppression du groupe ne verifiait rien.
    groupe = _local_group(name)
    if groupe and groupe['is_admin'] and any(_local_user_is_admin(m) for m in membres):
        if _admins_locaux_apres({m['username']: (0, None) for m in membres}) == 0:
            return _json_erreur(
                f"Le groupe {name} porte les droits d'administration de ses membres et "
                f"aucun administrateur local ne subsisterait après sa suppression. "
                f"Ajoute un autre administrateur local d'abord.", 409)
    db.execute("UPDATE local_users SET group_name=NULL WHERE group_name=?", (name,))
    db.execute("DELETE FROM user_groups WHERE name=?", (name,))
    db.commit()
    # Les membres perdent le quota du groupe : leur NOUVEAU plafond effectif
    # (override personnel, sinon défaut global) doit être réécrit côté LiteLLM.
    # La création de groupe le faisait, la suppression non — les membres
    # gardaient donc en silence une enveloppe parfois plus généreuse que le
    # défaut, c'est-à-dire un quota fantôme.
    echecs = []
    for m in membres:
        if m['max_budget'] is not None:
            continue                     # plafond personnel : rien à propager
        frais = db.execute("SELECT * FROM local_users WHERE username=?",
                           (m['username'],)).fetchone()
        if frais and not _sync_local_user_budget(m['username'], frais):
            echecs.append(m['username'])
    log_audit(session.get('username'), 'group.delete',
              f"suppression du groupe {name} — {len(membres)} membre(s) rebasculé(s) "
              f"sur le défaut" + (f", QUOTA NON APPLIQUÉ pour {', '.join(echecs)}" if echecs else ""))
    return jsonify({'ok': True, 'membres': len(membres), 'echecs_quota': echecs})

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
    # L'audit manquait sur la bascule : c'est pourtant l'action qui coupe l'acces
    # a tout le monde, et « qui a active la maintenance a 3 h du matin » etait
    # jusqu'ici sans reponse en base.
    log_audit(session.get('username'), 'maintenance',
              'activé' if now_on else 'désactivé')
    # Le flash « email non envoyé » n'etait rendu par personne : l'avertissement
    # SMTP documente comme visible par l'operateur ne l'etait pas. Il part
    # desormais dans la reponse JSON, donc dans le bandeau de l'interface.
    avert = None
    if all([SMTP_HOST, SMTP_USER, SMTP_PASS, ADMIN_EMAIL]) and not sent:
        avert = "L'email d'alerte n'a pas pu être envoyé — vérifie la config SMTP."
    return _json_ok("Mode maintenance activé." if now_on else "Mode maintenance désactivé.",
                    maintenance_mode=now_on, warning=avert)

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
        return _json_erreur("Demande introuvable ou déjà traitée.", 404)
    try:
        amount_val = float(amount)
        if amount_val <= 0:
            raise ValueError
    except ValueError:
        return _json_erreur("Le montant à ajouter doit être un nombre positif.")
    if amount_val > 1_000_000_000_000:
        # Garde-fou : un montant aberrant (typo « 6666726666666 ») rend le
        # compte illimité de facto — c'est arrivé sur ce serveur sans que
        # personne ne le voie. Au-delà d'1e12 tokens, c'est une erreur de
        # saisie, on refuse.
        return _json_erreur("Montant irréaliste (> 1e12 tokens) — vérifie la saisie.")
    # Budget at the ACCOUNT level: we increment the LiteLLM user's envelope.
    info = _litellm_user_info(breq['username'])
    current_budget = info.get('max_budget') or 0
    # La fenêtre de reset accompagne toujours la mise à jour : sans elle le
    # montant devient un plafond à vie, jamais remis à zéro. On aligne sur le
    # défaut global (hebdomadaire depuis le 2026-09-08).
    grant_duration = get_setting('default_key_duration', KEY_DURATION)
    # « durée » : vide = permanent (l'ancien comportement) ; N jours = le
    # supplément N'EST PAS une nouvelle limite à vie — un reaper ramène le
    # compte à son plafond de base à l'échéance.
    grant = db.execute(
        "SELECT * FROM budget_grants WHERE username=? AND expires_at > ?",
        (breq['username'], datetime.utcnow().isoformat())).fetchone()
    duree = request.form.get('grant_days', '').strip()
    expires_at = None
    if duree:
        try:
            jours = int(duree)
            if not 1 <= jours <= 365:
                raise ValueError
        except ValueError:
            return _json_erreur("Durée de la subvention : nombre de jours entre 1 et 365 "
                                "(vide = permanent).")
        expires_at = (datetime.utcnow() + timedelta(days=jours)).isoformat()
    if expires_at:
        # Subvention TEMPORAIRE : le plafond de base est figé UNE fois (au
        # premier boost) ; un 2e boost pendant la période s'empile dessus,
        # l'échéance revient quand même au MÊME plafond de base.
        base = grant['base_budget'] if grant else current_budget
        new_budget = (grant['current_budget'] if grant else base) + amount_val
        if not litellm_update_user_budget(breq['username'], new_budget,
                                          budget_duration=grant_duration):
            return _json_erreur("Erreur lors de la mise à jour du budget sur LiteLLM.", 502)
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
            return _json_erreur("Erreur lors de la mise à jour du budget sur LiteLLM.", 502)
        if grant:
            # Hausse PERMANENTE pendant une subvention : elle doit survivre à
            # l'échéance, donc elle relève le plafond de base lui-même.
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
    # Accorder un budget n'etait PAS audite : c'est pourtant l'action qui a le
    # plus riche passe d'incidents sur cette machine (la typo a 6,7e12 tokens),
    # et « qui a augmente le quota de qui » doit avoir une reponse en base.
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
    """Redéfinir le plafond d'un compte (montant EXACT, pas un ajout).

    Permet de BAISSER un quota (200M → 50M) comme de le hausser. Toute
    subvention temporaire en cours est écrasée : la décision admin fait foi.

    0 est REFUSÉ. Mesuré sur cette instance : `user/update` avec `max_budget: 0`
    laisse bien 0 en base (LiteLLM ne l'interprète pas comme « illimité »), donc
    le compte est réellement plafonné à zéro token — un « je mets 0 pour lever
    la limite » coupe l'accès. Le blocage fait le même effet, en réversible et
    avec un motif consigné.
    """
    db = get_db()
    raw = request.form.get('budget', '').strip()
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        return _json_erreur(
            "Budget : nombre strictement positif attendu (tokens). Pour couper "
            "l'accès d'un compte, utilise le blocage du compte — réversible et "
            "tracé ; un budget de 0 le plafonne réellement à zéro token.")
    if value > 1_000_000_000_000:
        # Même garde-fou que l'approbation : au-delà d'1e12 tokens c'est une
        # typo qui rend le compte illimité de facto.
        return _json_erreur("Montant irréaliste (> 1e12 tokens) — vérifie la saisie.")
    grant_duration = get_setting('default_key_duration', KEY_DURATION)
    if not litellm_update_user_budget(username, value, budget_duration=grant_duration):
        return _json_erreur("Erreur lors de la mise à jour du budget sur LiteLLM.", 502)
    db.execute("DELETE FROM budget_grants WHERE username=?", (username,))
    db.commit()
    log_audit(session.get('username', '?'), 'budget_set', f'{username}={value:.0f}')
    _fmt = lambda n: f"{n:,.0f}".replace(',', ' ')  # noqa: E731
    return _json_ok(f"Budget de {username} redéfini à {_fmt(value)} tokens / {grant_duration}.")


def revert_expired_grants():
    """Ramène à leur plafond de base les comptes dont la subvention a expiré.

    Retourne le nombre de comptes ramenés. Appelé par le reaper (thread du
    portail, toutes les 60 s) et une fois au démarrage : après une coupure,
    l'échéance manquée est rattrapée à la minute du boot.
    """
    db = get_db()
    now = datetime.utcnow().isoformat()
    rows = db.execute("SELECT * FROM budget_grants WHERE expires_at <= ?", (now,)).fetchall()
    if not rows:
        return 0
    grant_duration = get_setting('default_key_duration', KEY_DURATION)
    ramenes = 0
    for g in rows:
        info = _litellm_user_info(g['username'])
        actuel = info.get('max_budget')
        if actuel is None or abs(actuel - g['current_budget']) < 1:
            # Personne n'a touché au plafond depuis le boost : on revient à la base.
            if not litellm_update_user_budget(g['username'], g['base_budget'],
                                              budget_duration=grant_duration):
                # La ligne NE DOIT PAS disparaître : elle porte l'échéance ET le
                # plafond de base, donc la seule raison de retenter au prochain
                # passage. La supprimer malgré l'échec transformait une
                # subvention TEMPORAIRE en hausse permanente, en silence — une
                # minute de LiteLLM injoignable suffisait, et personne ne
                # pouvait plus savoir qu'un retour était dû.
                log_audit('reaper', 'budget.grant_revert_echec',
                          f"{g['username']} : retour au plafond de base non appliqué "
                          f"(LiteLLM injoignable ?), nouvelle tentative au prochain passage")
                continue
            ramenes += 1
        # Sinon l'admin est intervenu entre-temps : la subvention est simplement
        # oubliée, on n'écrase PAS sa décision.
        db.execute("DELETE FROM budget_grants WHERE id=?", (g['id'],))
    db.commit()
    return ramenes

@bp.route('/admin/budget/reject/<int:req_id>', methods=['POST'])
@admin_required
def reject_budget(req_id):
    db = get_db()
    breq = db.execute("SELECT username FROM budget_requests WHERE id=?", (req_id,)).fetchone()
    if not breq:
        return _json_erreur("Demande introuvable.", 404)
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
        return _json_erreur("Statut invalide.")
    db = get_db()
    req = db.execute("SELECT username, model_id FROM model_requests WHERE id=?", (req_id,)).fetchone()
    if not req:
        return _json_erreur("Demande introuvable.", 404)

    nom, deja, routage_ok = None, False, True
    if status == 'done' and req['model_id']:
        # On ENREGISTRE d'abord, on annonce ensuite. La demande etait marquee
        # « Lancé ✓ » et l'email « ton modèle est disponible » partait AVANT de
        # savoir si l'enregistrement LiteLLM avait abouti ; l'echec ne sortait que
        # dans un flash que l'interface ne rendait pas. Le demandeur apprenait
        # donc qu'il pouvait utiliser un modele qui n'etait pas route.
        nom, deja = _add_model_to_catalog(db, req['model_id'])
        cfg = db.execute("SELECT vllm_args, engine FROM model_configs WHERE name=?", (nom,)).fetchone()
        routage_ok = _register_litellm_model(
            nom, cfg['vllm_args'] if cfg else DEFAULT_VLLM_ARGS,
            (cfg['engine'] if cfg else 'vllm') or 'vllm')
        if not routage_ok:
            db.commit()          # le catalogue, lui, est bien a jour
            log_audit(session.get('username'), 'request.routage_echec',
                      f"demande #{req_id} ({nom}) : catalogue OK, LiteLLM non enregistré")
            return _json_erreur(
                f"Le modèle « {nom} » est dans le catalogue, mais son enregistrement "
                f"LiteLLM a échoué : la demande reste EN ATTENTE et le demandeur n'est "
                f"pas prévenu (il ne pourrait pas l'utiliser). Réessaie.", 502)

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
              f"demande #{req_id} → {status}" + (f" ({nom})" if nom else ""))

    if status != 'done' or not req['model_id']:
        return _json_ok("Demande mise à jour.")

    # Notifies the requester by email that their model is available.
    email = ldap_lookup_email(req['username'])
    if email:
        send_user_email(email, "[Cronos] Ton modèle est disponible",
                        f"Bonne nouvelle — le modèle que tu as demandé est validé et "
                        f"disponible sur la plateforme Cronos :\n\n  {req['model_id']}\n\n"
                        f"Tu peux l'utiliser via l'API / le Playground une fois lancé.\n"
                        f"https://dgx.cronos.website/\n")
    if deja:
        return _json_ok(f"Modèle déjà dans le catalogue sous « {nom} ».")
    add_announcement('model_add', nom)
    return _json_ok(f"Modèle « {nom} » ajouté au catalogue et routé par LiteLLM — "
                    f"vérifie ses args vLLM puis lance-le.")


@bp.route('/admin/support/feedback')
@admin_required
def admin_support_feedback():
    """Retours utilisateurs sur le Support (pouce haut/bas + commentaires).

    Sert à savoir QUELLES réponses échouent : sans cela, le prompt du Support ne
    se corrigeait qu'à l'intuition. Agrégat + derniers retours négatifs (les plus
    utiles pour corriger), borné.
    """
    db = get_db()
    tot = db.execute("SELECT COUNT(*) c, SUM(CASE WHEN vote>0 THEN 1 ELSE 0 END) p "
                     "FROM support_feedback").fetchone()
    par_user = db.execute(
        "SELECT username, COUNT(*) c, SUM(CASE WHEN vote>0 THEN 1 ELSE 0 END) p "
        "FROM support_feedback GROUP BY username ORDER BY c DESC LIMIT 20").fetchall()
    recents = db.execute(
        "SELECT username, vote, comment, question, answer, model, created_at "
        "FROM support_feedback ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify({
        'total': tot['c'] or 0,
        'positifs': tot['p'] or 0,
        'par_utilisateur': [dict(r) for r in par_user],
        'recents': [dict(r) for r in recents],
    })
