#!/bin/sh
# Logique de version du dépôt — une seule source de vérité, et un seul chemin
# pour publier.
#
# Pourquoi un script plutôt qu'une étiquette posée à la main : la version vit à
# TROIS endroits qui peuvent diverger en silence — `dgx-portal-frontend/package.json`
# (et son lockfile), l'entrée la plus récente de `CHANGELOG.md`, et le tag git.
# Un tag qui ne correspond pas au paquet livré ne casse rien tout de suite : il
# rend simplement impossible de dire, six mois plus tard, ce qui tournait. Ici,
# publier et vérifier passent par le même code.
#
#   scripts/release.sh --check            # cohérence paquet ↔ CHANGELOG (CI)
#   scripts/release.sh --check v0.1.0     # …et le tag annoncé correspond
#   scripts/release.sh --notes v0.1.0     # corps de release, extrait du CHANGELOG
#   scripts/release.sh 0.2.0              # publie : version, commit, tag, push
#
# Le CHANGELOG s'écrit AVANT : la section « ## [0.2.0] » doit exister, sinon le
# script refuse (et `--notes` n'aurait rien à publier). C'est volontaire — des
# notes de version rédigées après coup décrivent ce dont on se souvient, pas ce
# qui a changé.
set -eu
cd "$(dirname "$0")/.."

PKG=dgx-portal-frontend/package.json
LOCK=dgx-portal-frontend/package-lock.json
CHANGELOG=CHANGELOG.md
REPO=https://github.com/Sunderrrr/dgx-spark-llm-platform

die() { echo "✗ $*" >&2; exit 1; }

# Version du paquet (source de vérité pour ce que l'application embarque).
pkg_version() {
    python3 -c "import json;print(json.load(open('$PKG'))['version'])"
}

# Version de l'entrée la plus RÉCENTE du CHANGELOG : la première ligne
# « ## [x.y.z] » (les liens de comparaison en bas du fichier ne commencent pas
# par « ## [ »).
changelog_version() {
    sed -n 's/^## \[\([0-9][^]]*\)\].*/\1/p' "$CHANGELOG" | head -n 1
}

# Corps d'une section de version, jusqu'à la section suivante (les lignes
# « ## [...] » et les définitions de liens finales sont exclues).
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

    # L'arbre doit être propre : un tag posé sur un mélange de modifications non
    # commitées ne décrit rien de reproductible.
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
        # Le lockfile répète la version du paquet racine dans packages[""] : npm
        # compare les deux, et une divergence fait échouer `npm ci` en CI.
        data["packages"][""]["version"] = version
    open(path, "w", encoding="utf-8").write(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n")
PY
    echo "  $PKG et $LOCK alignés sur $v"

    git add "$PKG" "$LOCK" "$CHANGELOG"
    git commit -q -m "chore(version): publie la version $v"
    # Tag ANNOTÉ : il porte les notes, donc `git show v$v` suffit à savoir ce qui
    # a été livré, même sans accès à l'interface GitHub.
    printf '%s\n' "$notes" | git tag -a "v$v" -F -
    echo "  commit et tag v$v créés"

    branch=$(git rev-parse --abbrev-ref HEAD)
    echo "→ push de $branch puis du tag (le garde-fou pré-push s'exécute ici)"
    git push origin "$branch"
    git push origin "v$v"
    echo "✓ v$v publiée. La release GitHub est créée par .github/workflows/release.yml"
    ;;
esac
