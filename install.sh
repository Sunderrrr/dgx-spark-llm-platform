#!/usr/bin/env bash
#
# DGX — one-shot host bootstrap for the DGX Spark LLM platform.
# Installs system packages (Docker, Python, pipx, vLLM, earlyoom), clones the
# repo, generates .env, installs the systemd units, checks what it installed,
# and prints the next steps.
#
# Usage:
#   sudo ./install.sh                         # from inside a cloned repo
#   curl -fsSL <raw>/install.sh | sudo bash   # standalone (clones the repo)
#
# Exit status: 0 when every check of the final summary passed, 1 otherwise —
# the summary lists what to fix. Re-running the script is safe.
set -euo pipefail

REPO_URL="${DGX_REPO:-https://github.com/Sunderrrr/dgx-spark-llm-platform.git}"
DEFAULT_DIR="/root/ai-platform"        # systemd units reference this path
RUNNER_USER="vllmrunner"
RUNNER_HOME="/var/lib/vllm-runner"
# Both as set in systemd/vllm-runner.service (VLLM_BIN, HF_HOME).
VLLM_BIN="/root/.local/bin/vllm"
HF_HOME_DIR="/root/.cache/huggingface"
# The subnet the portal's URLs (172.19.0.1:8000/8001/8188, docker-compose.yml)
# and vllm-restrict.service are written for.
DEFAULT_SUBNET="172.19.0.0/16"
DEFAULT_GATEWAY="172.19.0.1"

PROBLEMS=()
log()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; PROBLEMS+=("$*"); }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."

# ── 1. System packages ──────────────────────────────────────────────────────
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# earlyoom is not optional on the GB10: GPU memory is invisible to the kernel
# OOM killer, which then kills everything BUT the culprit and the box freezes.
# earlyoom acts on MemAvailable first (drop-in installed in step 7).
apt-get install -y ca-certificates curl git iptables acl python3 python3-pip pipx earlyoom sudo

# Flask for the host runner (system python3). PEP 668-safe. Pinned version
# (audit M5).
apt-get install -y python3-flask 2>/dev/null || pip3 install --break-system-packages "flask==3.1.3"

# ── 2. Docker + compose plugin ──────────────────────────────────────────────
# get.docker.com is the official script, but we no longer pipe it straight into
# sh: we download it, check the download is neither empty nor truncated, then
# run it separately. The SIGNATURE is NOT verified — the script publishes no
# usable one here — this is a known, recorded risk (SECURITY.md §3, accepted
# risks), not a protection we believe we have.
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker"
  DOCKER_SH="$(mktemp)"
  curl -fsSL https://get.docker.com -o "$DOCKER_SH"
  [ -s "$DOCKER_SH" ] || die "get.docker.com download failed"
  grep -q 'get.docker.com' "$DOCKER_SH" || die "get.docker.com content unexpected"
  sh "$DOCKER_SH"
  rm -f "$DOCKER_SH"
fi
docker compose version >/dev/null 2>&1 || apt-get install -y docker-compose-plugin
systemctl enable --now docker >/dev/null 2>&1 || warn "could not enable/start docker"

# ── 3. vLLM (host, for the runner) ──────────────────────────────────────────
if [ ! -x "$VLLM_BIN" ] && ! command -v vllm >/dev/null 2>&1; then
  log "Installing vLLM via pipx (this can take a while)"
  # Pinned version (audit M5): the one serving in production (0.31.0,
  # 2026-10-14). On GB10/Blackwell, NVIDIA publishes a prebuilt wheel; if the
  # generic install fails, install that wheel so $VLLM_BIN exists.
  pipx install "vllm==0.31.0" \
    || warn "vLLM install failed — on GB10/Blackwell install NVIDIA's prebuilt wheel so $VLLM_BIN exists"
fi

# ── 4. Repository ───────────────────────────────────────────────────────────
if [ -f docker-compose.yml ] && [ -d portal-rs ]; then
  REPO_DIR="$(pwd)"
else
  log "Cloning repository to $DEFAULT_DIR"
  # `git clone` into an existing non-empty directory only says "destination
  # path already exists" (seen on a bench run over a copied repo, 2026-10-02):
  # say what to do instead. A directory that IS a clone is reused as before.
  if [ -e "$DEFAULT_DIR" ] && [ ! -d "$DEFAULT_DIR/.git" ]; then
    die "$DEFAULT_DIR exists but is not a git clone — move or empty it, then re-run."
  fi
  [ -d "$DEFAULT_DIR/.git" ] || git clone "$REPO_URL" "$DEFAULT_DIR"
  REPO_DIR="$DEFAULT_DIR"
