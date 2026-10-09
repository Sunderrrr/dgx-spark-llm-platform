#!/usr/bin/env python3
"""Local backup of the CrOS databases — portal SQLite + LiteLLM Postgres.

The goal is a *local*, reproducible backup, with retention. It is NOT meant to
be pushed anywhere: the dumps hold the DB data (tokens, etc.) and go to a
directory outside the repo (/var/backups/dgx).

What is backed up:
  - dgx-portal   → SQLite /app/data/portal.db (consistent snapshot via the
                   sqlite3.backup API, copied out of the container).
  - litellm-postgres → pg_dump custom (-Fc) dump of the `litellm` database.

Usage :
  python3 backup.py            # backup + retention
  python3 backup.py --list     # list existing backups
  python3 backup.py --keep N   # keep N backups (default 14)
  python3 backup.py --dir PATH # destination directory (default /var/backups/dgx)
"""
import argparse
import glob
import os
import subprocess
import sys
import time

DEST = "/var/backups/dgx"
KEEP = 14

# Generated files mounted from the portal volume. The jobs are trimmed
# (IMAGE_HISTORY_LIMIT etc.) but the FILES stayed forever: the orphan purge
# (no job row references the prompt_id anymore, file older than GRACE_DAYS
# days) reclaims the space. Recent files are spared: a « running » job writes
# its file before finishing.
PORTAL_VOLUME = "/var/lib/docker/volumes/ai-platform_portal_data/_data"
# Path UNUSABLE by the unit: the volume belongs to uid 10001 in 0700 and
# cronos-backup runs without CAP_DAC_OVERRIDE, so a read there returns EACCES
# (which `os.path.isdir` turned into False, hence a purge announcing « 0
# orphelin » while doing nothing). We go through the container, which mounts it
# here:
PORTAL_DATA_IN_CONTAINER = "/app/data"
PORTAL_CONTAINER = "dgx-portal"
ORPHAN_DIRS = [("image_files", "image_jobs", "prompt_id"),
               ("music_files", "music_jobs", "job_id"),
               # video_files was missing (audit of 2026-10-02): videos piled
               # up unbounded (10 to 100 MB per job) while the table itself was
               # properly purged to 10 rows per account.
               ("video_files", "video_jobs", "prompt_id")]
ORPHAN_GRACE_DAYS = 7


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, **kw)


def _sqlite_backup(dest_dir):
    ts = time.strftime("%Y%m%d-%H%M%S")
    tmp = "/tmp/portal-backup-{}.db".format(os.getpid())
    out = os.path.join(dest_dir, "portal-{}.db".format(ts))
    # Consistent snapshot (even if the DB is being written) via sqlite3.backup.
    r = _run(["docker", "exec", "dgx-portal", "python3", "-c",
              "import sqlite3;"
              f" src=sqlite3.connect('/app/data/portal.db');"
              f" dst=sqlite3.connect('{tmp}');"
              f" src.backup(dst);"
              f" dst.close(); src.close()"])
    if r.returncode != 0:
        return None, f"sqlite backup failed: {r.stderr.strip()}"
    r2 = _run(["docker", "cp", "dgx-portal:{}".format(tmp), out])
    _run(["docker", "exec", "dgx-portal", "rm", "-f", tmp])
    if r2.returncode != 0:
        return None, f"docker cp failed: {r2.stderr.strip()}"
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    return out, None


def _pg_dump(dest_dir):
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(dest_dir, "litellm-{}.dump".format(ts))
    with open(out, "wb") as fh:
        r = subprocess.run(
            ["docker", "exec", "litellm-postgres",
             "pg_dump", "-U", "litellm", "-Fc", "litellm"],
            stdout=fh, stderr=subprocess.PIPE, timeout=180)
    if r.returncode != 0:
        return None, f"pg_dump failed: {r.stderr.decode().strip()}"
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    return out, None


def _list(dest_dir):
    for path in sorted(glob.glob(os.path.join(dest_dir, "portal-*.db"))
                       + glob.glob(os.path.join(dest_dir, "litellm-*.dump"))):
        size = os.path.getsize(path)
        print(f"{os.path.basename(path):30s} {size/1024:,.0f} KiB")


