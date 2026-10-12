"""Skill packages: list, browse, download, import (.zip / Git), sync.

The model-facing side (how a skill is offered and loaded) lives in
support.py and chat_routes.py; skills_store.py holds the parsing and the
archive handling. Every answer is JSON with an honest status (see the
« Admin — action contract » in CLAUDE.md): a refused import says why, a
partial one lists what was skipped.
"""
import logging

from flask import Blueprint, Response, jsonify, request, session

import skills_store as store
from auth import login_required
from db import get_db

bp = Blueprint('skills', __name__)
_log = logging.getLogger('app')

# Same cap as manual skills (settings_routes.MAX_SKILLS).
MAX_SKILLS = 50


def _row(db, skill_id, username):
    return db.execute("SELECT * FROM skills WHERE id=? AND username=?",
                      (skill_id, username)).fetchone()


def _summary(db, r):
    files = db.execute("SELECT COUNT(*) n FROM skill_files WHERE skill_id=?",
                       (r['id'],)).fetchone()['n']
    return {'id': r['id'], 'name': r['name'], 'description': r['description'],
            'source': r['source'] or 'manual', 'source_url': r['source_url'],
            'source_ref': r['source_ref'], 'source_path': r['source_path'],
            'version': r['version'] or '', 'synced_at': r['synced_at'],
            'created_at': r['created_at'], 'enabled': bool(r['enabled']),
            'file_count': files}


@bp.route('/api/skills')
@login_required
def api_skills():
    db = get_db()
    rows = db.execute("SELECT * FROM skills WHERE username=? ORDER BY name",
                      (session['username'],)).fetchall()
    return jsonify({'ok': True, 'skills': [_summary(db, r) for r in rows],
                    'max': MAX_SKILLS})


@bp.route('/api/skills/<int:skill_id>')
@login_required
def api_skill_detail(skill_id):
    db = get_db()
    r = _row(db, skill_id, session['username'])
    if not r:
        return jsonify({'ok': False, 'error': "Compétence introuvable."}), 404
    files = store.skill_files(db, skill_id)
    if not files:
        # A skill written before packages existed: it still has a SKILL.md.
        files = {'SKILL.md': store.manual_skill_md(r['name'], r['description'],
                                                   r['instructions'])}
    return jsonify({'ok': True, 'skill': {**_summary(db, r), 'instructions': r['instructions'],
                                          'files': [{'path': p, 'size': len(c.encode('utf-8')),
                                                     'content': c}
                                                    for p, c in sorted(files.items())]}})


@bp.route('/api/skills/<int:skill_id>/download')
@login_required
def api_skill_download(skill_id):
    db = get_db()
    r = _row(db, skill_id, session['username'])
    if not r:
        return jsonify({'ok': False, 'error': "Compétence introuvable."}), 404
    files = store.skill_files(db, skill_id) or {
        'SKILL.md': store.manual_skill_md(r['name'], r['description'], r['instructions'])}
    data = store.build_zip(r['name'], files)
    return Response(data, mimetype='application/zip', headers={
        'Content-Disposition': f'attachment; filename="{r["name"]}.zip"'})


def _import(db, username, skills, skipped, replace, source, url=None, ref=None, path=None):
    """Stores imported skills; returns the JSON answer."""
    count = db.execute("SELECT COUNT(*) n FROM skills WHERE username=?",
                       (username,)).fetchone()['n']
    imported = []
    skipped = [{'name': n, 'reason': why} for n, why in skipped]
    for skill in skills:
        exists = db.execute("SELECT 1 FROM skills WHERE username=? AND name=?",
                            (username, skill['name'])).fetchone()
        if not exists and count >= MAX_SKILLS:
            skipped.append({'name': skill['name'],
                            'reason': f"maximum {MAX_SKILLS} compétences atteint"})
            continue
        skill_id, why = store.save_skill(db, username, skill, source, url, ref, path, replace)
        if skill_id is None:
            skipped.append({'name': skill['name'], 'reason': why})
            continue
        if not exists:
            count += 1
        imported.append({'id': skill_id, 'name': skill['name']})
    db.commit()
    if not imported:
        return jsonify({'ok': False, 'error': "Aucune compétence importée.",
                        'skipped': skipped}), 400
    return jsonify({'ok': True, 'imported': imported, 'skipped': skipped})


@bp.route('/api/skills/import', methods=['POST'])
@login_required
def api_skill_import_zip():
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': "Fichier .zip requis."}), 400
    data = f.read(store.MAX_ARCHIVE_BYTES + 1)
    try:
        skills, skipped = store.skills_from_zip(data)
    except store.SkillImportError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return _import(get_db(), session['username'], skills, skipped,
                   request.form.get('replace') == '1', 'zip')


@bp.route('/api/skills/import-git', methods=['POST'])
@login_required
def api_skill_import_git():
    data = request.get_json(silent=True) or {}
    url = str(data.get('url') or '').strip()[:500]
    if not url:
        return jsonify({'ok': False, 'error': "Adresse du dépôt requise."}), 400
    try:
        skills, skipped, src = store.fetch_git_skills(
            url, str(data.get('ref') or '').strip()[:100], str(data.get('path') or '').strip()[:300])
    except store.SkillImportError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return _import(get_db(), session['username'], skills, skipped,
                   bool(data.get('replace')), 'git', src['url'], src['ref'], src['path'])


@bp.route('/api/skills/<int:skill_id>/sync', methods=['POST'])
@login_required
def api_skill_sync(skill_id):
    """Re-imports a Git skill from its source (same name, replaced)."""
    db = get_db()
    username = session['username']
    r = _row(db, skill_id, username)
    if not r:
        return jsonify({'ok': False, 'error': "Compétence introuvable."}), 404
    if r['source'] != 'git' or not r['source_url']:
        return jsonify({'ok': False, 'error': "Cette compétence n'a pas de dépôt source."}), 400
    try:
        skills, _skipped, src = store.fetch_git_skills(r['source_url'], r['source_ref'] or '',
                                                       r['source_path'] or '')
    except store.SkillImportError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    match = next((s for s in skills if s['name'] == r['name']), None)
    if match is None:
        return jsonify({'ok': False, 'error': "La compétence n'existe plus dans le dépôt."}), 404
    store.save_skill(db, username, match, 'git', src['url'], src['ref'], src['path'], replace=True)
    db.commit()
    return jsonify({'ok': True, 'skill': _summary(db, _row(db, skill_id, username))})


@bp.route('/api/skills/<int:skill_id>/enabled', methods=['POST'])
@login_required
def api_skill_enabled(skill_id):
    db = get_db()
    enabled = 1 if (request.get_json(silent=True) or {}).get('enabled') else 0
    cur = db.execute("UPDATE skills SET enabled=? WHERE id=? AND username=?",
                     (enabled, skill_id, session['username']))
    db.commit()
    if not cur.rowcount:
        return jsonify({'ok': False, 'error': "Compétence introuvable."}), 404
    return jsonify({'ok': True, 'enabled': bool(enabled)})
