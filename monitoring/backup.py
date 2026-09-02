#!/usr/bin/env python3
"""Sauvegarde locale des bases CrOS — SQLite du portail + Postgres LiteLLM.

Le but est un backup *local*, reproductible, avec rétention. Il N'est PAS
destiné à être poussé : les dumps contiennent les données de la DB (jetons,
etc.) et vont dans un répertoire hors du dépôt (/var/backups/cronos).

Ce qui est sauvegardé :
  - dgx-portal   → SQLite /app/data/portal.db (snapshot cohérent via l'API
                   sqlite3.backup, copié hors du conteneur).
  - litellm-postgres → dump pg_dump custom (-Fc) de la base `litellm`.

Usage :
  python3 backup.py            # sauvegarde + rétention
  python3 backup.py --list     # liste les backups existants
  python3 backup.py --keep N   # garde N sauvegardes (défaut 14)
  python3 backup.py --dir PATH # répertoire de destination (défaut /var/backups/cronos)
"""
import argparse
import glob
import os
import subprocess
import sys
import time

DEST = "/var/backups/cronos"
KEEP = 14


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, **kw)


def _sqlite_backup(dest_dir):
    ts = time.strftime("%Y%m%d-%H%M%S")
    tmp = "/tmp/portal-backup-{}.db".format(os.getpid())
    out = os.path.join(dest_dir, "portal-{}.db".format(ts))
    # Snapshot cohérent (même si la DB est en écriture) via sqlite3.backup.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep", type=int, default=KEEP)
    ap.add_argument("--dir", default=DEST)
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    # Les dumps contiennent des clés API / journaux : répertoire 0700 et
    # fichiers 0600 (le script tourne en root ; un autre process local ne doit
    # pas pouvoir lire l'historique des clés).
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
    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
