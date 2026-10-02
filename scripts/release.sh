#!/bin/sh
# Repo versioning logic — a single source of truth, and a single path to
# publish.
#
# Why a script rather than a tag set by hand: the version lives in THREE places
# that can silently diverge — `dgx-portal-frontend/package.json` (and its
# lockfile), the most recent entry of `CHANGELOG.md`, and the git tag. A tag
# that does not match the shipped package breaks nothing right away: it simply
# makes it impossible to tell, six months later, what was running. Here,
# publishing and checking go through the same code.
#
#   scripts/release.sh --check            # package ↔ CHANGELOG consistency (CI)
#   scripts/release.sh --check v0.1.0     # …and the announced tag matches
#   scripts/release.sh --notes v0.1.0     # release body, extracted from the CHANGELOG
#   scripts/release.sh 0.2.0              # publish: version, commit, tag, push
#
# The CHANGELOG is written FIRST: the « ## [0.2.0] » section must exist,
# otherwise the script refuses (and `--notes` would have nothing to publish).
# That is deliberate — release notes written after the fact describe what we
# remember, not what changed.
set -eu
cd "$(dirname "$0")/.."

PKG=dgx-portal-frontend/package.json
LOCK=dgx-portal-frontend/package-lock.json
CHANGELOG=CHANGELOG.md
REPO=https://github.com/Sunderrrr/dgx-spark-llm-platform

die() { echo "✗ $*" >&2; exit 1; }

# Package version (source of truth for what the application embeds).
pkg_version() {
    python3 -c "import json;print(json.load(open('$PKG'))['version'])"
}

# Version of the most RECENT CHANGELOG entry: the first « ## [x.y.z] » line
# (the comparison links at the bottom of the file do not start with « ## [ »).
changelog_version() {
    sed -n 's/^## \[\([0-9][^]]*\)\].*/\1/p' "$CHANGELOG" | head -n 1
}

# Body of a version section, up to the next section (the « ## [...] » lines and
# the trailing link definitions are excluded).
changelog_notes() {
    awk -v v="$1" '
        $0 ~ "^## \\[" v "\\]" { inside = 1; next }
        inside && /^## \[/ { exit }
        inside && /^\[[^]]+\]: / { exit }
        inside { print }
    ' "$CHANGELOG" | sed -e '/./,$!d' -e ':a' -e '/^\n*$/{$d;N;ba' -e '}'
}

[ $# -ge 1 ] || die "usage : scripts/release.sh --check [ref] | --notes <ref> | <version>"

case "$1" in
  --check)
    ref="${2:-}"
    pkg=$(pkg_version)
    new=$(changelog_version)
    [ -n "$new" ] || die "$CHANGELOG n'a aucune section « ## [x.y.z] »"
    echo "  package.json : $pkg"
    echo "  CHANGELOG    : $new (entrée la plus récente)"
    [ "$pkg" = "$new" ] || die "désaccord : le paquet annonce $pkg, le CHANGELOG $new.
  → écrire la section « ## [$pkg] » dans $CHANGELOG (les notes d'abord), ou
    mettre à jour $PKG si la version n'a pas encore été publiée."
    if [ -n "$ref" ]; then
        tag=$(printf '%s' "$ref" | sed 's/^v//')
        echo "  référence    : $ref"
        [ "$tag" = "$pkg" ] || die "le tag $ref ne correspond pas à la version $pkg du paquet"
    fi
    echo "✓ version cohérente ($pkg)"
    ;;

  --notes)
    [ $# -eq 2 ] || die "usage : scripts/release.sh --notes <ref>"
    v=$(printf '%s' "$2" | sed 's/^v//')
    notes=$(changelog_notes "$v")
    [ -n "$notes" ] || die "aucune section « ## [$v] » dans $CHANGELOG"
    printf '%s\n' "$notes"
    printf '\n---\n\nReconstructed from [`%s`](%s/blob/master/%s).\n' "$CHANGELOG" "$REPO" "$CHANGELOG"
    ;;

  *)
    v=$(printf '%s' "$1" | sed 's/^v//')
    printf '%s' "$v" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
        || die "version attendue au format X.Y.Z (reçu : $1)"

    # The tree must be clean: a tag set on a mix of uncommitted changes
    # describes nothing reproducible.
    [ -z "$(git status --porcelain)" ] \
        || die "arbre de travail non propre — commiter ou remiser avant de publier :
$(git status --short)"
    current=$(pkg_version)
    [ "$v" != "$current" ] || die "$v est déjà la version du paquet"
    git rev-parse -q --verify "refs/tags/v$v" >/dev/null \
        && die "le tag v$v existe déjà"

    notes=$(changelog_notes "$v")
    [ -n "$notes" ] || die "aucune section « ## [$v] » dans $CHANGELOG.
  → écrire les notes de version d'abord (elles composent le corps de la release)."

    echo "→ $current → $v"
    python3 - "$PKG" "$LOCK" "$v" <<'PY'
import json, sys
for path, version in ((sys.argv[1], sys.argv[3]), (sys.argv[2], sys.argv[3])):
    data = json.load(open(path, encoding="utf-8"))
    data["version"] = version
    if path.endswith("package-lock.json"):
        # The lockfile repeats the root package version in packages[""]: npm
        # compares both, and a divergence makes `npm ci` fail in CI.
        data["packages"][""]["version"] = version
    open(path, "w", encoding="utf-8").write(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n")
PY
    echo "  $PKG et $LOCK alignés sur $v"

    git add "$PKG" "$LOCK" "$CHANGELOG"
    git commit -q -m "chore(version): publie la version $v"
    # ANNOTATED tag: it carries the notes, so `git show v$v` is enough to know
    # what was shipped, even without access to the GitHub UI.
    printf '%s\n' "$notes" | git tag -a "v$v" -F -
    echo "  commit et tag v$v créés"

    branch=$(git rev-parse --abbrev-ref HEAD)
    echo "→ push de $branch puis du tag (le garde-fou pré-push s'exécute ici)"
    git push origin "$branch"
    git push origin "v$v"
    echo "✓ v$v publiée. La release GitHub est créée par .github/workflows/release.yml"
    ;;
esac
