"""Admin — storage: where generated files live (volume or S3), their
migration, and the backups' destination and retention.

« rajouter le s3 dans les paramètres … que l'on puisse choisir que les backups
aillent dessus, la rétention des backups sur l'interface, et que l'archivage
… soit en local soit sur le s3 » (operator, 2026-10-14). The S3 connection
itself is a card of Admin → Config (instance_config, object_store); this is
what uses it.

Routes of the `admin` blueprint (defined in admin_routes.py).
"""
from flask import jsonify, request, session

import backups
import media_store
import object_store
from admin_routes import _json_error, bp
from auth import admin_required
from db import log_audit, set_setting


@bp.route('/admin/storage', methods=['GET'])
@admin_required
def admin_storage():
    return jsonify({
        'ok': True,
        's3_ready': object_store.configured(),
        'media_storage': media_store.location() if object_store.configured() else 'local',
        'media_usage': media_store.usage(),
        'migration': media_store.migration_status(),
        'backup': backups.settings(),
        'last_backup': backups.last_state(),
        'max_keep': backups.MAX_KEEP,
    })


@bp.route('/admin/storage', methods=['POST'])
@admin_required
def admin_storage_save():
    """Saves the fields present in the form. S3 cannot be chosen before it is
    configured: files written « to S3 » would land on the volume anyway, and
    the panel would claim otherwise."""
    f = request.form
    changes = []
    if 'media_storage' in f:
        v = f['media_storage']
        if v not in ('local', 's3'):
            return _json_error("Emplacement inconnu.")
        if v == 's3' and not object_store.configured():
            return _json_error("Configure d'abord le stockage S3 (Admin → Config).")
        set_setting('media_storage', v)
        changes.append(f'fichiers={v}')
    if 'backup_destination' in f:
        v = f['backup_destination']
        if v not in backups.DESTINATIONS:
            return _json_error("Destination inconnue.")
        if v != 'local' and not object_store.configured():
            return _json_error("Configure d'abord le stockage S3 (Admin → Config).")
        set_setting('backup_destination', v)
        changes.append(f'sauvegardes={v}')
    for name in ('backup_keep_local', 'backup_keep_s3'):
        if name in f:
            try:
                n = int(f[name])
            except ValueError:
                return _json_error("La rétention doit être un nombre de sauvegardes.")
            if not 1 <= n <= backups.MAX_KEEP:
                return _json_error(f"La rétention va de 1 à {backups.MAX_KEEP} sauvegardes.")
            set_setting(name, n)
            changes.append(f'{name}={n}')
    log_audit(session['username'], 'stockage.modifie', ', '.join(changes) or '(rien)')
    return jsonify({'ok': True})


@bp.route('/admin/storage/migrate', methods=['POST'])
@admin_required
def admin_storage_migrate():
    """Moves every existing file to `target`, in the background. Files stay
    readable during the move (looked up in both places)."""
    target = request.form.get('target', '')
    started, error = media_store.start_migration(target)
    if not started:
        return _json_error(error, 409 if 'déjà' in error else 400)
    log_audit(session['username'], 'stockage.migration', f'vers {target}')
    return jsonify({'ok': True, 'migration': media_store.migration_status()})
