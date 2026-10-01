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

# Fichiers générés montés depuis le volume du portail. Les jobs sont trimés
# (IMAGE_HISTORY_LIMIT etc.) mais les FICHIERS restaient pour toujours : la
# purge des orphelins (plus aucune ligne de job ne référence le prompt_id,
# fichier de plus de GRACE_DAYS jours) reprend l'espace. Les fichiers récents
# sont épargnés : un job « running » écrit son fichier avant de finir.
PORTAL_VOLUME = "/var/lib/docker/volumes/ai-platform_portal_data/_data"
# Chemin INUTILISABLE par l'unité : le volume appartient à uid 10001 en 0700 et
# cronos-backup tourne sans CAP_DAC_OVERRIDE, donc une lecture y renvoie EACCES
# (que `os.path.isdir` traduisait en False, d'où une purge qui annonçait « 0
# orphelin » sans rien faire). On passe par le conteneur, qui le monte ici :
PORTAL_DATA_IN_CONTAINER = "/app/data"
PORTAL_CONTAINER = "dgx-portal"
ORPHAN_DIRS = [("image_files", "image_jobs", "prompt_id"),
               ("music_files", "music_jobs", "job_id"),
               # video_files manquait (audit du 2026-10-02) : les vidéos
               # s'accumulaient sans borne (10 à 100 Mo par job) alors que la
               # table, elle, était bien purgée à 10 lignes par compte.
               ("video_files", "video_jobs", "prompt_id")]
ORPHAN_GRACE_DAYS = 7


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


# Purge des orphelins, exécutée DANS le conteneur du portail : c'est le seul
# propriétaire du volume média (uid 10001, 0700). Voir _purge_orphans.
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
        # `video_files` nomme ses fichiers `<prompt_id>.mp4`, SANS suffixe : la
        # comparaison sur le préfixe avant `_` (pensée pour `<prompt_id>_<index>.png`)
        # ne suffit pas si un identifiant contient un souligné. On teste les DEUX
        # formes — un faux « orphelin » ferait supprimer un fichier encore référencé.
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
    """Supprime les fichiers média dont plus aucun job ne référence le préfixe.

    La référence est le DUMP qui vient d'être écrit, et non la base vivante : si
    portal.db était réinitialisée (la panne que cette purge ne doit surtout pas
    aggraver), la base vivante ne référencerait plus RIEN et tous les médias
    seraient « orphelins ». Le dump, lui, garde l'état du dernier instant sain.

    La suppression passe par `docker exec` : l'unité cronos-backup tourne avec
    CapabilityBoundingSet vide et ProtectSystem=strict, alors que le volume média
    appartient à uid 10001 en 0700. Depuis l'hôte, `os.path.isdir()` AVALAIT
    l'EACCES et rendait False — la purge annonçait donc « 0 orphelin » chaque
    nuit, sans jamais rien supprimer ni échouer. Le conteneur, lui, est chez lui.
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
        # Un échec doit être VISIBLE : le silence précédent a coûté des mois
        # d'accumulation de vidéos de 10 à 100 Mo.
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
    if not args.no_purge:
        try:
            _purge_orphans(args.dir, dry_run=args.purge_dry_run)
        except Exception as exc:
            # La purge est un plus : ne jamais la faire échouer le backup.
            print(f"purge orphelins impossible : {exc}", file=sys.stderr)
    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
