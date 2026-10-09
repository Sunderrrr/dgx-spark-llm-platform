#!/usr/bin/env bash
# What must survive a disk failure: the data, the configuration, and the
# identity of the instance.
#
# « fait le point 3 pour les backup » (operator, 2026-10-08). Three things make
# a backup worth having and all three are here:
#   1. it is CONSISTENT — the SQLite files are copied with the online backup
#      API (`sqlite3 .backup`), not with `cp`: a `cp` of a live database can
#      give you a file that looks fine and restores nothing;
#   2. it is OFF-SITE — it lands in the operator's own Garage bucket (S3), so
#      the box and its backup can die separately;
#   3. it is VERIFIED — every archive is listed back from the bucket before
#      the run is called a success. An unverified backup is a hope.
#
# The JOURNAUX are a separate stream (scripts/logs-to-s3.sh): they are high
# volume and low value next to the data, and mixing them made the archive
# 659 MB for 3 MB of database.
#
# What is NOT here, deliberately: the model weights (87 GiB, re-downloadable
# from Hugging Face) and the media output (images/videos regenerate). A backup
# that costs 100 GiB is a backup nobody keeps.
#
# The S3 credentials live in secrets/s3-garage.cfg (0600), never in git.
# Le monde extérieur (chemins, domaines, bucket) vient de .env — voir
# scripts/config.sh : rien en dur dans ce script.
. "$(dirname "${BASH_SOURCE[0]}")/config.sh"

set -eu

S3CFG=${DGX_ROOT}/secrets/s3-garage.cfg
BUCKET=${DGX_S3_BUCKET}
STAMP=$(date +%Y-%m-%d_%H%M)
TMP=$(mktemp -d /tmp/dgx-backup.XXXXXX)
trap 'rm -rf "$TMP" "$ARCHIVE"' EXIT

[ -f "$S3CFG" ] || { echo "✗ $S3CFG manquant — sauvegarde impossible." >&2; exit 1; }
S3="s3cmd -c $S3CFG"

echo "· base du portail"
# sqlite3 is not in the container; the online-backup API of Python's sqlite3
# is the SAME consistency guarantee and needs no extra package.
docker exec dgx-portal python3 -c "
import sqlite3
src = sqlite3.connect('/app/data/portal.db')
dst = sqlite3.connect('/tmp/portal.db')
src.backup(dst)
dst.close(); src.close()
" >/dev/null 2>&1
docker cp dgx-portal:/tmp/portal.db "$TMP/portal.db" >/dev/null 2>&1

echo "· base LiteLLM (postgres)"
docker exec litellm-postgres pg_dump -U litellm -d litellm > "$TMP/litellm.sql" 2>/dev/null

echo "· configuration et identité"
cp ${DGX_ROOT}/docker-compose.yml "$TMP/docker-compose.yml"
cp ${DGX_ROOT}/.env "$TMP/env" 2>/dev/null || true
tar -C ${DGX_ROOT} -czf "$TMP/secrets.tar.gz" secrets 2>/dev/null || true

echo "· envoi"
ARCHIVE="/tmp/dgx-$STAMP.tar.gz"
tar -C "$TMP" -czf "$ARCHIVE" . >/dev/null 2>&1
$S3 put "$ARCHIVE" "$BUCKET/dgx-$STAMP.tar.gz" >/dev/null
$S3 ls "$BUCKET/dgx-$STAMP.tar.gz" >/dev/null

TAILLE=$(du -h "$ARCHIVE" | cut -f1)
echo "✓ sauvegardée : dgx-$STAMP.tar.gz ($TAILLE) → $BUCKET"

# Retention: 7 days (operator, 2026-10-09). A daily archive is one per
# night — the eighth oldest is a week old and out of the window.
$S3 ls "$BUCKET/" 2>/dev/null | awk '{print $4}' | sort -r | tail -n +8 | while read -r vieux; do
  [ -n "$vieux" ] && { $S3 del "$BUCKET/$vieux" >/dev/null; echo "· purgé : $vieux"; }
done
