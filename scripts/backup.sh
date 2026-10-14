#!/usr/bin/env bash
# What must survive a disk failure: the data, the configuration, and the
# identity of the instance.
#
# « do point 3 for the backups » (operator, 2026-10-08). Three things make
# a backup worth having and all three are here:
#   1. it is CONSISTENT — the SQLite files are copied with the online backup
#      API (`sqlite3 .backup`), not with `cp`: a `cp` of a live database can
#      give you a file that looks fine and restores nothing;
#   2. it can be OFF-SITE — on the S3 bucket configured in Admin → Config, so
#      the box and its backup can die separately;
#   3. it is VERIFIED — an uploaded archive is read back from the bucket
#      before the run is called a success. An unverified backup is a hope.
#
# WHERE and HOW MANY are set in Admin → Stockage (2026-10-14): local, S3 or
# both, and a retention for each. This script asks the portal for them; the
# S3 upload itself runs INSIDE the portal (dgx-portal/backups.py), so the
# bucket credentials never leave it. If the upload fails, the archive is kept
# locally whatever the destination: a backup that could not leave the box is
# still a backup.
#
# The LOGS are a separate stream (scripts/logs-to-s3.sh): they are high
# volume and low value next to the data, and mixing them made the archive
# 659 MB for 3 MB of database.
#
# What is NOT here, deliberately: the model weights (87 GiB, re-downloadable
# from Hugging Face) and the generated media (they regenerate, and on S3 they
# are already off the box). A backup that costs 100 GiB is a backup nobody
# keeps.
#
# Also run here, nightly: the orphan sweep of the generated files
# (media_store.py) — files whose database row is gone.
. "$(dirname "${BASH_SOURCE[0]}")/config.sh"

set -eu

LOCAL_DIR=/var/backups/dgx/archives
STATE=/var/backups/dgx/state.json
STAMP=$(date +%Y-%m-%d_%H%M)
NAME="dgx-$STAMP.tar.gz"
TMP=$(mktemp -d /tmp/dgx-backup.XXXXXX)
DATA="$TMP/data"
mkdir "$DATA"
ARCHIVE="$TMP/$NAME"
trap 'rm -rf "$TMP"' EXIT

# Records the run for the admin panel and the monitor (they read this file,
# not the bucket). A failed run keeps the time of the last GOOD backup, so the
# monitor's freshness check still fires.
record() {  # record <ok 0|1> <json fields>
  python3 - "$STATE" "$1" "$2" <<'PY'
import json, sys, datetime
path, ok, fields = sys.argv[1], sys.argv[2] == '1', json.loads(sys.argv[3])
try:
    old = json.load(open(path))
except Exception:
    old = {}
now = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
state = dict(old) if not ok else {}
state.update(fields, last_attempt=now, last_ok=ok)
if ok:
    state['last_backup'] = now
    state.pop('error', None)
json.dump(state, open(path + '.tmp', 'w'))
import os; os.replace(path + '.tmp', path)
PY
}
mkdir -p /var/backups/dgx
fail() { echo "✗ $1" >&2; record 0 "$(python3 -c 'import json,sys; print(json.dumps({"error": sys.argv[1]}))' "$1")"; exit 1; }

CFG=$(docker exec dgx-portal python3 -m backups settings) || fail "the portal did not answer (settings)"
get() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$CFG" "$1"; }
DEST=$(get destination); KEEP_LOCAL=$(get keep_local); KEEP_S3=$(get keep_s3); S3_READY=$(get s3_ready)
echo "· destination: $DEST (keep: local $KEEP_LOCAL, S3 $KEEP_S3)"

echo "· portal database"
# sqlite3 is not in the container; the online-backup API of Python's sqlite3
# is the SAME consistency guarantee and needs no extra package.
docker exec dgx-portal python3 -c "
import sqlite3
src = sqlite3.connect('/app/data/portal.db')
dst = sqlite3.connect('/tmp/portal.db')
src.backup(dst)
dst.close(); src.close()
" >/dev/null 2>&1 || fail "portal database snapshot failed"
docker cp dgx-portal:/tmp/portal.db "$DATA/portal.db" >/dev/null 2>&1 || fail "portal database copy failed"

