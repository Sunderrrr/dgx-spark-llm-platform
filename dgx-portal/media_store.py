"""Generated files — images, videos, OCR scans, voice audio — local or on S3.

« que l'on puisse choisir que l'archivage … soit en local soit sur le s3 »
(operator, 2026-10-14). Admin → Stockage picks where NEW files are written
(`media_storage` = local | s3). Reading never depends on that choice: a file
is looked for on the local volume first, then in the bucket, so a half-moved
set — during a migration, or after a switch without one — keeps working.
The conversations themselves stay in the portal database (they travel with
the backups); only these files move.

Layout: `/app/data/<kind>/<name>` locally, `<prefix>/media/<kind>/<name>` in
the bucket. `<kind>` is one of KINDS; `<name>` is a bare file name chosen by
the server (never a client path).

Also here, run nightly by scripts/backup.sh: the ORPHAN SWEEP. Deleting an
account or trimming a history removes database rows; a file left without its
row is removed after a grace period. It used to live in monitoring/backup.py,
which no unit ran any more — files had been piling up since 2026-10-08.

CLI (host side, through `docker exec dgx-portal python3 -m media_store …`):
  sweep [--dry-run]     remove orphan files (local and S3)
"""
import fcntl
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time

import object_store
from db import DB_PATH, read_setting

ROOT = '/app/data'
KINDS = ('image_files', 'video_files', 'ocr_images', 'voice_audio')
STATUS_FILE = os.path.join(ROOT, '.media_migration.json')
LOCK_FILE = os.path.join(ROOT, '.media_migration.lock')
ORPHAN_GRACE_DAYS = 7

_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$')


def location():
    """Where NEW files go: 's3' only when chosen AND configured."""
    if read_setting('media_storage', 'local') == 's3' and object_store.configured():
        return 's3'
    return 'local'


def _check(kind, name):
    if kind not in KINDS or not name or not _NAME.match(name) or '..' in name:
        raise ValueError(f"invalid media reference {kind}/{name!r}")


def local_path(kind, name):
    _check(kind, name)
    return os.path.join(ROOT, kind, name)


def _key(kind, name):
    return object_store.key('media', kind, name)


def _log(msg):
    print(f"[media_store] {msg}", file=sys.stderr, flush=True)


def save(kind, name, data, content_type='application/octet-stream'):
    """Writes a new file where Admin → Stockage says. Returns 'local' | 's3'.

    An S3 failure does not lose the file: it is written locally instead (and
    served from there), and the next migration moves it."""
    path = local_path(kind, name)
    if location() == 's3':
        try:
            object_store.put_bytes(_key(kind, name), data, content_type)
            return 's3'
        except Exception as exc:                                 # noqa: BLE001
            _log(f"S3 write failed for {kind}/{name}, kept locally: {exc}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return 'local'


def offload(kind, name, content_type='application/octet-stream'):
    """A file that had to be written locally first (the video cache downloads
    to disk): moved to S3 when that is the location. Never raises."""
    path = local_path(kind, name)
    if location() != 's3' or not os.path.isfile(path):
        return
    try:
        _upload_verified(path, kind, name, content_type)
        os.remove(path)
    except Exception as exc:                                     # noqa: BLE001
        _log(f"S3 offload failed for {kind}/{name}, kept locally: {exc}")


def exists(kind, name):
    if os.path.isfile(local_path(kind, name)):
        return True
    if not object_store.configured():
        return False
    try:
        return object_store.head(_key(kind, name)) is not None
    except Exception:                                            # noqa: BLE001
        return False


def delete(kind, name):
    """Removes the file wherever it is. Never raises: a leftover is swept later."""
    try:
        os.remove(local_path(kind, name))
    except OSError:
        pass
    if object_store.configured():
        try:
            object_store.delete(_key(kind, name))
        except Exception as exc:                                 # noqa: BLE001
            _log(f"S3 delete failed for {kind}/{name}: {exc}")


def serve(kind, name, mimetype):
    """The Flask response for a stored file, or None when it exists nowhere.

    From S3 the browser's Range header is passed through: a <video> seeks with
    byte ranges, and answering the whole file to each seek would download it
    again every time."""
    from flask import Response, request, send_file
    path = local_path(kind, name)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return send_file(path, mimetype=mimetype, conditional=True)
    if not object_store.configured():
        return None
    rng = request.headers.get('Range')
    if rng and not re.fullmatch(r'bytes=\d*-\d*', rng):
        rng = None
    try:
        obj = object_store.get(_key(kind, name), rng)
    except Exception as exc:                                     # noqa: BLE001
        _log(f"S3 read failed for {kind}/{name}: {exc}")
        return None
    if obj is None:
        return None
    body = obj['Body']
    headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(obj['ContentLength']),
               'Cache-Control': 'private, max-age=3600'}
    if obj.get('ContentRange'):
        headers['Content-Range'] = obj['ContentRange']
    return Response(body.iter_chunks(64 * 1024), status=206 if obj.get('ContentRange') else 200,
                    mimetype=mimetype, headers=headers, direct_passthrough=True)


