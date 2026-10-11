#!/usr/bin/env bash
# Installs the DGX git hooks into .git/hooks. Idempotent — run it once per
# clone (the .git dir isn't tracked, so hooks don't travel with the repo).
# Each hook only delegates to the tracked script, so a fix to the script
# applies at once instead of leaving a stale copy in .git/hooks.
set -eu
cd "$(dirname "$0")/.."
install_hook() {
  # install_hook <git hook name> <tracked script>
  local hook=".git/hooks/$1"
  cat > "$hook" <<EOH
#!/usr/bin/env bash
# Auto-installed by scripts/install-git-hooks.sh — delegates to $2.
exec "\$(git rev-parse --show-toplevel)/$2" "\$@"
EOH
  chmod +x "$hook"
  echo "✓ Installed $hook → $2"
}
install_hook pre-commit scripts/pre-commit-check.sh
install_hook pre-push scripts/pre-push-check.sh
