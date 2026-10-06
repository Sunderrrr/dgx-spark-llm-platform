#!/usr/bin/env bash
# Refuses a COMMIT whose message or staged diff looks like it carries a secret.
#
# Why a second gate, on top of `pre-push-check.sh`: the leak of 2026-10-06 was
# in a COMMIT MESSAGE and no hook looked at messages. The push gate catches it
# at the exit — this one catches it at the entrance, where fixing costs one
# `git commit --amend` and not a history rewrite. Installed as `.git/hooks/pre-commit`.
set -u

# Same patterns as the push gate — keep the two in sync.
SECRET_VALUE_RE='(-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9]{24,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})'
PLACEHOLDER_RE='(changeme|change-me|placeholder|example|sk-test|sk-changeme|your[-_]?key|xxxxx|<[a-z_]+>)'
FORBIDDEN_FILE_RE='(^|/)(\.env(\..*)?|DEBUG_USERS\.txt|.*\.pem|.*\.key|.*\.p12|id_rsa[^/]*)$'
FORBIDDEN_FILE_ALLOW='(^|/)\.env\.example$'

hit=0
dire() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }

# 1. The message about to be committed (the exact hole of 2026-10-06).
msg=$(git log -1 --format=%B --no-commit-header 2>/dev/null || cat "$1" 2>/dev/null || true)
if [ -n "${1:-}" ] && [ -f "${1:-}" ]; then msg=$(cat "$1"); fi
if [ -n "$msg" ]; then
  if printf '%s' "$msg" | grep -qE "$SECRET_VALUE_RE" && ! printf '%s' "$msg" | grep -qEi "$PLACEHOLDER_RE"; then
    dire "Ce message de commit contient ce qui ressemble à un secret."
    hit=1
  fi
fi

# 2. A file name that must never enter the repository.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  printf '%s' "$f" | grep -qE "$FORBIDDEN_FILE_ALLOW" && continue
  if printf '%s' "$f" | grep -qE "$FORBIDDEN_FILE_RE"; then
    dire "Fichier interdit dans le commit : $f"; hit=1
  fi
done < <(git diff --cached --name-only --diff-filter=AM 2>/dev/null)

# 3. A secret-looking value in the staged ADDED lines.
while IFS= read -r line; do
  content=${line#+}
  printf '%s' "$content" | grep -qEi "$PLACEHOLDER_RE" && continue
  if printf '%s' "$content" | grep -qE "$SECRET_VALUE_RE"; then
    dire "Valeur qui ressemble à un secret dans une ligne ajoutée : $(printf '%.60s' "$content")"
    hit=1
  fi
done < <(git diff --cached -U0 --diff-filter=AM 2>/dev/null | grep -E '^\+[^+]')

# 4. The LIVE values of .env must never be staged: a credential does not look
#    like a pattern, it looks like a word. Nothing is echoed, only the key name.
if [ -f .env ]; then
  staged=$(git diff --cached 2>/dev/null)
  while IFS='=' read -r cle val; do
    [ -z "$cle" ] && continue
    case "$cle" in \#*) continue ;; esac
    [ "${#val}" -lt 8 ] && continue
    if printf '%s' "$staged" | grep -qF -- "$val"; then
      dire "Une valeur de .env ($cle) est dans le diff."
      hit=1
    fi
  done < .env
fi

if [ "$hit" -ne 0 ]; then
  printf '\n\033[31mCommit refusé. Un secret ne va pas dans git : mets-le dans .env et recommence.\033[0m\n' >&2
  exit 1
fi
exit 0