def _upload_verified(path, kind, name, content_type='application/octet-stream'):
    """Uploads a local file and checks the bucket holds the same size: a
    migration deletes the local copy only after that check."""
    k = _key(kind, name)
    object_store.client().upload_file(path, object_store.bucket(), k,
                                      ExtraArgs={'ContentType': content_type})
    meta = object_store.head(k)
    if not meta or meta['ContentLength'] != os.path.getsize(path):
        raise IOError(f"size mismatch after upload of {kind}/{name}")


# ── Usage, for Admin → Stockage ─────────────────────────────────────────────

def usage():
    """Files and bytes per kind, locally and in the bucket (None: unreachable)."""
    out = {'local': {}, 's3': None}
    for kind in KINDS:
        d = os.path.join(ROOT, kind)
        n = size = 0
        if os.path.isdir(d):
            for f in os.scandir(d):
                if f.is_file():
                    n += 1
                    size += f.stat().st_size
        out['local'][kind] = {'files': n, 'bytes': size}
    if object_store.configured():
        try:
            s3 = {k: {'files': 0, 'bytes': 0} for k in KINDS}
            for k, sz, _m in object_store.list_keys(object_store.key('media')):
                kind = k.rsplit('/', 2)[-2]
                if kind in s3:
                    s3[kind]['files'] += 1
                    s3[kind]['bytes'] += sz
            out['s3'] = s3
        except Exception as exc:                                 # noqa: BLE001
            _log(f"S3 listing failed: {exc}")
    return out


# ── Migration (Admin → Stockage « Migrer ») ─────────────────────────────────
# One at a time across the 4 gunicorn workers: a file lock, held by the thread
# that runs it. The progress is a JSON file every worker can read.

def migration_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {'running': False}


def _write_status(st):
    tmp = STATUS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f)
    os.replace(tmp, STATUS_FILE)