def _retain(dest_dir, keep):
    for prefix in ("portal-", "litellm-"):
        files = sorted(glob.glob(os.path.join(dest_dir, prefix + "*")))
        for old in files[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass


# Orphan purge, run INSIDE the portal container: it is the only owner of the
# media volume (uid 10001, 0700). See _purge_orphans.
_PURGE_CODE = r'''
import json, os, sys, time
charge = json.load(sys.stdin)
racine, grace, dry = charge["racine"], charge["grace"], charge["dry"]
total = 0
for dossier, refs in charge["refs"].items():
    refs = set(refs)
    d = os.path.join(racine, dossier)
    if not os.path.isdir(d):
        print(f"  {dossier} : répertoire absent")
        continue
    for f in sorted(os.listdir(d)):
        # `video_files` names its files `<prompt_id>.mp4`, WITHOUT suffix: the
        # prefix comparison before `_` (meant for `<prompt_id>_<index>.png`)
        # is not enough if an id contains an underscore. We test BOTH forms —
        # a false « orphelin » would delete a still-referenced file.
        pref = os.path.splitext(f.split("_")[0])[0]
        complet = os.path.splitext(f)[0]
        chemin = os.path.join(d, f)
        if pref in refs or complet in refs or not os.path.isfile(chemin):
            continue
        age_j = (time.time() - os.path.getmtime(chemin)) / 86400
        if age_j < grace:
            continue
        total += 1
        if dry:
            print(f"  orphelin (serait supprimé) : {dossier}/{f} ({age_j:.0f} j)")
            continue
        try:
            os.remove(chemin)
            print(f"  orphelin supprimé : {dossier}/{f} ({age_j:.0f} j)")
        except OSError as exc:
            print(f"  échec suppression {dossier}/{f} : {exc}")
print(f"purge orphelins : {total} fichier(s) {'(dry-run)' if dry else 'supprimé(s)'}")
'''


def _purge_orphans(dest_dir, dry_run=False):
    """Delete the media files whose prefix no job references anymore.

    The reference is the DUMP that was just written, not the live database: if
    portal.db were reset (the failure this purge must above all not make worse),
    the live database would reference NOTHING anymore and every media file would
    be « orphelin ». The dump, on the other hand, keeps the state of the last
    healthy instant.

    Deletion goes through `docker exec`: the cronos-backup unit runs with an
    empty CapabilityBoundingSet and ProtectSystem=strict, while the media volume
    belongs to uid 10001 in 0700. From the host, `os.path.isdir()` SWALLOWED the
    EACCES and returned False — the purge therefore announced « 0 orphelin »
    every night, never deleting anything nor failing. The container, however, is
    at home there.
    """
    import json
    import sqlite3
    dumps = glob.glob(os.path.join(dest_dir, "portal-*.db"))
    if not dumps:
        print("purge orphelins : aucun dump, on s'abstient")
        return
    latest = max(dumps, key=os.path.getmtime)
    conn = sqlite3.connect(f"file:{latest}?mode=ro", uri=True)
    refs = {}
    for dossier, table, col in ORPHAN_DIRS:
        refs[dossier] = [str(r[0]) for r in conn.execute(f"SELECT {col} FROM {table}")]
    conn.close()
    charge = json.dumps({"racine": PORTAL_DATA_IN_CONTAINER, "grace": ORPHAN_GRACE_DAYS,
                         "dry": dry_run, "refs": refs})
    r = subprocess.run(["docker", "exec", "-i", PORTAL_CONTAINER, "python3", "-c", _PURGE_CODE],
                       input=charge.encode(), capture_output=True, timeout=300)
    sortie = (r.stdout or b"").decode().rstrip()
    if sortie:
        print(sortie)
    if r.returncode != 0:
        # A failure must be VISIBLE: the previous silence cost months of
        # accumulated 10-to-100 MB videos.
        print(f"purge orphelins : ÉCHEC ({r.returncode}) : "
              f"{(r.stderr or b'').decode().strip()[:300]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep", type=int, default=KEEP)
    ap.add_argument("--dir", default=DEST)
    ap.add_argument("--no-purge", action="store_true",
                    help="ne pas purger les fichiers orphelins")
    ap.add_argument("--purge-dry-run", action="store_true",
                    help="liste les orphelins sans rien supprimer")
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    # The dumps hold API keys / logs: 0700 directory and 0600 files (the script
    # runs as root; another local process must not be able to read the key
    # history).
    try:
        os.chmod(args.dir, 0o700)
    except OSError:
        pass
    if args.list:
        _list(args.dir)
        return 0

    errors = []
    out, err = _sqlite_backup(args.dir)
    if out:
        print("backup portal:", out)
    else:
        errors.append(err or "portal backup failed")
    out, err = _pg_dump(args.dir)
    if out:
        print("backup litellm:", out)
    else:
        errors.append(err or "litellm backup failed")

    _retain(args.dir, args.keep)
    if not args.no_purge:
        try:
            _purge_orphans(args.dir, dry_run=args.purge_dry_run)
        except Exception as exc:
            # The purge is a bonus: never let it fail the backup.
            print(f"purge orphelins impossible : {exc}", file=sys.stderr)
    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