fi
cd "$REPO_DIR"
log "Using repo at $REPO_DIR"

# ── 5. .env (random secrets) ────────────────────────────────────────────────
if [ ! -f .env ]; then
  log "Generating .env with random secrets"
  ./setup.sh || warn "setup.sh failed — .env is missing or incomplete; run ./setup.sh and read its error"
fi
chmod 600 .env 2>/dev/null || true

# ── 6. Runner user ──────────────────────────────────────────────────────────
id "$RUNNER_USER" >/dev/null 2>&1 || {
  log "Creating non-root runner user '$RUNNER_USER'"
  useradd -r -m -d "$RUNNER_HOME" -s /usr/sbin/nologin "$RUNNER_USER"
}

# ── 6b. What the runner must reach under /root ──────────────────────────────
# The repo, vLLM and the model cache live under /root (0700 by default on
# Debian/Ubuntu) while the runner runs as $RUNNER_USER. Without these ACLs the
# install LOOKS complete and fails at first use: the unit crash-loops (it
# cannot run the binary), a launch dies on « permission denied » (it cannot
# execute vLLM), a download fails (it cannot write the cache). We grant the
# least each path needs: TRAVERSAL (--x) on the parents, never their listing.
#
# grant_traverse <path>: --x on every ancestor directory of <path> that
# « others » cannot already traverse.
grant_traverse() {
  local d
  d=$(dirname "$1")
  while [ "$d" != "/" ]; do
    if [ "$(stat -c %A "$d" | cut -c10)" != "x" ]; then
      setfacl -m u:"$RUNNER_USER":--x "$d"
    fi
    d=$(dirname "$d")
  done
}
if command -v setfacl >/dev/null 2>&1; then
  # The daemon is the installed binary (/usr/local/bin, traversable by all):
  # it needs no read access to the repo itself.
  # The engine: the vllm link AND the pipx venv it points to.
  if [ -e "$VLLM_BIN" ]; then
    grant_traverse "$VLLM_BIN"
    grant_traverse "$(readlink -f "$VLLM_BIN")"
  fi
  # The model cache: read-write, and inherited by what gets downloaded.
  mkdir -p "$HF_HOME_DIR"
  grant_traverse "$HF_HOME_DIR"
  setfacl -m u:"$RUNNER_USER":rwx -m d:u:"$RUNNER_USER":rwx "$HF_HOME_DIR"
else
  warn "setfacl missing: the runner cannot reach its code, vLLM or the model cache under /root. Install 'acl' and re-run."
fi

