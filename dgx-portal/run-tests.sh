#!/bin/sh
# Runs the test suite in a throwaway container built from the portal image:
# same dependencies as production, and a brand-new SQLite database (no DATA
# volume mounted) — the tests therefore never touch the real data. Sole
# exception, read-only: `asr/server.py`, the dictation sidecar code (outside
# the portal image) — mounted to be tested, cf. tests/test_asr_sidecar.py
# (2026-10-03, coverage scan).
#
#   ./dgx-portal/run-tests.sh            # everything
#   ./dgx-portal/run-tests.sh test_app   # a single module
set -e
cd "$(dirname "$0")/.."
# The name of the built image depends on the clone FOLDER name
# (`<projet>-dgx-portal`), and the `docker run` below must target the same:
# without this pinning, the command documented in the README (« git clone …
# puis ./dgx-portal/run-tests.sh ») fails anywhere but in a folder named
# « ai-platform », with an « Unable to find image ». CI already pins this
# name (.github/workflows/ci.yml), so both now follow the same rule.
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ai-platform}"
docker compose build dgx-portal >/dev/null
# The unittest arguments are chosen BEFORE the exec: `${1:+tests.$1}
# ${1:-discover -s tests}` passed the module TWICE (« tests.test_app
# test_app »), so every single-module run ended with
# « FAILED (errors=1) » because of a missing module — a false failure at the
# end of a command the docs recommend.
if [ -n "${1:-}" ]; then
  set -- "tests.$1"
else
  set -- discover -s tests
fi
exec docker run --rm \
  -v "$PWD/asr/server.py:/app/tests/asr_sidecar.py:ro" \
  -e SECRET_KEY=test-secret-0123456789abcdef0123456789abcdef \
  -e LITELLM_MASTER_KEY=sk-test \
  -e CRONOS_NO_REAPER=1 \
  --entrypoint python ai-platform-dgx-portal \
  -m unittest "$@" -v
