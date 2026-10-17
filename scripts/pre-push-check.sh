#!/usr/bin/env bash
# Pre-push safety gate for DGX.
#
# Three checks, all must pass before code leaves the box:
#   1. Secret scan — refuses to push a diff that adds a secret file
#      (.env, DEBUG_USERS.txt, private keys…) or a secret-looking value.
#   2. Test suite  — runs the backend tests in a throwaway container.
#   3. i18n coverage — every t("…") string has its English translation, and no
#      screen formats its numbers/dates with a hardcoded locale.
#
# Usage:
#   ./scripts/pre-push-check.sh              # scan @{u}..HEAD, run tests
#   SKIP_TESTS=1 ./scripts/pre-push-check.sh # skip the backend suite
#   SECRETS_ONLY=1 SCAN_TREE=1 ./scripts/pre-push-check.sh
#                                            # secrets only, whole tracked tree
#
# It is also installed as .git/hooks/pre-push (see scripts/install-git-hooks.sh),
# where git feeds it "<local ref> <local sha> <remote ref> <remote sha>" on stdin.
#
# Exit non-zero → push is aborted. Never push around a red gate.
set -u
cd "$(dirname "$0")/.." || exit 2

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

  # COMMIT MESSAGES. The leak of 2026-10-06 was not in a file but in a commit
  # message — this loop did not exist yet. A message is public the moment it is
  # pushed, and rewriting history afterwards does not make readers forget.
  while IFS= read -r msg; do
    [ -z "$msg" ] && continue
    if printf '%s' "$msg" | grep -qEi "$PLACEHOLDER_RE"; then continue; fi
    if printf '%s' "$msg" | grep -qE "$SECRET_VALUE_RE"; then
      fail "Secret-looking value in a COMMIT MESSAGE: $(printf '%.60s' "$msg")"; secret_hit=1
    fi
  done < <(git log --format=%B "$range" 2>/dev/null)

  # The LIVE values of .env must appear in NO diff and NO message: a real
  # credential never looks like a pattern, it looks like a word. Reading the
  # file is local-only and nothing is echoed (the report names the KEY).
  # ADDED lines only (and messages): removing a site value from a tracked file
  # publishes nothing new and must not block the push that removes it.
  if [ -f .env ]; then
    haystack=$(git diff -U0 "$range" 2>/dev/null | grep -E '^\+[^+]'; git log --format=%B "$range" 2>/dev/null)
    while IFS='=' read -r key val; do
      [ -z "$key" ] && continue
      case "$key" in \#*|'') continue ;; esac
      [ "${#val}" -lt 8 ] && continue
      if printf '%s' "$haystack" | grep -qF -- "$val"; then
        fail "A value from .env ($key) is in the push."; secret_hit=1
      fi
    done < .env
  fi

  # Secret-looking values in added lines only ('+' lines of the diff).
  while IFS= read -r line; do
    content=${line#+}
    if printf '%s' "$content" | grep -qEi "$PLACEHOLDER_RE"; then continue; fi
    if printf '%s' "$content" | grep -qE "$SECRET_VALUE_RE"; then
      fail "Secret-looking value in added line: $(printf '%.80s' "$content")"; secret_hit=1
    fi
  done < <(git diff --diff-filter=AM -U0 "$range" 2>/dev/null | grep -E '^\+[^+]')
done

# ── 1b. Whole tracked tree (SCAN_TREE=1, used by validate.sh) ───────────────
# The range scan above only sees what is about to be pushed: run after a push,
# it inspects NOTHING. The tree scan answers « is any secret in the repo right
# now », whatever commit brought it.
if [ "${SCAN_TREE:-0}" = "1" ]; then
  info "Scanning the whole tracked tree…"
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if printf '%s' "$f" | grep -qE "$FORBIDDEN_FILE_ALLOW"; then continue; fi
    if printf '%s' "$f" | grep -qE "$FORBIDDEN_FILE_RE"; then
      fail "Forbidden file tracked: $f"; secret_hit=1
    fi
  done < <(git ls-files)
  while IFS= read -r hit; do
    if printf '%s' "$hit" | grep -qEi "$PLACEHOLDER_RE"; then continue; fi
    fail "Secret-looking value tracked: $(printf '%.80s' "$hit")"; secret_hit=1
  done < <(git grep -I -n -E "$SECRET_VALUE_RE" 2>/dev/null)
  if [ -f .env ]; then
    while IFS='=' read -r key val; do
      [ -z "$key" ] && continue
      case "$key" in \#*|'') continue ;; esac
      val=${val%\"}; val=${val#\"}
      [ "${#val}" -lt 8 ] && continue
      if git grep -I -q -F -- "$val" 2>/dev/null; then
        fail "A value from .env ($key) is in a tracked file."; secret_hit=1
      fi
    done < .env
  fi
fi

if [ "$secret_hit" -ne 0 ]; then
  fail "Secret scan FAILED — push aborted. Remove the secret / file and rewrite the commit."
  exit 1
fi
ok "Secret scan clean"
if [ "${SECRETS_ONLY:-0}" = "1" ]; then
  exit 0
fi

# ── 2. Test suite ─────────────────────────────────────────────────────────────
if [ "${SKIP_TESTS:-0}" = "1" ]; then
  info "SKIP_TESTS=1 — skipping the test suite (secret scan only)"
else
  info "Running backend test suite (cargo test --locked, portal-rs)…"
  if bash -c 'cd portal-rs && CARGO_TARGET_DIR="${PORTAL_RS_TARGET_DIR:-${XDG_CACHE_HOME:-${HOME:-/root}/.cache}/ai-platform/portal-rs-target}" cargo test --locked'; then
    ok "Tests passed"
  else
    fail "Tests FAILED — push aborted."
    exit 1
  fi
fi

# ── 3. i18n coverage ─────────────────────────────────────────────────────────
# A missing translation key breaks NOTHING: the text silently falls back to
# French and the English-speaking user reads French. A local check that takes a
# second beats discovering it in production; it also runs in CI, but better to
# catch it before the push.
info "i18n coverage check (scripts/check-i18n.py)…"
if python3 scripts/check-i18n.py; then
  ok "i18n covered"
else
  fail "Missing translations or hardcoded locale — push aborted."
  exit 1
fi

# ── 4. Frontend unit tests ──────────────────────────────────────────────────
# The playground parsers (parseAsk, recoller, the JSON repair chain) turn model
# output into UI: a silent regression there shows up as raw JSON in a chat
# bubble, which no backend test can see. `node --test` runs the TS tests
# natively (Node >= 23) with zero dependencies.
info "Running frontend unit tests (dgx-portal-frontend: npm test)…"
output=$(cd dgx-portal-frontend && npm test 2>&1); status=$?
if [ "$status" -eq 0 ]; then
  ok "Frontend tests passed"
else
  printf '%s\n' "$output" | tail -25
  fail "Frontend tests FAILED — push aborted."
  exit 1
fi

ok "Pre-push gate GREEN — safe to push"
