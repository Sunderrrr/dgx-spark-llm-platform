#!/bin/bash
# dgx-portal sends a FRESH reference sample to /upload_reference at every
# generation (random name, never reused — see voice_clone() in app.py) and
# Chatterbox exposes no deletion route: without this cleanup,
# /app/reference_audio grows indefinitely (up to 15 MB per generation). The
# file is only useful for the /tts call that immediately follows the upload,
# so a one-hour TTL is very generous.
#
# Runs inside the container itself rather than on the portal side: the portal
# is non-root and has no access to this container's filesystem.
set -euo pipefail

cleanup_loop() {
  while true; do
    find /app/reference_audio -maxdepth 1 -type f -mmin +60 -delete 2>/dev/null || true
    sleep 600
  done
}

cleanup_loop &

exec python3 server.py
