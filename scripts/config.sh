#!/usr/bin/env bash
# The ONE place the operational scripts read their world from.
#
# « rien ne doit être monolithique et tout doit être pensé pour fonctionner sur
# l'infra d'une personne qui déploie le service, rien en dur, tout variabilisé,
# qui se base sur le .env » (operator, 2026-10-09). Every path, domain and
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
#   DGX_S3_CFG      s3cmd configuration file (default: <root>/secrets/s3-garage.cfg)
#   DGX_S3_BUCKET   where the backups go (default: s3://dgx/backups)
#   DGX_S3_LOGS     where the journals go (default: s3://dgx/logs)
#   DGX_DEMO_USER   demo account used by the validation journeys
#   DGX_DEMO_PASS   file holding its password (never the password itself)
#   DGX_PYTEST_BIN  the python that carries Playwright

# Where is this repository? Derive it from the script, don't guess.
_RACINE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# The .env is the deployer's contract: it is git-ignored, it already carries
# secrets, and every service already reads it. Read it the same way docker
# compose does — KEY=VALUE, ignore comments and blanks — so a value set there
# applies to the platform AND to its scripts.
if [ -f "$_RACINE/.env" ]; then
  while IFS='=' read -r cle val; do
    case "$cle" in ''|\#*) continue ;; esac
    case "$cle" in
      DGX_*) export "$cle=${val:-}" ;;
    esac
  done < "$_RACINE/.env"
fi

export DGX_ROOT="${DGX_ROOT:-$_RACINE}"
export DGX_WEB="${DGX_WEB:-https://example.org}"
export DGX_API="${DGX_API:-https://api.example.org}"
export DGX_S3_CFG="${DGX_S3_CFG:-$DGX_ROOT/secrets/s3-garage.cfg}"
export DGX_S3_BUCKET="${DGX_S3_BUCKET:-s3://dgx/backups}"
export DGX_S3_LOGS="${DGX_S3_LOGS:-s3://dgx/logs}"
export DGX_DEMO_USER="${DGX_DEMO_USER:-demo}"
export DGX_DEMO_PASS="${DGX_DEMO_PASS:-/root/shots/demo-credentials}"
export DGX_PYTEST_BIN="${DGX_PYTEST_BIN:-/root/shots-venv/bin/python}"
