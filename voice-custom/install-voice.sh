#!/bin/bash
# Install a reference voice and make it the active personal voice.
#
#   sudo voice-custom/install-voice.sh <reference_dir> <name>
#
#   <reference_dir>  ref.wav + ref.lab, as written by reference/try_voice.sh
#   <name>           directory name under /var/lib/voice-custom (a-z 0-9 _ -),
#                    also the voice id the portal shows (first 16 characters)
#
# Run by the operator by hand (not by the runner). The copy is root-owned and
# read-only for the container; previous voices stay on disk, so going back is
# this script on the older directory. It does NOT restart anything: the new
# voice is loaded at the next launch of the « custom-voice » engine
# (Admin → Modèles), never behind the user's back.
set -euo pipefail

if [ $# -ne 2 ]; then
  sed -n 2,14p "$0" >&2
  exit 2
fi
SRC=$(readlink -f "$1")
NAME=$2
ROOT=/var/lib/voice-custom

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[[ "$NAME" =~ ^[a-z0-9_-]{1,32}$ ]] || { echo "name: 1–32 chars, a-z 0-9 _ -" >&2; exit 2; }
for f in ref.wav ref.lab; do
  [ -s "$SRC/$f" ] || { echo "not a reference voice: $SRC/$f missing or empty" >&2; exit 2; }
done

install -d -o root -g root -m 0755 "$ROOT"
DEST="$ROOT/$NAME"
TMP="$ROOT/.$NAME.tmp"
rm -rf "$TMP"
install -d -o root -g root -m 0755 "$TMP"
install -o root -g root -m 0644 "$SRC/ref.wav" "$SRC/ref.lab" "$TMP/"
printf '{"name": "%s", "speaker": "%s", "source": "%s", "installed_at": "%s"}\n' \
  "$NAME" "${NAME:0:16}" "$SRC" "$(date -Iseconds)" > "$TMP/VOICE.json"
chmod 0644 "$TMP/VOICE.json"
rm -rf "$DEST"
mv "$TMP" "$DEST"
# Atomic switch: a launch that resolves the link mid-install sees the old
# voice or the new one, never a half-copied directory.
ln -sfn "$NAME" "$ROOT/.active.new"
mv -T "$ROOT/.active.new" "$ROOT/active"

echo "voice « $NAME » installed in $DEST and made active."
echo "Load it: Admin → Modèles → voix « custom-voice » → Lancer (or relaunch it if it is already the voice engine)."
