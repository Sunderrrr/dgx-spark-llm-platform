#!/usr/bin/env bash
#
# Cronos — one-shot host bootstrap for the DGX Spark LLM platform.
# Installs system packages (Docker, Python, pipx, vLLM), clones the repo,
# generates .env, installs the systemd units, and prints the next steps.
#
# Usage:
#   sudo ./install.sh                         # from inside a cloned repo
#   curl -fsSL <raw>/install.sh | sudo bash   # standalone (clones the repo)
#
set -euo pipefail

REPO_URL="${CRONOS_REPO:-https://github.com/Sunderrrr/dgx-spark-llm-platform.git}"
DEFAULT_DIR="/root/ai-platform"        # systemd units reference this path
RUNNER_USER="vllmrunner"
RUNNER_HOME="/var/lib/vllm-runner"

log() { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."

# ── 1. System packages ──────────────────────────────────────────────────────
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git iptables acl python3 python3-pip pipx

# Flask for the host runner (system python3). PEP 668-safe. Pinned version
# (audit M5) — aligned with dgx-portal/requirements.txt.
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
systemctl enable --now docker >/dev/null 2>&1 || true

# ── 3. vLLM (host, for the runner) ──────────────────────────────────────────
if [ ! -x /root/.local/bin/vllm ] && ! command -v vllm >/dev/null 2>&1; then
  log "Installing vLLM via pipx (this can take a while)"
  # Pinned version (audit M5): aligned with the runner (0.24.0 observed in prod).
  # On GB10/Blackwell, NVIDIA publishes a prebuilt wheel; if the generic install
  # fails, install that wheel manually.
  pipx install "vllm==0.24.0" \
    || echo "!! vLLM install failed — on GB10/Blackwell you may need NVIDIA's prebuilt wheel; install it manually so /root/.local/bin/vllm exists."
fi

# ── 4. Repository ───────────────────────────────────────────────────────────
if [ -f docker-compose.yml ] && [ -d dgx-portal ]; then
  REPO_DIR="$(pwd)"
else
  log "Cloning repository to $DEFAULT_DIR"
  # `git clone` into an existing non-empty directory only says "destination
  # path already exists" (seen on a bench run over a copied repo, 2026-10-02):
  # say what to do instead. A directory that IS a clone is reused as before.
  if [ -e "$DEFAULT_DIR" ] && [ ! -d "$DEFAULT_DIR/.git" ]; then
    die "$DEFAULT_DIR existe sans être un clone git — déplace-le ou vide-le, puis relance."
  fi
  [ -d "$DEFAULT_DIR/.git" ] || git clone "$REPO_URL" "$DEFAULT_DIR"
  REPO_DIR="$DEFAULT_DIR"
fi
cd "$REPO_DIR"
log "Using repo at $REPO_DIR"

# ── 5. .env (random secrets) ────────────────────────────────────────────────
if [ ! -f .env ]; then
  log "Generating .env with random secrets"
  ./setup.sh || true
fi
chmod 600 .env 2>/dev/null || true

# ── 6. Runner user ──────────────────────────────────────────────────────────
id "$RUNNER_USER" >/dev/null 2>&1 || {
  log "Creating non-root runner user '$RUNNER_USER'"
  useradd -r -m -d "$RUNNER_HOME" -s /usr/sbin/nologin "$RUNNER_USER"
}

# ── 6b. /root must stay TRAVERSABLE by the runner ───────────────────────────
# The repo lives under /root (0700 by default on Debian/Ubuntu) and the unit
# runs as vllmrunner: without this ACL, the interpreter cannot even OPEN
# runner.py, and since the unit has Restart=always it crash-loops — silently,
# because the activation above uses `|| true`. We only loosen TRAVERSAL (--x),
# never reading of the directory.
if command -v setfacl >/dev/null 2>&1; then
  setfacl -m u:"$RUNNER_USER":--x /root
  setfacl -m u:"$RUNNER_USER":--x "$REPO_DIR" 2>/dev/null || true
else
  echo "!! setfacl absent : si $REPO_DIR est sous /root, vllm-runner ne pourra pas"
  echo "   lire runner.py (le service bouclera). Installe 'acl', ou déplace le"
  echo "   dépôt hors de /root et adapte les unités."
fi

# ── 7. systemd units (paths patched to the real repo dir) ───────────────────
log "Installing systemd units"
# ALL the repo's units, not just the startup ones: the network restriction units
# and the monitor/backup pair are described in the README as deployed, and
# without them the install is half-protected in silence.
for unit in vllm-runner.service vllm-restrict.service cronos-docker-restrict.service \
             cronos-web-restrict.service cronos-ocr-restrict.service hawser-restrict.service \
             cronos-traefik-boot.service cronos-monitor.service cronos-monitor.timer \
             cronos-backup.service cronos-backup.timer comfyui.service comfyui-relay.service; do
  [ -f "systemd/$unit" ] || continue
  sed "s#/root/ai-platform#${REPO_DIR}#g" "systemd/$unit" > "/etc/systemd/system/$unit"
done

# ── 7b. Root-owned wrappers called by the runner via scoped sudo ────────────
# Without them, the admin can launch a model but NO media sidecar at all:
# runner.py runs them with `sudo`, and the sudoers rules below are the only ones
# authorizing them. A missing wrapper shows up as « command not allowed » on the
# first click on « lancer », not at install time.
for s in ocr/ocr-recreate.sh voice/voice-recreate.sh voice-qwen/voice-qwen-recreate.sh \
         asr/asr-recreate.sh music/music-recreate.sh systemd/image-recreate.sh \
         systemd/ocr-restrict.sh; do
  if [ -f "$s" ]; then
    install -o root -g root -m 0755 "$s" "/usr/local/sbin/$(basename "$s")"
  fi
done

# ── 7c. Scoped sudo rules (0440 mandatory: sudo REFUSES a file writable by
#        its owner) ──────────────────────────────────────────────────────────
for f in systemd/sudoers.d-*; do
  if [ -f "$f" ]; then
    install -o root -g root -m 0440 "$f" "/etc/sudoers.d/$(basename "$f" | sed 's/^sudoers\.d-//')"
  fi
done
# An invalid rule breaks sudo for EVERYONE, repair included: validate it before
# continuing, if the tool is there.
if command -v visudo >/dev/null 2>&1; then
  visudo -c >/dev/null || die "une règle sudoers est invalide — voir /etc/sudoers.d/"
fi

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

systemctl daemon-reload
systemctl enable --now vllm-restrict.service cronos-docker-restrict.service 2>/dev/null || true
systemctl enable --now vllm-runner.service 2>/dev/null || true
# Network isolation: those three are iptables rules, they must survive a reboot
# (without them, web_net regains access to the host).
systemctl enable --now cronos-web-restrict.service cronos-ocr-restrict.service 2>/dev/null || true
systemctl enable hawser-restrict.service 2>/dev/null || true
# Monitoring and backup: the responsible parties are TIMERS, not the services.
systemctl enable --now cronos-monitor.timer cronos-backup.timer 2>/dev/null || true
# Traefik boot guard: enabled for the next boot, not started now (no point
# restarting an already healthy Traefik at install time).
systemctl enable cronos-traefik-boot.service 2>/dev/null || true
# comfyui and comfyui-relay are deliberately ON-DEMAND: installed, never
# enabled (see the header of their units).

# ── Done ────────────────────────────────────────────────────────────────────
cat <<EOF

$(log "Bootstrap complete")
Next steps:
  1. Edit  $REPO_DIR/.env  and fill the remaining secrets:
       LDAP_BIND_PW, OIDC_CLIENT_ID/SECRET, AUTHENTIK_LITELLM_*,
       SMTP_*, ADMIN_EMAIL, DISCORD_WEBHOOK_URL
  2. Start the stack:      cd $REPO_DIR && docker compose up -d
  3. Open the portal on :5000  →  Admin  →  launch a model from the catalog.

Firewall note: the portal's 5000 (published to container 3000) is open to
Traefik only, and LiteLLM publishes NO port at all — the API is reachable
solely through https://api.cronos.website, which keeps the maintenance
forwardAuth in the path. Adjust systemd/cronos-docker-restrict.service for
your network.
EOF
