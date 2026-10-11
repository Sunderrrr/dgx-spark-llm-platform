#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/validate.sh — THE single command: « this change broke nothing ».
#
# Four groups, each with its own verdict:
#   1. Code gates          backend tests, frontend tests, typing, lint, i18n,
#                          secret scan (reuses the existing scripts)
#   2. Running service     scripts/smoke.sh against the DEPLOYED service
#   3. User journeys       tests/e2e/: Playwright journeys doing what a user
#                          does (chat, reasoning, image, web search, skills,
#                          conversations, export, admin, API keys, pages,
#                          security) + coverage of every route
#   4. Health & resources  memory, served model, disk, S3 backup
#
# GUARANTEES:
#   - safe to run AT ANY TIME, even while people use the platform: nothing
#     destructive, no restart, no --force-recreate; journeys only use the demo
#     account and clean up after themselves;
#   - the served model is protected: a memory-heavy journey (web search starts
#     a browser, the image tool loads a model) is NOT run when available memory
#     is under VALIDATE_HEAVY_MEM_MIN_GIB — on unified memory, earlyoom would
#     pick the model server (run 20261011-133040 killed it that way);
#   - one cause, one failure: a journey that needs the model waits for it to be
#     back (bounded) instead of failing in cascade, and a journey during which
#     the model went down says so, with the earlyoom lines when there are any;
#   - bounded in time: every check has its own timeout;
#   - a failure speaks: « ✗ <check> » + the tail of its log + the log path;
#   - readable by a human AND a git hook / CI: no TTY needed, colours only on a
#     terminal (NO_COLOR honoured); non-zero exit if ANYTHING failed.
#
# Usage:
#   ./scripts/validate.sh                          # everything
#   VALIDATE_GROUPS=1,2 ./scripts/validate.sh      # only these groups
#   VALIDATE_VERBOSE=1 ./scripts/validate.sh       # show the checks' output
#
# Tunables (environment or .env): VALIDATE_MEM_MIN_GIB (4),
# VALIDATE_HEAVY_MEM_MIN_GIB (8), VALIDATE_MODEL_WAIT_S (420),
# VALIDATE_DISK_MAX_PCT (90). The former French names (VALIDER_*) still work.
#
# Full logs stay in runtime/validation/<timestamp>/.
# ─────────────────────────────────────────────────────────────────────────────
# The outside world (paths, domains, bucket) comes from .env — see
# scripts/config.sh: nothing hard-coded here.
DGX_VERIFIER_DOMAINES=1 . "$(dirname "${BASH_SOURCE[0]}")/config.sh"

set -u
cd "$(dirname "$0")/.."

GROUPS_WANTED=${VALIDATE_GROUPS:-${VALIDER_GROUPES:-1,2,3,4}}
VERBOSE=${VALIDATE_VERBOSE:-${VALIDER_VERBEUX:-0}}
MEM_MIN_GIB=${VALIDATE_MEM_MIN_GIB:-${VALIDER_MEM_MIN_GIB:-4}}
HEAVY_MEM_MIN_GIB=${VALIDATE_HEAVY_MEM_MIN_GIB:-8}
MODEL_WAIT_S=${VALIDATE_MODEL_WAIT_S:-420}
DISK_MAX_PCT=${VALIDATE_DISK_MAX_PCT:-${VALIDER_DISK_MAX_PCT:-90}}
export MEM_MIN_GIB DISK_MAX_PCT

# ── Colours only on a terminal (never in a hook or CI) ──────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RST=$'\033[0m'
else
  RED=""; GREEN=""; YELLOW=""; BOLD=""; RST=""
fi

STAMP=$(date +%Y%m%d-%H%M%S)
LOGDIR="runtime/validation/$STAMP"
mkdir -p "$LOGDIR"
START=$(date +%s)
NUM=0
FAILURES=()
GROUP_OK=0; GROUP_TOTAL=0

