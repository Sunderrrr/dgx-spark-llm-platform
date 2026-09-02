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
apt-get install -y ca-certificates curl git iptables python3 python3-pip pipx

# Flask for the host runner (system python3). PEP 668-safe. Version pinnée
# (audit M5) — alignée sur celle dgx-portal/requirements.txt.
apt-get install -y python3-flask 2>/dev/null || pip3 install --break-system-packages "flask==3.1.3"

# ── 2. Docker + compose plugin ──────────────────────────────────────────────
# get.docker.com est le script officiel, mais on ne pipe plus directement dans
# sh : on le télécharge, on vérifie qu'il est signé/sain (taille non vide + pas
# de téléchargement tronqué), puis on l'exécute séparément.
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
  # Version pinnée (audit M5) : alignée sur le runner (0.24.0 constaté en prod).
  # Sur GB10/Blackwell, NVIDIA publie une roue précompilée ; si l'install
  # générique échoue, installe cette roue manuellement.
  pipx install "vllm==0.24.0" \
    || echo "!! vLLM install failed — on GB10/Blackwell you may need NVIDIA's prebuilt wheel; install it manually so /root/.local/bin/vllm exists."
fi

# ── 4. Repository ───────────────────────────────────────────────────────────
if [ -f docker-compose.yml ] && [ -d dgx-portal ]; then
  REPO_DIR="$(pwd)"
else
  log "Cloning repository to $DEFAULT_DIR"
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

# ── 7. systemd units (paths patched to the real repo dir) ───────────────────
log "Installing systemd units"
for unit in vllm-runner.service vllm-restrict.service cronos-docker-restrict.service cronos-traefik-boot.service; do
  [ -f "systemd/$unit" ] || continue
  sed "s#/root/ai-platform#${REPO_DIR}#g" "systemd/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now vllm-restrict.service cronos-docker-restrict.service 2>/dev/null || true
systemctl enable --now vllm-runner.service 2>/dev/null || true
# Garde-fou de boot Traefik : activé pour le prochain démarrage, pas lancé
# maintenant (inutile de redémarrer un Traefik déjà sain à l'install).
systemctl enable cronos-traefik-boot.service 2>/dev/null || true

# ── Done ────────────────────────────────────────────────────────────────────
cat <<EOF

$(log "Bootstrap complete")
Next steps:
  1. Edit  $REPO_DIR/.env  and fill the remaining secrets:
       LLDAP_ADMIN_PASSWORD, OIDC_CLIENT_ID/SECRET, AUTHENTIK_LITELLM_*,
       SMTP_*, ADMIN_EMAIL, DISCORD_WEBHOOK_URL
  2. Start the stack:      cd $REPO_DIR && docker compose up -d
  3. Open the portal on :5000  →  Admin  →  launch a model from the catalog.

Firewall note: 4001 (API) is opened to your LAN/VPN and 5000 (portal) to
Traefik only. Adjust systemd/cronos-docker-restrict.service for your network.
EOF