echo "· LiteLLM database (postgres)"
docker exec litellm-postgres pg_dump -U litellm -d litellm > "$DATA/litellm.sql" 2>/dev/null || fail "LiteLLM dump failed"

echo "· configuration and identity"
cp "${DGX_ROOT}/docker-compose.yml" "$DATA/docker-compose.yml"
cp "${DGX_ROOT}/.env" "$DATA/env" 2>/dev/null || true
tar -C "${DGX_ROOT}" -czf "$DATA/secrets.tar.gz" secrets 2>/dev/null || true

tar -C "$DATA" -czf "$ARCHIVE" . || fail "archive creation failed"
SIZE=$(du -h "$ARCHIVE" | cut -f1)

S3_RESULT=""
UPLOAD_ERROR=""
if [ "$DEST" != "local" ]; then
  if [ "$S3_READY" != "True" ]; then
    UPLOAD_ERROR="S3 is not configured (Admin → Config)"
  else
    echo "· upload to S3"
    S3_RESULT=$(docker exec -i dgx-portal python3 -m backups upload "$NAME" "$KEEP_S3" < "$ARCHIVE") \
      || UPLOAD_ERROR=$(python3 -c 'import json,sys
try: print(json.loads(sys.argv[1]).get("error") or "upload failed")
except Exception: print("upload failed")' "$S3_RESULT")
  fi
fi

# Local copy: when asked for, and ALWAYS when the upload failed.
KEPT_LOCAL=""
if [ "$DEST" != "s3" ] || [ -n "$UPLOAD_ERROR" ]; then
  install -d -m 0700 "$LOCAL_DIR"
  install -m 0600 "$ARCHIVE" "$LOCAL_DIR/$NAME"
  KEPT_LOCAL=1
fi
# Retention: the KEEP_LOCAL most recent archives (one per night) — applied
# whatever the destination, so the fallback copies of a failed upload do not
# pile up once S3 works again.
if [ -d "$LOCAL_DIR" ]; then
  find "$LOCAL_DIR" -maxdepth 1 -name 'dgx-*.tar.gz' -printf '%f\n' | sort -r | tail -n +"$((KEEP_LOCAL + 1))" \
    | while read -r old; do rm -f "$LOCAL_DIR/$old"; echo "· purged (local): $old"; done
fi
LOCAL_COUNT=$(find "$LOCAL_DIR" -maxdepth 1 -name 'dgx-*.tar.gz' 2>/dev/null | wc -l)

echo "· orphan sweep of the generated files"
docker exec dgx-portal python3 -m media_store sweep || echo "  (sweep failed — see the portal logs)"

FIELDS=$(python3 - "$NAME" "$SIZE" "$DEST" "$LOCAL_COUNT" "$KEPT_LOCAL" "$S3_RESULT" "$UPLOAD_ERROR" <<'PY'
import json, sys
name, size, dest, local_count, kept_local, s3, err = sys.argv[1:8]
try:
    s3 = json.loads(s3) if s3 else None
except ValueError:
    s3 = None
out = {'file': name, 'size': size, 'destination': dest,
       'local_count': int(local_count), 'local': bool(kept_local),
       's3': bool(s3 and s3.get('ok')), 's3_count': (s3 or {}).get('kept'),
       's3_purged': (s3 or {}).get('purged', [])}
if err:
    out['error'] = err
print(json.dumps(out))
PY
)
if [ -n "$UPLOAD_ERROR" ]; then
  # The data is safe locally, but the off-site copy the operator asked for is
  # missing: that is a failed run, and it says so.
  record 0 "$FIELDS"
  echo "✗ S3 upload failed ($UPLOAD_ERROR) — archive kept locally: $LOCAL_DIR/$NAME" >&2
  exit 1
fi
record 1 "$FIELDS"
echo "✓ backed up: $NAME ($SIZE) → $DEST"