group_wanted() { case ",$GROUPS_WANTED," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

log_path() {
  printf '%s/%02d-%s.log' "$LOGDIR" "$NUM" \
    "$(printf '%s' "$1" | tr -cs 'a-zA-Z0-9' '-' | cut -c1-48 | sed 's/-$//')"
}

# A check that is not run still counts — and says why, in its own log.
refuse() {
  # refuse <label> <reason>
  NUM=$((NUM + 1)); GROUP_TOTAL=$((GROUP_TOTAL + 1))
  local file; file=$(log_path "$1")
  printf '%s\n' "$2" >"$file"
  printf '%s·%s %s\n' "$YELLOW" "$RST" "$1"
  printf '  %s✗%s %s — not run: %s\n' "$RED" "$RST" "$1" "$2"
  FAILURES+=("$1 (not run)")
}

# ── Run one check, bounded in time, logged ──────────────────────────────────
run_check() {
  # run_check <label> <timeout_s> <command…>
  local label="$1"; shift; local limit="$1"; shift
  NUM=$((NUM + 1))
  local file; file=$(log_path "$label")
  local t0 t1 rc
  t0=$(date +%s)
  printf '%s·%s %s\n' "$YELLOW" "$RST" "$label"
  if [ "$VERBOSE" = "1" ]; then
    timeout --foreground "$limit" "$@" 2>&1 | tee "$file"
    rc=${PIPESTATUS[0]}
  else
    timeout --foreground "$limit" "$@" >"$file" 2>&1
    rc=$?
  fi
  t1=$(date +%s)
  GROUP_TOTAL=$((GROUP_TOTAL + 1))
  if [ "$rc" -eq 0 ]; then
    GROUP_OK=$((GROUP_OK + 1))
    printf '  %s✓%s %s (%s s)\n' "$GREEN" "$RST" "$label" "$((t1 - t0))"
    return 0
  fi
  if [ "$rc" -eq 124 ]; then
    printf '  %s✗%s %s — timed out (%s s): the check is bounded, see its log\n' \
      "$RED" "$RST" "$label" "$limit"
  else
    printf '  %s✗%s %s — failed (exit %s)\n' "$RED" "$RST" "$label" "$rc"
  fi
  printf '     %s→%s end of log (%s):\n' "$YELLOW" "$RST" "$file"
  tail -n 25 "$file" | sed 's/^/       │ /'
  printf '     full log: %s\n' "$file"
  FAILURES+=("$label")
  return 1
}

close_group() {
  # close_group <title>
  if [ "$GROUP_OK" -eq "$GROUP_TOTAL" ]; then
    printf '%s✓ Group %s — %s (%s/%s checks)%s\n\n' "$GREEN" "$GROUP_NUM" "$1" "$GROUP_OK" "$GROUP_TOTAL" "$RST"
  else
    printf '%s✗ Group %s — %s (%s/%s checks)%s\n\n' "$RED" "$GROUP_NUM" "$1" "$GROUP_OK" "$GROUP_TOTAL" "$RST"
  fi
  GROUP_OK=0; GROUP_TOTAL=0
}

# ── Served model probes (the token is NEVER printed) ────────────────────────
runner_token() {
  grep -E '^RUNNER_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"''
}

# Prints « <status> <pid> », or « unreachable - ».
model_state() {
  local token; token=$(runner_token)
  curl -s -m 10 -H "Authorization: Bearer $token" "$DGX_RUNNER_URL/status" 2>/dev/null \
    | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreachable -"); raise SystemExit
print(d.get("status") or "unknown", d.get("pid") or "-")' 2>/dev/null || echo "unreachable -"
}

mem_available_gib() {
  awk '/^MemAvailable:/ {printf "%.1f", $2/1024/1024}' /proc/meminfo
}

# Waits (bounded) for the served model to be running. 0 = running.
wait_for_model() {
  local deadline=$(( $(date +%s) + MODEL_WAIT_S )) state announced=0
  while :; do
    state=$(model_state)
    [ "${state%% *}" = "running" ] && return 0
    if [ "$announced" = 0 ]; then
      printf '  %s…%s served model is « %s », waiting up to %s s for it to come back\n' \
        "$YELLOW" "$RST" "${state%% *}" "$MODEL_WAIT_S"
      announced=1
    fi
    [ "$(date +%s)" -ge "$deadline" ] && return 1
    sleep 10
  done
}

# Explains a model drop that happened during a journey.
report_model_drop() {
  # report_model_drop <before> <after> <since_epoch>
  printf '  %s⚠%s the served model went down during this journey (status/pid %s → %s)\n' \
    "$RED" "$RST" "$1" "$2"
  if command -v journalctl >/dev/null 2>&1; then
    journalctl -u earlyoom --since "@$3" --no-pager -q 2>/dev/null \
      | grep -E 'SIGTERM|SIGKILL|low memory' | tail -3 | sed 's/^/       │ /'
  fi
}

# ── Health & resources checks (group 4) ─────────────────────────────────────
check_memory() {
  local avail; avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  awk -v k="$avail" -v s="$MEM_MIN_GIB" \
    'BEGIN {printf "available memory: %.1f GiB (floor: %s GiB)\n", k/1024/1024, s; exit !(k > s*1024*1024)}'
}

check_model() {
  # The served model must report « running »: otherwise the platform serves
  # nothing, even if the site answers.
  local state; state=$(model_state)
  echo "runner: status=${state%% *} pid=${state##* }"
  [ "${state%% *}" = "running" ]
}

check_disk() {
  local pct; pct=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
  echo "disk /: ${pct} % used (ceiling: ${DISK_MAX_PCT} %)"
  [ "$pct" -le "$DISK_MAX_PCT" ]
}

check_s3() {
  # The backup must be REACHABLE: a backup that cannot be listed does not
  # exist (dump freshness is watched by the monitor).
  local out
  out=$(timeout 60 s3cmd -c "$DGX_S3_CFG" ls "${DGX_S3_BUCKET}/" 2>&1) || {
    echo "s3cmd failed: $(printf '%s' "$out" | tail -2)"; return 1; }
  if [ -z "$out" ]; then echo "${DGX_S3_BUCKET}/ is empty or unreadable"; return 1; fi
  echo "latest visible backups:"
  printf '%s\n' "$out" | tail -3
}

# ── Header ──────────────────────────────────────────────────────────────────
printf '%s════════════════════════════════════════════════════════════════%s\n' "$BOLD" "$RST"
printf '%s FULL SERVICE VALIDATION — %s%s\n' "$BOLD" "$(date '+%Y-%m-%d %H:%M:%S')" "$RST"
printf '%s════════════════════════════════════════════════════════════════%s\n' "$BOLD" "$RST"
printf 'logs: %s\n' "$LOGDIR"
printf 'groups: %s\n\n' "$GROUPS_WANTED"

# ── Prerequisites (fail fast and clearly) ───────────────────────────────────
for tool in docker node npm python3 timeout s3cmd curl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf '%s✗ missing prerequisite: %s%s\n' "$RED" "$tool" "$RST"
    exit 2
  fi
done
PYTHON_E2E=${PYTHON_E2E:-${DGX_PYTEST_BIN}}
if [ ! -x "$PYTHON_E2E" ]; then
  printf '%s✗ missing prerequisite: %s (Playwright interpreter, DGX_PYTEST_BIN)%s\n' "$RED" "$PYTHON_E2E" "$RST"
  exit 2
fi

# The journeys sign the demo account in ~15 times and (but for j01) never out:
# every run left ~15 live sessions for 12 h. Close the ones THIS run opened, on
# any exit — failure and interruption included.
close_journey_sessions() {
  (cd tests/e2e && "$PYTHON_E2E" close_sessions.py "$START") || true
}
if group_wanted 3; then trap close_journey_sessions EXIT; fi

# ════════════════════════════════════════════════════════════════════════════
# GROUP 1 — Code gates
# ════════════════════════════════════════════════════════════════════════════
if group_wanted 1; then
  GROUP_NUM=1
  printf '%s══ Group 1 — Code gates ══%s\n' "$BOLD" "$RST"
  run_check "1.1 Backend tests (dgx-portal/run-tests.sh — full suite)" 2400 \
    ./dgx-portal/run-tests.sh
  run_check "1.2 Frontend tests (npm test)" 600 \
    bash -c 'cd dgx-portal-frontend && npm test'
  run_check "1.3 Typing (npx tsc --noEmit)" 900 \
    bash -c 'cd dgx-portal-frontend && npx tsc --noEmit'
  run_check "1.4 Lint (npx eslint .)" 900 \
    bash -c 'cd dgx-portal-frontend && npx eslint .'
  run_check "1.5 i18n coverage (scripts/check-i18n.py)" 300 \
    python3 scripts/check-i18n.py
  # The secret scan of pre-push-check.sh, without re-running the backend
  # suite (SKIP_TESTS=1): called, not duplicated.
  run_check "1.6 Secret scan (pre-push-check.sh, SKIP_TESTS=1)" 600 \
    bash -c 'SKIP_TESTS=1 ./scripts/pre-push-check.sh < /dev/null'
  close_group "Code gates"
fi

# ════════════════════════════════════════════════════════════════════════════
# GROUP 2 — Running service
# ════════════════════════════════════════════════════════════════════════════
if group_wanted 2; then
  GROUP_NUM=2
  printf '%s══ Group 2 — Running service ══%s\n' "$BOLD" "$RST"
  run_check "2.1 Deployed service probes (scripts/smoke.sh)" 600 ./scripts/smoke.sh
  close_group "Running service"
fi

# ════════════════════════════════════════════════════════════════════════════
# GROUP 3 — User journeys (tests/e2e/)
# ════════════════════════════════════════════════════════════════════════════
if group_wanted 3; then
  GROUP_NUM=3
  printf '%s══ Group 3 — User journeys (demo account only) ══%s\n' "$BOLD" "$RST"
  # <label>|<script>|<timeout_s>|<flags> — m: needs the served model,
  # h: memory-heavy (starts a browser or loads a model next to the served one).
  JOURNEYS=(
    "3.1 Journey « login » (login, session, SSO)|j01_login|300|"
    "3.2 Journey « chat » (stream, model, tokens/TTFT)|j02_chat|480|m"
    "3.3 Journey « reasoning » (thinking block)|j03_reasoning|480|m"
    "3.4 Journey « skills » (/ menu)|j06_skills|240|"
    "3.5 Journey « conversations » (create/rename/reopen)|j07_conversations|480|m"
    "3.6 Journey « export » (Markdown)|j08_export|420|m"
    "3.7 Journey « web search » (steps + sources)|j05_web_search|600|mh"
    "3.8 Journey « image tool » (real image, on-demand sidecar)|j04_image_tool|660|mh"
    "3.9 Journey « admin » (6 tabs, deep link, filter)|j09_admin|420|"
    "3.10 Journey « API keys » (create, domain usage, revoke)|j10_api_keys|420|m"
    "3.11 Journey « page sweep »|j11_page_sweep|540|"
    "3.12 Journey « page actions » (incl. Support assistant)|j12_page_actions|720|m"
    "3.13 Journey « security » (metrics/authcheck closed)|j13_security|240|"
    "3.14 Route coverage (every route tested or justified)|check_coverage|600|"
  )
  for entry in "${JOURNEYS[@]}"; do
    IFS='|' read -r label name limit flags <<<"$entry"
    if [[ "$flags" == *m* ]] && ! wait_for_model; then
      refuse "$label" "the served model is « $(model_state | cut -d' ' -f1) » after ${MODEL_WAIT_S} s"
      continue
    fi
    if [[ "$flags" == *h* ]]; then
      avail=$(mem_available_gib)
      if awk -v a="$avail" -v m="$HEAVY_MEM_MIN_GIB" 'BEGIN {exit !(a < m)}'; then
        refuse "$label" "${avail} GiB available < ${HEAVY_MEM_MIN_GIB} GiB (VALIDATE_HEAVY_MEM_MIN_GIB) — running it could make earlyoom kill the served model"
        continue
      fi
    fi
    before=$(model_state); since=$(date +%s)
    if [ "$name" = "check_coverage" ]; then
      run_check "$label" "$limit" python3 tests/e2e/check_coverage.py
    else
      run_check "$label" "$limit" "$PYTHON_E2E" "tests/e2e/$name.py"
    fi
    after=$(model_state)
    if [ "${before%% *}" = "running" ] && [ "$after" != "$before" ]; then
      report_model_drop "$before" "$after" "$since"
    fi
  done
  close_group "User journeys"
fi

# ════════════════════════════════════════════════════════════════════════════
# GROUP 4 — Health & resources
# ════════════════════════════════════════════════════════════════════════════
if group_wanted 4; then
  GROUP_NUM=4
  printf '%s══ Group 4 — Health & resources ══%s\n' "$BOLD" "$RST"
  # timeout cannot run shell functions: export them, call them in a subshell.
  export -f check_memory check_model check_disk check_s3 model_state runner_token
  run_check "4.1 Available memory (configurable floor)" 60 bash -c check_memory
  run_check "4.2 Served model is « running »" 60 bash -c check_model
  run_check "4.3 Disk under the ceiling" 60 bash -c check_disk
  run_check "4.4 S3 backup reachable (s3cmd ls ${DGX_S3_BUCKET}/)" 120 bash -c check_s3
  close_group "Health & resources"
fi

# ════════════════════════════════════════════════════════════════════════════
# Overall verdict
# ════════════════════════════════════════════════════════════════════════════
DURATION=$(( $(date +%s) - START ))
printf '%s════════════════════════════════════════════════════════════════%s\n' "$BOLD" "$RST"
if [ ${#FAILURES[@]} -eq 0 ]; then
  printf '%s✓ OVERALL VERDICT: ALL GREEN — the service works normally%s\n' "$GREEN" "$RST"
  printf '  duration: %s s — logs: %s\n' "$DURATION" "$LOGDIR"
  printf '%s════════════════════════════════════════════════════════════════%s\n' "$BOLD" "$RST"
  exit 0
fi
printf '%s✗ OVERALL VERDICT: %s check(s) failed%s\n' "$RED" "${#FAILURES[@]}" "$RST"
for f in "${FAILURES[@]}"; do printf '   %s✗%s %s\n' "$RED" "$RST" "$f"; done
printf '  duration: %s s — logs: %s\n' "$DURATION" "$LOGDIR"
printf '%s════════════════════════════════════════════════════════════════%s\n' "$BOLD" "$RST"
exit 1
