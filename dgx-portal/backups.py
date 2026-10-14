"""Backups: where they go and how many are kept, set in Admin → Stockage.

The archive itself is built on the HOST (scripts/backup.sh: it needs the
LiteLLM database, `.env` and `secrets/`, none of which the portal can reach).
The host asks the portal for the settings, keeps local copies itself, and
hands the archive to `upload` for the bucket — so the S3 credentials, set in
Admin → Config, never leave the portal.

CLI, through `docker exec [-i] dgx-portal python3 -m backups …`:
  settings                 the settings as JSON (no secret in it)
  upload <name> <keep>     stdin → <prefix>/backups/<name>, verified, then
                           only the <keep> most recent archives are kept
"""
import json
import sys

from db import read_setting
import object_store

DESTINATIONS = ('local', 's3', 'both')
DEFAULT_KEEP = 7
MAX_KEEP = 365
STATE_FILE = '/var/backups/dgx/state.json'      # written by the host, mounted read-only


def _keep(name):
    try:
        return max(1, min(MAX_KEEP, int(read_setting(name, '') or DEFAULT_KEEP)))
    except ValueError:
        return DEFAULT_KEEP


def settings():
    destination = read_setting('backup_destination', '') or 'local'
    if destination not in DESTINATIONS:
        destination = 'local'
    return {
        'destination': destination,
        'keep_local': _keep('backup_keep_local'),
        'keep_s3': _keep('backup_keep_s3'),
        's3_ready': object_store.configured(),
    }


def last_state():
    """What the host recorded about the last run, or None."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _archives():
    """[(key, size, last_modified)] of the archives in the bucket, newest first."""
    prefix = object_store.key('backups')
    found = [o for o in object_store.list_keys(prefix) if o[0].endswith('.tar.gz')]
    return sorted(found, key=lambda o: o[0], reverse=True)


def upload(name, keep, stream):
    """Uploads one archive, checks it is there, prunes the older ones."""
    if not name.endswith('.tar.gz') or '/' in name:
        raise ValueError(f"unexpected archive name {name!r}")
    k = object_store.key('backups', name)
    object_store.put_stream(k, stream)
    meta = object_store.head(k)
    if not meta or not meta['ContentLength']:
        raise IOError(f"{k} not found after upload")
    purged = []
    for old_key, _size, _m in _archives()[keep:]:
        object_store.delete(old_key)
        purged.append(old_key.rsplit('/', 1)[-1])
    return {'key': k, 'size': meta['ContentLength'], 'kept': min(keep, len(_archives())),
            'purged': purged}


def main(argv):
    if argv[:1] == ['settings']:
        print(json.dumps(settings()))
        return 0
    if argv[:1] == ['upload'] and len(argv) == 3:
        try:
            out = upload(argv[1], max(1, int(argv[2])), sys.stdin.buffer)
        except Exception as exc:                                 # noqa: BLE001
            print(json.dumps({'ok': False, 'error': str(exc)[:300]}))
            return 1
        print(json.dumps({'ok': True, **out}))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