# ── 6c. The Docker network the portal's URLs are written for ────────────────
# The portal reaches the host engines at 172.19.0.1 and vllm-restrict.service
# admits 172.19.0.0/16: both assume the stack's default network has THAT
# subnet. Docker picks the next free one — on a host with other networks it
# can be anything, and then nothing is logged: the portal just never finds a
# model. Compose adopts a network created beforehand with its labels (verified,
# Compose v5.2), so a fresh install creates it with the expected subnet. An
# existing network is never touched (recreating it detaches running stacks).
# Compose names the project from COMPOSE_PROJECT_NAME (environment, then .env),
# else from the directory.
PROJECT=${COMPOSE_PROJECT_NAME:-$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' .env 2>/dev/null | tail -1)}
PROJECT=${PROJECT:-$(basename "$REPO_DIR" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}
NET="${PROJECT}_default"
if ! docker network inspect "$NET" >/dev/null 2>&1; then
  docker network create --subnet "$DEFAULT_SUBNET" --gateway "$DEFAULT_GATEWAY" \
    --label com.docker.compose.project="$PROJECT" --label com.docker.compose.network=default \
    "$NET" >/dev/null \
    || warn "could not create $NET on $DEFAULT_SUBNET (subnet taken?) — the portal will not reach the runner; see the subnet note in docker-compose.yml"
else
  sub=$(docker network inspect "$NET" -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}')
  [ "$sub" = "$DEFAULT_SUBNET" ] \
    || warn "$NET has subnet $sub, not $DEFAULT_SUBNET — the portal will not reach the runner; see the subnet note in docker-compose.yml"
fi

# ── 6d. The runner daemon: the Rust binary, built from the repo ─────────────
# systemd/vllm-runner.service runs /usr/local/bin/vllm-runner. Building it needs
# a Rust toolchain on the host (rustup); the portal's own image is built by Docker.
log "Building the runner daemon (vllm-runner-rs)"
command -v cargo >/dev/null 2>&1 || die "cargo not found: install a Rust toolchain on the host (https://rustup.rs), then re-run."
cargo build --release --locked --manifest-path "$REPO_DIR/vllm-runner-rs/Cargo.toml"
install -o root -g root -m 0755 "$REPO_DIR/vllm-runner-rs/target/release/vllm-runner" /usr/local/bin/vllm-runner

# ── 7. systemd units (paths patched to the real repo dir) ───────────────────
log "Installing systemd units"
# ALL the repo's units, not just the startup ones: the network restriction units
# and the monitor/backup pair are described in the README as deployed, and
# without them the install is half-protected in silence.
for unit in vllm-runner.service vllm-restrict.service dgx-docker-restrict.service \
             dgx-web-restrict.service dgx-ocr-restrict.service \
             dgx-traefik-boot.service dgx-monitor.service dgx-monitor.timer \
             dgx-backup.service dgx-backup.timer dgx-logs.service dgx-logs.timer \
             dgx-validation.service dgx-validation.path \
             comfyui.service comfyui-relay.service; do
  [ -f "systemd/$unit" ] || continue
  sed "s#/root/ai-platform#${REPO_DIR}#g" "systemd/$unit" > "/etc/systemd/system/$unit"
done
# The site's firewall sources: a template only, never overwritten.
[ -f /etc/default/dgx-firewall ] || install -m 0644 systemd/dgx-firewall.example /etc/default/dgx-firewall
# earlyoom drop-in (GB10 unified memory: the kernel OOM killer cannot see GPU
# memory, earlyoom acts on MemAvailable first).
if systemctl cat earlyoom.service >/dev/null 2>&1; then
  install -d /etc/systemd/system/earlyoom.service.d
  install -m 0644 systemd/earlyoom.service.d-dgx.conf /etc/systemd/system/earlyoom.service.d/dgx.conf
else
  warn "earlyoom is not installed — the box has no protection against GPU-memory exhaustion"
fi

# ── 7b. Root-owned scripts: wrappers called by the runner via scoped sudo,
#        and helpers of the units ─────────────────────────────────────────────
# Without the wrappers, the admin can launch a model but NO media sidecar and
# cannot delete model files: the runner runs them with `sudo`, and the sudoers
# rules below are the only ones authorizing them. A missing wrapper shows up as
# « command not allowed » on the first click, not at install time.
for s in ocr/ocr-recreate.sh voice/voice-recreate.sh voice-qwen/voice-qwen-recreate.sh \
         voice-kyutai/voice-kyutai-recreate.sh \
         asr/asr-recreate.sh systemd/image-recreate.sh systemd/model-files-rm.sh \
         systemd/ocr-restrict.sh systemd/dgx-docker-restrict.sh; do
  if [ -f "$s" ]; then
    install -o root -g root -m 0755 "$s" "/usr/local/sbin/$(basename "$s")"
  fi
done
# Every wrapper a sudoers rule names must exist, or that feature is dead.
for w in $(grep -ho '/usr/local/sbin/[A-Za-z0-9._-]*' systemd/sudoers.d-* 2>/dev/null | sort -u); do
  [ -x "$w" ] || warn "$w is named by a sudoers rule but is not installed"
done

# ── 7c. Scoped sudo rules (0440 mandatory: sudo REFUSES a file writable by
#        its owner) ──────────────────────────────────────────────────────────
# An invalid rule breaks sudo for EVERYONE, repair included: each file is
# validated BEFORE it is put in place, not after.
for f in systemd/sudoers.d-*; do
  [ -f "$f" ] || continue
  if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$f" >/dev/null; then
    die "invalid sudoers rule $f — nothing was installed from it; fix it and re-run"
  fi
  install -o root -g root -m 0440 "$f" "/etc/sudoers.d/$(basename "$f" | sed 's/^sudoers\.d-//')"
done

# ── 7d. needrestart: do not restart vllm-runner at every update ─────────────
# The file is NOT a systemd unit (it is Perl): it lives outside systemd/ on
# purpose, so that a `cp systemd/* /etc/systemd/system/` — the reflex —
# does not drop an inert file where one thinks a protection was placed.
if [ -f needrestart/99-vllm-runner.conf ]; then
  # `install` does not create parent directories: on a minimal Ubuntu without
  # the needrestart package, /etc/needrestart/conf.d/ was ABSENT and the bare
  # `install` error aborted the script (set -e) BEFORE the systemctl enables
  # below — units on disk, never enabled, no message (measured on a throwaway
  # 24.04 container, 2026-10-02). Create the directory first.
  install -d -o root -g root /etc/needrestart/conf.d
  install -o root -g root -m 0644 needrestart/99-vllm-runner.conf \
    /etc/needrestart/conf.d/99-vllm-runner.conf
fi

# ── 7e. Enable ──────────────────────────────────────────────────────────────
systemctl daemon-reload
# enable_units [--now] <unit>…: a failure is reported, not swallowed.
enable_units() {
  local now=() u
  [ "$1" = "--now" ] && { now=(--now); shift; }
  for u in "$@"; do
    systemctl enable "${now[@]}" "$u" >/dev/null 2>&1 || warn "could not enable ${now[*]} $u (systemctl status $u)"
  done
}
enable_units --now earlyoom.service
systemctl restart earlyoom.service >/dev/null 2>&1 || true   # pick up the drop-in
enable_units --now vllm-restrict.service dgx-docker-restrict.service
enable_units --now vllm-runner.service
# Network isolation: those are iptables rules, they must survive a reboot
# (without them, web_net regains access to the host).
enable_units --now dgx-web-restrict.service dgx-ocr-restrict.service
# Monitoring, backup and log shipping: the responsible parties are TIMERS.
enable_units --now dgx-monitor.timer dgx-backup.timer dgx-logs.timer
# The admin panel's « run the validation » button (request file → path unit).
enable_units --now dgx-validation.path
# Traefik boot guard: enabled for the next boot, not started now (no point
# restarting an already healthy Traefik at install time).
enable_units dgx-traefik-boot.service
# comfyui and comfyui-relay are deliberately ON-DEMAND: installed, never
# enabled (see the header of their units).

# ── 8. Checks: does what was installed actually work? ───────────────────────
log "Checking the installation"
# The runner can run its engine and write its cache, as ITS user.
[ -x /usr/local/bin/vllm-runner ] || warn "the runner daemon /usr/local/bin/vllm-runner is missing"
if [ -e "$VLLM_BIN" ]; then
  sudo -u "$RUNNER_USER" test -x "$(readlink -f "$VLLM_BIN")" || warn "the runner cannot execute $VLLM_BIN as $RUNNER_USER"
fi
sudo -u "$RUNNER_USER" test -w "$HF_HOME_DIR" || warn "the runner cannot write the model cache $HF_HOME_DIR"
failed=$(systemctl list-units --state=failed --plain --no-legend 'dgx-*' 'vllm-*' earlyoom.service 2>/dev/null | awk '{print $1}')
[ -z "$failed" ] || warn "failed units: $(echo "$failed" | tr '\n' ' ')"

# ── Done ────────────────────────────────────────────────────────────────────
if [ "${#PROBLEMS[@]}" -eq 0 ]; then
  log "Bootstrap complete — every check passed"
else
  printf '\n\033[1;31m==> Bootstrap finished with %d problem(s):\033[0m\n' "${#PROBLEMS[@]}"
  for p in "${PROBLEMS[@]}"; do printf '   - %s\n' "$p"; done
fi
cat <<EOF

Next steps:
  1. Edit  $REPO_DIR/.env : set DGX_WEB and DGX_API (docker compose refuses
     to start without them). Nothing else is required there.
  2. Start the stack:      cd $REPO_DIR && docker compose up -d
  3. Open the portal: http://<host>:5000 for a first look, or DGX_WEB behind
     your reverse proxy. The login page creates the OWNER account; it asks for
     the setup token printed in the portal's log:
       docker logs portal-rs 2>&1 | grep setup
  4. Admin → Config: LDAP, SSO and e-mail (all optional). Then Admin → launch
     a model from the catalog.

Exposure (see .env.example, « Host ports »):
  - Web UI: host port 5000 on every interface by default. Behind a reverse
    proxy on the stack's Docker network, bind it to loopback (DGX_WEB_PUBLISH).
  - LiteLLM: loopback only by default; the API is meant to be reached through
    DGX_API, which keeps the maintenance gate in the path (DGX_LITELLM_PUBLISH).
  - Model engine (8000), runner (8001), ComfyUI (8188): local and Docker
    bridge only (vllm-restrict.service).
  - To restrict published ports by source, edit /etc/default/dgx-firewall
    and run: systemctl restart dgx-docker-restrict
EOF
[ "${#PROBLEMS[@]}" -eq 0 ]