def start_migration(target):
    """Starts moving every existing file to `target` ('local' | 's3').
    Returns (started, error message)."""
    if target not in ('local', 's3'):
        return False, "Destination inconnue."
    if not object_store.configured():
        return False, "Configure d'abord le stockage S3 (Admin → Config)."
    os.makedirs(ROOT, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False, "Une migration est déjà en cours."
    st = {'running': True, 'target': target, 'total': 0, 'done': 0, 'failed': 0,
          'bytes': 0, 'started': time.time(), 'finished': None, 'last_error': ''}
    _write_status(st)
    # Started by an admin action and bounded by the file count: a request-scoped
    # thread, unnamed like the others (test_regression_guards).
    threading.Thread(target=_migrate, args=(target, st, fd), daemon=True).start()
    return True, ''


def _migrate(target, st, fd):
    try:
        if target == 's3':
            todo = [(kind, f.name, f.path) for kind in KINDS
                    if os.path.isdir(os.path.join(ROOT, kind))
                    for f in os.scandir(os.path.join(ROOT, kind)) if f.is_file()]
            st['total'] = len(todo)
            for kind, name, path in todo:
                try:
                    size = os.path.getsize(path)
                    _upload_verified(path, kind, name)
                    os.remove(path)
                    st['done'] += 1
                    st['bytes'] += size
                except Exception as exc:                         # noqa: BLE001
                    st['failed'] += 1
                    st['last_error'] = f"{kind}/{name}: {exc}"[:300]
                _write_status(st)
        else:
            todo = [(k, sz) for k, sz, _m in object_store.list_keys(object_store.key('media'))]
            st['total'] = len(todo)
            for k, sz in todo:
                kind, name = k.rsplit('/', 2)[-2:]
                try:
                    path = local_path(kind, name)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    fh, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.mig-')
                    with os.fdopen(fh, 'wb') as out:
                        object_store.client().download_fileobj(object_store.bucket(), k, out)
                    if os.path.getsize(tmp) != sz:
                        os.remove(tmp)
                        raise IOError("size mismatch after download")
                    os.replace(tmp, path)
                    object_store.delete(k)
                    st['done'] += 1
                    st['bytes'] += sz
                except Exception as exc:                         # noqa: BLE001
                    st['failed'] += 1
                    st['last_error'] = f"{kind}/{name}: {exc}"[:300]
                _write_status(st)
    except Exception as exc:                                     # noqa: BLE001
        st['last_error'] = str(exc)[:300]
    finally:
        st['running'] = False
        st['finished'] = time.time()
        _write_status(st)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── Orphan sweep ────────────────────────────────────────────────────────────

def _id_of(kind, name):
    """The database reference a file belongs to."""
    if kind in ('ocr_images', 'voice_audio'):
        return name                                  # stored as the file name
    stem = name.rsplit('.', 1)[0]
    if kind == 'image_files':
        return re.sub(r'_\d+$', '', stem)            # <prompt_id>_<idx>.<ext>
    return stem                                      # video: <prompt_id>.mp4


_REFS = {'image_files': "SELECT prompt_id FROM image_jobs",
         'video_files': "SELECT prompt_id FROM video_jobs",
         'ocr_images': "SELECT image_path FROM ocr_jobs WHERE image_path IS NOT NULL",
         'voice_audio': "SELECT audio_path FROM voice_jobs WHERE audio_path IS NOT NULL"}


def sweep(grace_days=ORPHAN_GRACE_DAYS, dry_run=False, db_path=DB_PATH):
    """Removes files no database row references any more, older than the grace
    period (a job writes its file before its row in some paths). Returns
    {'local': n, 's3': n, 'dry_run': bool}."""
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=10)
    try:
        refs = {kind: {str(r[0]) for r in con.execute(q)} for kind, q in _REFS.items()}
    finally:
        con.close()
    limit = time.time() - grace_days * 86400
    removed = {'local': 0, 's3': 0, 'dry_run': dry_run}
    for kind in KINDS:
        d = os.path.join(ROOT, kind)
        if not os.path.isdir(d):
            continue
        for f in os.scandir(d):
            if (f.is_file() and not f.name.startswith('.') and f.stat().st_mtime < limit
                    and _id_of(kind, f.name) not in refs[kind]):
                removed['local'] += 1
                if not dry_run:
                    os.remove(f.path)
    if object_store.configured():
        for k, _sz, modified in object_store.list_keys(object_store.key('media')):
            kind, name = k.rsplit('/', 2)[-2:]
            if (kind in refs and modified.timestamp() < limit
                    and _id_of(kind, name) not in refs[kind]):
                removed['s3'] += 1
                if not dry_run:
                    object_store.delete(k)
    return removed


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'sweep':
        print(json.dumps(sweep(dry_run='--dry-run' in sys.argv)))
    else:
        print(__doc__)
        sys.exit(2)
