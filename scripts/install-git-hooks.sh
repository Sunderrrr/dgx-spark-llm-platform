#!/usr/bin/env bash
# Installs the Cronos git hooks into .git/hooks. Idempotent — run it once per
# clone (the .git dir isn't tracked, so hooks don't travel with the repo).
set -eu
cd "$(dirname "$0")/.."
hook=.git/hooks/pre-push
cat > "$hook" <<'EOF'
#!/usr/bin/env bash
# Auto-installed by scripts/install-git-hooks.sh — runs the pre-push safety gate.
exec "$(git rev-parse --show-toplevel)/scripts/pre-push-check.sh"
EOF
chmod +x "$hook"
echo "✓ Installed $hook → scripts/pre-push-check.sh"
