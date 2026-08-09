#!/usr/bin/env bash
# Pre-push safety gate for Cronos.
#
# Two checks, both must pass before code leaves the box:
#   1. Secret scan — refuses to push a diff that adds a secret file
#      (.env, DEBUG_USERS.txt, private keys…) or a secret-looking value.
#   2. Test suite  — runs the backend tests in a throwaway container.
#
# Usage:
#   ./scripts/pre-push-check.sh              # scan @{u}..HEAD, run tests
#   SKIP_TESTS=1 ./scripts/pre-push-check.sh # scan only (fast)
#
# It is also installed as .git/hooks/pre-push (see scripts/install-git-hooks.sh),
# where git feeds it "<local ref> <local sha> <remote ref> <remote sha>" on stdin.
#
# Exit non-zero → push is aborted. Never push around a red gate.
set -u
cd "$(dirname "$0")/.."

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
fail() { printf '%s✗ %s%s\n' "$RED" "$1" "$RST" >&2; }
ok()   { printf '%s✓ %s%s\n' "$GRN" "$1" "$RST"; }
info() { printf '%s• %s%s\n' "$YEL" "$1" "$RST"; }

# ── Determine the commit range being pushed ───────────────────────────────────
# As a pre-push hook, git passes ref lines on stdin. Standalone, fall back to the
# upstream tracking branch, then to the whole history if there is no upstream.
ranges=()
if [ ! -t 0 ]; then
  while read -r _local_ref local_sha _remote_ref remote_sha; do
    [ -z "${local_sha:-}" ] && continue
    case "$local_sha" in *[!0]*) : ;; *) continue ;; esac   # deleting a ref: skip
    if printf '%s' "$remote_sha" | grep -qE '^0+$'; then
      ranges+=("$local_sha")                                # new branch: all commits
    else
      ranges+=("$remote_sha..$local_sha")
    fi
  done
fi
if [ ${#ranges[@]} -eq 0 ]; then
  if up=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
    ranges=("$up..HEAD")
  else
    ranges=("HEAD")
  fi
fi
info "Range(s) to inspect: ${ranges[*]}"

# ── 1. Secret scan ────────────────────────────────────────────────────────────
# Filenames that must never be committed (an .env.example placeholder is fine).
FORBIDDEN_FILE_RE='(^|/)(\.env(\..*)?|DEBUG_USERS\.txt|.*\.pem|.*\.key|.*\.p12|id_rsa[^/]*)$'
FORBIDDEN_FILE_ALLOW='(^|/)\.env\.example$'
# High-signal secret value patterns (kept tight to avoid crying wolf).
SECRET_VALUE_RE='(-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9]{24,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})'
# Obvious placeholders we never want to flag as a real secret.
PLACEHOLDER_RE='(changeme|change-me|placeholder|example|sk-test|sk-changeme|your[-_]?key|xxxxx|<[a-z_]+>)'

secret_hit=0
for range in "${ranges[@]}"; do
  # Added / modified files with a forbidden name.
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if printf '%s' "$f" | grep -qE "$FORBIDDEN_FILE_ALLOW"; then continue; fi
    if printf '%s' "$f" | grep -qE "$FORBIDDEN_FILE_RE"; then
      fail "Forbidden file in push: $f"; secret_hit=1
    fi
  done < <(git diff --diff-filter=AM --name-only "$range" 2>/dev/null)

  # Secret-looking values in added lines only ('+' lines of the diff).
  while IFS= read -r line; do
    content=${line#+}
    if printf '%s' "$content" | grep -qEi "$PLACEHOLDER_RE"; then continue; fi
    if printf '%s' "$content" | grep -qE "$SECRET_VALUE_RE"; then
      fail "Secret-looking value in added line: $(printf '%.80s' "$content")"; secret_hit=1
    fi
  done < <(git diff --diff-filter=AM -U0 "$range" 2>/dev/null | grep -E '^\+[^+]')
done

if [ "$secret_hit" -ne 0 ]; then
  fail "Secret scan FAILED — push aborted. Remove the secret / file and rewrite the commit."
  exit 1
fi
ok "Secret scan clean"

# ── 2. Test suite ─────────────────────────────────────────────────────────────
if [ "${SKIP_TESTS:-0}" = "1" ]; then
  info "SKIP_TESTS=1 — skipping the test suite (secret scan only)"
else
  info "Running backend test suite (dgx-portal/run-tests.sh)…"
  if ./dgx-portal/run-tests.sh; then
    ok "Tests passed"
  else
    fail "Tests FAILED — push aborted."
    exit 1
  fi
fi

ok "Pre-push gate GREEN — safe to push"
