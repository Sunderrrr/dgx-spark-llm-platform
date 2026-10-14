#!/usr/bin/env bash
# The ONE place the operational scripts read their world from.
#
# « nothing must be monolithic and everything must be designed to work on the
# infrastructure of whoever deploys the service, nothing hardcoded, everything
# configurable, based on the .env » (operator, 2026-10-09). Every path, domain and
# bucket used to be spelled out in each script — true on THIS box, false on the
# next one. Now every script sources this file and the deployer edits `.env`,
# never the scripts.
#
# Each value has a default that matches a plain clone: `source` this, get
# something that works. The deployer overrides what differs in `.env`.
#
#   DGX_ROOT        the checkout (default: this repository)
#   DGX_WEB         the web UI origin (default: https://example.org)
#   DGX_API         the API origin (default: https://api.example.org)
#   DGX_S3_CFG      s3cmd configuration file of the log shipping (default:
#                   <root>/secrets/s3-garage.cfg). The BACKUPS use the S3 of
#                   Admin → Config instead (scripts/backup.sh).
#   DGX_S3_LOGS     where the journals go (default: s3://dgx/logs)
#   DGX_DEMO_USER   demo account used by the validation journeys
#   DGX_DEMO_PASS   file holding its password (never the password itself)
#   DGX_PYTEST_BIN  the python that carries Playwright
#   DGX_RUNNER_URL  the model runner's control API (default: http://127.0.0.1:8001)

# Where is this repository? Derive it from the script, don't guess.
_REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# The .env is the deployer's contract: it is git-ignored, it already carries
# secrets, and every service already reads it. Read it the same way docker
# compose does — KEY=VALUE, ignore comments and blanks — so a value set there
# applies to the platform AND to its scripts.
if [ -f "$_REPO_ROOT/.env" ]; then
  while IFS='=' read -r key val; do
    case "$key" in ''|\#*) continue ;; esac
    case "$key" in
      DGX_*) export "$key=${val:-}" ;;
    esac
  done < "$_REPO_ROOT/.env"
fi

export DGX_ROOT="${DGX_ROOT:-$_REPO_ROOT}"
export DGX_WEB="${DGX_WEB:-https://example.org}"
export DGX_API="${DGX_API:-https://api.example.org}"
export DGX_S3_CFG="${DGX_S3_CFG:-$DGX_ROOT/secrets/s3-garage.cfg}"
export DGX_S3_LOGS="${DGX_S3_LOGS:-s3://dgx/logs}"
export DGX_DEMO_USER="${DGX_DEMO_USER:-demo}"
export DGX_DEMO_PASS="${DGX_DEMO_PASS:-/root/shots/demo-credentials}"
export DGX_PYTEST_BIN="${DGX_PYTEST_BIN:-/root/shots-venv/bin/python}"
export DGX_RUNNER_URL="${DGX_RUNNER_URL:-http://127.0.0.1:8001}"

# The deployer's values are the point of all this: if a domain is still the
# generic default, say so LOUDLY instead of probing example.org and reporting
# « the site is down » — which is a lie about the platform and a truth about
# the configuration.
_check_domain() {
  case "$1" in
    *example.org|*example.com)
      printf '\033[31m✗ %s is still the default value (%s).\033[0m\n' "$2" "$1" >&2
      printf '   Set it in .env — see the DGX_* block of .env.example\n' >&2
      return 1 ;;
  esac
  return 0
}
# Only the scripts that PROBE a domain need one (smoke, validation). A backup
# or a log shipment must run whatever the domain is called.
if [ "${DGX_CHECK_DOMAINS:-}" = "1" ]; then
  _check_domain "$DGX_WEB" DGX_WEB || exit 2
  _check_domain "$DGX_API" DGX_API || exit 2
fi
