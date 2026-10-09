#!/usr/bin/env bash
# The journals, on their own stream: high volume, low value next to the data.
#
# « tien les logs de mon s3 garage » — they land under ${DGX_S3_LOGS}/ so a
# forensic read is a `s3cmd ls`, and the backup archive stays what it should
# be: the data. Compressed daily, rotated out after 14 days, same as the
# backups.
# Retention: 7 archives, i.e. 7 days.
# Le monde extérieur (chemins, domaines, bucket) vient de .env — voir
# scripts/config.sh : rien en dur dans ce script.
. "$(dirname "${BASH_SOURCE[0]}")/config.sh"

set -eu
S3CFG=${DGX_ROOT}/secrets/s3-garage.cfg
BUCKET=${DGX_S3_LOGS}
STAMP=$(date +%Y-%m-%d)
TMP=$(mktemp -d /tmp/dgx-logs.XXXXXX)
trap 'rm -rf "$TMP" "$ARCHIVE"' EXIT
S3="s3cmd -c $S3CFG"

echo "· journaux des conteneurs"
for c in dgx-portal litellm litellm-postgres traefik; do
  docker logs "$c" > "$TMP/$c.log" 2>&1 || true
done

echo "· journaux système (non compressés, 7 derniers jours)"
find /var/log -type f ! -name '*.gz' ! -name '*.1' -mtime -7 -size +1k 2>/dev/null | \
  tar -C / -czf "$TMP/systeme.tar.gz" -T - 2>/dev/null || true

echo "· envoi"
ARCHIVE="/tmp/journaux-$STAMP.tar.gz"
tar -C "$TMP" -czf "$ARCHIVE" . >/dev/null 2>&1
$S3 put "$ARCHIVE" "$BUCKET/journaux-$STAMP.tar.gz" >/dev/null
$S3 ls "$BUCKET/journaux-$STAMP.tar.gz" >/dev/null
echo "✓ journaux : journaux-$STAMP.tar.gz ($(du -h "$ARCHIVE" | cut -f1)) → $BUCKET"

$S3 ls "$BUCKET/" 2>/dev/null | awk '{print $4}' | sort -r | tail -n +8 | while read -r vieux; do
  [ -n "$vieux" ] && $S3 del "$BUCKET/$vieux" >/dev/null
done
