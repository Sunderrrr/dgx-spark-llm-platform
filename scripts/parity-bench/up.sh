#!/usr/bin/env bash
# Parity bench (docs/rust-migration.md): the stack's Flask and Rust images, side
# by side on an internal Docker network with a synthetic database and a fake
# upstream — admin routes and writes are compared here, never on live data.
#
#   scripts/parity-bench/up.sh            # (re)create everything, fresh seed
#   scripts/parity-bench/up.sh rs         # recreate the Rust side only (after a build)
#   scripts/parity-bench/up.sh down       # remove the containers and network
#
# Images: ai-platform-dgx-portal and ai-platform-portal-rs (docker compose build).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
B="${PARITY_BENCH_DIR:-$HOME/.cache/parity-bench}"
FLASK_IMG=ai-platform-dgx-portal
RS_IMG=ai-platform-portal-rs
F=http://bench-fake:9000
ENVS=(-e VLLM_RUNNER_URL=$F/runner -e RUNNER_TOKEN=t -e VLLM_API_URL=$F/engine/v1
      -e LITELLM_URL=$F/litellm -e LITELLM_MASTER_KEY=k -e OCR_URL=$F/ocr/v1
      -e VOICE_URL=$F/voice -e ASR_URL=$F/asr -e IMAGE_URL=$F/image -e COMFYUI_URL=$F/comfy
      -e SESSION_COOKIE_SECURE=0)

rs() {
  docker rm -f bench-rs >/dev/null 2>&1 || true
  docker run -d --name bench-rs --network bench-net -v "$B/data-rs:/app/data" \
    -e DATABASE_URL=sqlite:/app/data/portal.db -e FLASK_URL=http://bench-flask:5000 \
    "${ENVS[@]}" "$RS_IMG" >/dev/null
  until docker exec bench-rs curl -sf -o /dev/null http://127.0.0.1:5100/healthz; do sleep 1; done
}

case "${1:-up}" in
  down)
    docker rm -f bench-fake bench-flask bench-rs >/dev/null 2>&1 || true
    docker network rm bench-net >/dev/null 2>&1 || true
    exit 0 ;;
  rs) rs; echo "bench-rs up"; exit 0 ;;
esac

mkdir -p "$B/data-flask" "$B/data-rs"
[ -s "$B/secret" ] || (umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > "$B/secret")
SK="$(cat "$B/secret")"
"$0" down
# The containers run as an unprivileged user: the data dirs must be theirs.
UID_IN="$(docker run --rm --entrypoint id "$FLASK_IMG" -u)"
chown "$UID_IN" "$B/data-flask" "$B/data-rs" 2>/dev/null || chmod 777 "$B/data-flask" "$B/data-rs"

# Seed: Flask creates its schema on import, seed.py fills it; both sides start
# from a copy of the result, and bench.py restores it before every write.
rm -f "$B"/data-flask/portal.db* "$B"/data-rs/portal.db*
docker run --rm --network none -v "$B/data-flask:/app/data" -e SECRET_KEY="$SK" "$FLASK_IMG" python3 -c "import app" >/dev/null 2>&1
docker run --rm --network none -v "$B/data-flask:/app/data" -v "$HERE/seed.py:/seed.py:ro" "$FLASK_IMG" python3 /seed.py
cp "$B/data-flask/portal.db" "$B/portal.seed.db"
cp "$B/portal.seed.db" "$B/data-rs/portal.db"
chown -R "$UID_IN" "$B/data-flask" "$B/data-rs" 2>/dev/null || true

docker network create --internal bench-net >/dev/null
docker run -d --name bench-fake --network bench-net -v "$HERE/fake.py:/fake.py:ro" "$FLASK_IMG" python3 /fake.py >/dev/null
docker run -d --name bench-flask --network bench-net -v "$B/data-flask:/app/data" \
  -v "$B/data-rs:/bench/rs" -v "$B/portal.seed.db:/bench/seed.db:ro" \
  -e SECRET_KEY="$SK" "${ENVS[@]}" "$FLASK_IMG" >/dev/null
rs
until docker exec bench-flask python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/healthz')" 2>/dev/null; do sleep 1; done
echo "bench up: $B"
