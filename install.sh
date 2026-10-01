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

# Flask for the host runner (system python3). PEP 668-safe. Version pinnée
# (audit M5) — alignée sur celle dgx-portal/requirements.txt.
apt-get install -y python3-flask 2>/dev/null || pip3 install --break-system-packages "flask==3.1.3"

# ── 2. Docker + compose plugin ──────────────────────────────────────────────
# get.docker.com est le script officiel, mais on ne pipe plus directement dans
# sh : on le télécharge, on vérifie que le téléchargement n'est ni vide ni
# tronqué, puis on l'exécute séparément. La SIGNATURE n'est PAS vérifiée — le
# script n'en publie pas d'exploitable ici — c'est un risque connu et consigné
# dans SECURITY.md (§3, risques acceptés), pas une protection qu'on croit avoir.
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

# ── 6b. /root doit rester TRAVERSABLE par le runner ─────────────────────────
# Le dépôt vit sous /root (0700 par défaut sur Debian/Ubuntu) et l'unité tourne
# en vllmrunner : sans cette ACL, l'interpréteur ne peut même pas OUVRIR
# runner.py, et comme l'unité est en Restart=always elle boucle en crash — sans
# message, puisque l'activation ci-dessous est en `|| true`. On ne relâche que la
# TRAVERSÉE (--x), jamais la lecture du dossier.
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
# TOUTES les unités du dépôt, pas seulement celles du démarrage : les unités de
# restriction réseau et le couple monitor/backup sont décrits dans le README
# comme déployés, et sans eux l'installation est à moitié protégée en silence.
for unit in vllm-runner.service vllm-restrict.service cronos-docker-restrict.service \
             cronos-web-restrict.service cronos-ocr-restrict.service hawser-restrict.service \
             cronos-traefik-boot.service cronos-monitor.service cronos-monitor.timer \
             cronos-backup.service cronos-backup.timer comfyui.service comfyui-relay.service; do
  [ -f "systemd/$unit" ] || continue
  sed "s#/root/ai-platform#${REPO_DIR}#g" "systemd/$unit" > "/etc/systemd/system/$unit"
done

# ── 7b. Wrappers root-owned appelés par le runner via sudo scoped ───────────
# Sans eux, l'admin peut lancer un modèle mais AUCUN sidecar média : runner.py
# les exécute en `sudo`, et les règles sudoers ci-dessous sont les seules à les
# autoriser. Un wrapper absent se manifeste par « command not allowed » au
# premier clic sur « lancer », pas à l'installation.
for s in ocr/ocr-recreate.sh voice/voice-recreate.sh voice-qwen/voice-qwen-recreate.sh \
         asr/asr-recreate.sh music/music-recreate.sh systemd/image-recreate.sh \
         systemd/ocr-restrict.sh; do
  if [ -f "$s" ]; then
    install -o root -g root -m 0755 "$s" "/usr/local/sbin/$(basename "$s")"
  fi
done

# ── 7c. Règles sudo scoped (0440 obligatoire : sudo REFUSE un fichier lisible
#        en écriture par son propriétaire) ──────────────────────────────────
for f in systemd/sudoers.d-*; do
  if [ -f "$f" ]; then
    install -o root -g root -m 0440 "$f" "/etc/sudoers.d/$(basename "$f" | sed 's/^sudoers\.d-//')"
  fi
done
# Une règle invalide casse sudo pour TOUT LE MONDE, y compris la réparation : on
# la valide avant de continuer, si l'outil est là.
if command -v visudo >/dev/null 2>&1; then
  visudo -c >/dev/null || die "une règle sudoers est invalide — voir /etc/sudoers.d/"
fi

# ── 7d. needrestart : ne pas redémarrer vllm-runner à chaque mise à jour ────
# Le fichier n'est PAS une unité systemd (c'est du Perl) : il vit hors de
# systemd/ exprès, pour qu'un `cp systemd/* /etc/systemd/system/` — le réflexe —
# ne dépose pas un fichier inerte là où on croit avoir posé une protection.
if [ -f needrestart/99-vllm-runner.conf ]; then
  install -o root -g root -m 0644 needrestart/99-vllm-runner.conf \
    /etc/needrestart/conf.d/99-vllm-runner.conf
fi

systemctl daemon-reload
systemctl enable --now vllm-restrict.service cronos-docker-restrict.service 2>/dev/null || true
systemctl enable --now vllm-runner.service 2>/dev/null || true
# Isolation réseau : ces trois-là sont des règles iptables, elles doivent
# survivre au reboot (sans elles, web_net retrouve l'accès à l'hôte).
systemctl enable --now cronos-web-restrict.service cronos-ocr-restrict.service 2>/dev/null || true
systemctl enable hawser-restrict.service 2>/dev/null || true
# Surveillance et sauvegarde : les responsables sont des TIMERS, pas les services.
systemctl enable --now cronos-monitor.timer cronos-backup.timer 2>/dev/null || true
# Garde-fou de boot Traefik : activé pour le prochain démarrage, pas lancé
# maintenant (inutile de redémarrer un Traefik déjà sain à l'install).
systemctl enable cronos-traefik-boot.service 2>/dev/null || true
# comfyui et comfyui-relay sont volontairement ON-DEMAND : installés, jamais
# activés (voir l'en-tête de leurs unités).

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
