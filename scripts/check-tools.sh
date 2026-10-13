#!/usr/bin/env bash
# Static-analysis tools of the validation (ruff, shellcheck, pip-audit,
# bandit), in a venv of their own: pinned versions, nothing installed into the
# system Python, nothing added to the portal image.
#
#   scripts/check-tools.sh            # create/update the venv, print its bin dir
#
# The venv lives at DGX_TOOLS_VENV (default /root/.venvs/dgx-tools). Bumping a
# version below re-installs on the next run.
set -euo pipefail

VENV=${DGX_TOOLS_VENV:-/root/.venvs/dgx-tools}
PINS=(ruff==0.17.0 shellcheck-py==0.11.0.1 pip-audit==2.10.1 bandit==1.9.4)
STAMP="$VENV/.pins"

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" >&2
fi
if [ "$(cat "$STAMP" 2>/dev/null)" != "${PINS[*]}" ]; then
  "$VENV/bin/pip" install -q --disable-pip-version-check "${PINS[@]}" >&2
  printf '%s' "${PINS[*]}" > "$STAMP"
fi
printf '%s\n' "$VENV/bin"
