#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/valider.sh — LA commande unique : « une modif n'a rien cassé ».
#
# « à chaque modif tu lances une énorme pipeline qui valide que la modification
# n'a rien cassé au fonctionnement normal du service … le plus complète possible »
#
# Quatre groupes, chacun avec son verdict :
#   1. Portes de code          tests backend (724), tests front, typage, lint,
#                              i18n, scan de secrets (réutilise les scripts
#                              existants — aucune duplication)
#   2. Service en fonctionnement   scripts/smoke.sh sur le service DÉPLOYÉ
#   3. Parcours utilisateur    tests/e2e/ : 13 parcours Playwright qui font ce
#                              qu'un utilisateur fait (chat, réflexion, image,
#                              recherche web, compétences, conversations, export,
#                              admin, clés API + API domaine, pages, actions,
#                              sécurité) + la couverture des 158 routes
#   4. Santé & ressources      mémoire, modèle servi, disque, sauvegarde S3
#
# GARANTIES :
#   - sûr à lancer À TOUT MOMENT, même quand quelqu'un utilise la plateforme :
#     aucune action destructive, aucun redémarrage, aucun --force-recreate ;
#     les parcours n'utilisent que le compte démo et nettoient après eux ;
#   - borné en temps : chaque contrôle a son délai, aucun accrochage possible ;
#   - un échec parle : « ✗ <contrôle> » + la fin du journal + le chemin du
#     journal complet ;
#   - lisible par un humain ET par un hook git / la CI : pas de TTY requis,
#     couleurs seulement quand stdout est un terminal (NO_COLOR respecté) ;
#   - code de sortie non nulsi QUOI QUE CE SOIT échoue.
#
# Usage :
#   ./scripts/valider.sh                  # tout
#   VALIDER_GROUPES=1,2 ./scripts/valider.sh   # seulement ces groupes
#   VALIDER_VERBEUX=1 ./scripts/valider.sh     # affiche la sortie des contrôles
#   SMOKE_API=https://127.0.0.1:9 ./scripts/valider.sh   # (démonstration)
#
# Les journaux complets restent dans runtime/validation/<horodatage>/.
# ─────────────────────────────────────────────────────────────────────────────
set -u
cd "$(dirname "$0")/.."

# ── Couleurs seulement sur un terminal (jamais dans un hook ou une CI) ──────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  ROUGE=$'\033[31m'; VERT=$'\033[32m'; JAUNE=$'\033[33m'; GRAS=$'\033[1m'; RST=$'\033[0m'
else
  ROUGE=""; VERT=""; JAUNE=""; GRAS=""; RST=""
fi

HORODATAGE=$(date +%Y%m%d-%H%M%S)
LOGDIR="runtime/validation/$HORODATAGE"
mkdir -p "$LOGDIR"
DEBUT=$(date +%s)
NUM=0
ECHECS=()
GROUPE_OK=0; GROUPE_TOTAL=0

GROUPES_DEMANDES=${VALIDER_GROUPES:-1,2,3,4}
groupe_demande() { case ",$GROUPES_DEMANDES," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

# ── Lancement d'un contrôle, borné en temps, journalisé ─────────────────────
lancer() {
  # lancer <libellé> <délai_s> <commande…>
  local libelle="$1"; shift; local delai="$1"; shift
  NUM=$((NUM + 1))
  local fichier
  fichier=$(printf '%s/%02d-%s.log' "$LOGDIR" "$NUM" \
    "$(printf '%s' "$libelle" | tr -cs 'a-zA-Z0-9' '-' | cut -c1-48 | sed 's/-$//')")
  local t0 t1 rc
  t0=$(date +%s)
  printf '%s·%s %s\n' "$JAUNE" "$RST" "$libelle"
  if [ "${VALIDER_VERBEUX:-0}" = "1" ]; then
    timeout --foreground "$delai" "$@" 2>&1 | tee "$fichier"
    rc=${PIPESTATUS[0]}
  else
    timeout --foreground "$delai" "$@" >"$fichier" 2>&1
    rc=$?
  fi
  t1=$(date +%s)
  GROUPE_TOTAL=$((GROUPE_TOTAL + 1))
  if [ "$rc" -eq 0 ]; then
    GROUPE_OK=$((GROUPE_OK + 1))
    printf '  %s✓%s %s (%s s)\n' "$VERT" "$RST" "$libelle" "$((t1 - t0))"
    return 0
  fi
  if [ "$rc" -eq 124 ]; then
    printf '  %s✗%s %s — délai dépassé (%s s) : le contrôle est borné, voir le journal\n' \
      "$ROUGE" "$RST" "$libelle" "$delai"
  else
    printf '  %s✗%s %s — échec (code %s)\n' "$ROUGE" "$RST" "$libelle" "$rc"
  fi
  printf '     %s→%s fin du journal (%s) :\n' "$JAUNE" "$RST" "$fichier"
  tail -n 25 "$fichier" | sed 's/^/       │ /'
  printf '     journal complet : %s\n' "$fichier"
  ECHECS+=("$libelle")
  return 1
}

conclure_groupe() {
  # conclure_groupe <titre> <ok> <total>
  local titre="$1" ok="$2" total="$3"
  if [ "$ok" -eq "$total" ]; then
    printf '%s✓ Groupe %s — %s (%s/%s contrôles)%s\n\n' "$VERT" "$GROUPE_NUM" "$titre" "$ok" "$total" "$RST"
  else
    printf '%s✗ Groupe %s — %s (%s/%s contrôles)%s\n\n' "$ROUGE" "$GROUPE_NUM" "$titre" "$ok" "$total" "$RST"
  fi
  GROUPE_OK=0; GROUPE_TOTAL=0
}

# ── Contrôles « santé & ressources » (groupe 4) ─────────────────────────────
verifier_memoire() {
  local seuil_gib=${VALIDER_MEM_MIN_GIB:-4}
  local dispo_kb
  dispo_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  local dispo_gib
  dispo_gib=$(awk -v k="$dispo_kb" 'BEGIN {printf "%.1f", k/1024/1024}')
  echo "mémoire disponible : ${dispo_gib} GiB (seuil : ${seuil_gib} GiB)"
  awk -v d="$dispo_kb" -v s="$seuil_gib" 'BEGIN {exit !(d > s*1024*1024)}'
}

verifier_modele() {
  # Le modèle SERVIT doit annoncer « running » — sans quoi la plateforme ne
  # sert rien, même si le site répond. Le jeton n'est JAMAIS affiché : seuls
  # quelques champs choisis sont imprimés.
  local token
  token=$(grep -E '^RUNNER_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"'')
  if [ -z "$token" ]; then echo "RUNNER_TOKEN absent de .env — impossible de sonder le runner"; return 1; fi
  curl -s -m 15 -H "Authorization: Bearer $token" http://127.0.0.1:8001/status \
    | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("réponse du runner illisible :", e); raise SystemExit(1)
print("runner :", {k: d.get(k) for k in ("status", "model", "engine", "pid")})
raise SystemExit(0 if d.get("status") == "running" else 1)'
}

verifier_disque() {
  local max=${VALIDER_DISK_MAX_PCT:-90}
  local pct
  pct=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
  echo "disque / : ${pct} % utilisé (plafond : ${max} %)"
  [ "$pct" -le "$max" ]
}

verifier_s3() {
  # La sauvegarde doit être JOIGNABLE : un backup qu'on ne peut pas lister
  # n'existe pas (la fraîcheur des dumps est surveillée par cronos-monitor).
  local sortie
  sortie=$(timeout 60 s3cmd -c secrets/s3-garage.cfg ls s3://dgx/backups/ 2>&1) || {
    echo "s3cmd a échoué : $(printf '%s' "$sortie" | tail -2)"; return 1; }
  if [ -z "$sortie" ]; then echo "s3://dgx/backups/ est vide ou illisible"; return 1; fi
  echo "dernières sauvegardes visibles :"
  printf '%s\n' "$sortie" | tail -3
}

# ── En-tête ─────────────────────────────────────────────────────────────────
printf '%s════════════════════════════════════════════════════════════════%s\n' "$GRAS" "$RST"
printf '%s VALIDATION COMPLÈTE DU SERVICE — %s%s\n' "$GRAS" "$(date '+%d/%m/%Y %H:%M:%S')" "$RST"
printf '%s════════════════════════════════════════════════════════════════%s\n' "$GRAS" "$RST"
printf 'journaux : %s\n' "$LOGDIR"
printf 'groupes demandés : %s\n\n' "$GROUPES_DEMANDES"

# ── Prérequis (échec rapide et clair) ───────────────────────────────────────
for outil in docker node npm python3 timeout s3cmd curl; do
  if ! command -v "$outil" >/dev/null 2>&1; then
    printf '%s✗ prérequis manquant : %s%s\n' "$ROUGE" "$outil" "$RST"
    exit 2
  fi
done
PYTHON_E2E=${PYTHON_E2E:-/root/shots-venv/bin/python}
if [ ! -x "$PYTHON_E2E" ]; then
  printf '%s✗ prérequis manquant : %s (interpréteur Playwright)%s\n' "$ROUGE" "$PYTHON_E2E" "$RST"
  exit 2
fi

# ════════════════════════════════════════════════════════════════════════════
# GROUPE 1 — Portes de code
# ════════════════════════════════════════════════════════════════════════════
if groupe_demande 1; then
  GROUPE_NUM=1
  printf '%s══ Groupe 1 — Portes de code ══%s\n' "$GRAS" "$RST"
  lancer "1.1 Tests backend (dgx-portal/run-tests.sh — la suite complète)" 2400 \
    ./dgx-portal/run-tests.sh
  lancer "1.2 Tests frontend (npm test)" 600 \
    bash -c 'cd dgx-portal-frontend && npm test'
  lancer "1.3 Typage (npx tsc --noEmit)" 900 \
    bash -c 'cd dgx-portal-frontend && npx tsc --noEmit'
  lancer "1.4 Lint (npx eslint .)" 900 \
    bash -c 'cd dgx-portal-frontend && npx eslint .'
  lancer "1.5 Couverture i18n (scripts/check-i18n.py)" 300 \
    python3 scripts/check-i18n.py
  # Le scan de secrets de pre-push-check.sh, sans relancer la suite backend
  # (SKIP_TESTS=1). Le script garde son comportement intégral par ailleurs
  # (i18n + npm test) : on ne le duplique pas, on l'appelle.
  lancer "1.6 Scan de secrets (pre-push-check.sh, SKIP_TESTS=1)" 600 \
    bash -c 'SKIP_TESTS=1 ./scripts/pre-push-check.sh < /dev/null'
  conclure_groupe "Portes de code" "$GROUPE_OK" "$GROUPE_TOTAL"
fi

# ════════════════════════════════════════════════════════════════════════════
# GROUPE 2 — Service en fonctionnement
# ════════════════════════════════════════════════════════════════════════════
if groupe_demande 2; then
  GROUPE_NUM=2
  printf '%s══ Groupe 2 — Service en fonctionnement ══%s\n' "$GRAS" "$RST"
  lancer "2.1 Sondes du service déployé (scripts/smoke.sh)" 600 \
    ./scripts/smoke.sh
  conclure_groupe "Service en fonctionnement" "$GROUPE_OK" "$GROUPE_TOTAL"
fi

# ════════════════════════════════════════════════════════════════════════════
# GROUPE 3 — Parcours utilisateur (tests/e2e/)
# ════════════════════════════════════════════════════════════════════════════
if groupe_demande 3; then
  GROUPE_NUM=3
  printf '%s══ Groupe 3 — Parcours utilisateur (compte démo uniquement) ══%s\n' "$GRAS" "$RST"
  # <libellé>|<script>|<délai_s> — chaque parcours est borné au-delà de son
  # propre garde-fou interne ; le délai tue proprement (124) un accrochage.
  PARCOURS=(
    "3.1 Parcours « connexion » (login, session, SSO)|j01_connexion|300"
    "3.2 Parcours « chat » (flux, modèle, tokens/TTFT)|j02_chat|480"
    "3.3 Parcours « réflexion » (bloc de pensée)|j03_reflexion|480"
    "3.4 Parcours « compétences » (menu /)|j06_competences|240"
    "3.5 Parcours « conversations » (créer/renommer/rouvrir)|j07_conversations|480"
    "3.6 Parcours « export » (Markdown)|j08_export|420"
    "3.7 Parcours « recherche web » (étapes + sources)|j05_recherche_web|600"
    "3.8 Parcours « outil image » (image réelle, sidecar à la demande)|j04_outil_image|660"
    "3.9 Parcours « admin » (5 onglets, lien profond, filtre)|j09_admin|420"
    "3.10 Parcours « clés API » (créer, usage domaine, révoquer)|j10_cles_api|420"
    "3.11 Parcours « balayage des pages »|j11_balayage_pages|540"
    "3.12 Parcours « actions des pages »|j12_actions_pages|720"
    "3.13 Parcours « sécurité » (metrics/authcheck fermés)|j13_securite|240"
    "3.14 Couverture des routes (chaque route testée ou motivée)|verifier_couverture|600"
  )
  for entree in "${PARCOURS[@]}"; do
    libelle=${entree%%|*}; reste=${entree#*|}
    nom=${reste%%|*}; delai=${entree##*|}
    if [ "$nom" = "verifier_couverture" ]; then
      lancer "$libelle" "$delai" python3 tests/e2e/verifier_couverture.py
    else
      lancer "$libelle" "$delai" "$PYTHON_E2E" "tests/e2e/$nom.py"
    fi
  done
  conclure_groupe "Parcours utilisateur" "$GROUPE_OK" "$GROUPE_TOTAL"
fi

# ════════════════════════════════════════════════════════════════════════════
# GROUPE 4 — Santé & ressources
# ════════════════════════════════════════════════════════════════════════════
if groupe_demande 4; then
  GROUPE_NUM=4
  printf '%s══ Groupe 4 — Santé & ressources ══%s\n' "$GRAS" "$RST"
  # timeout ne lance pas des fonctions shell : on les exporte et on les
  # appelle dans un sous-shell (borné, lui aussi).
  export -f verifier_memoire verifier_modele verifier_disque verifier_s3
  lancer "4.1 Mémoire disponible (seuil configurable)" 60 bash -c verifier_memoire
  lancer "4.2 Modèle servi en état « running »" 60 bash -c verifier_modele
  lancer "4.3 Disque sous le plafond" 60 bash -c verifier_disque
  lancer "4.4 Sauvegarde S3 joignable (s3cmd ls s3://dgx/backups/)" 120 bash -c verifier_s3
  conclure_groupe "Santé & ressources" "$GROUPE_OK" "$GROUPE_TOTAL"
fi

# ════════════════════════════════════════════════════════════════════════════
# Verdict global
# ════════════════════════════════════════════════════════════════════════════
DUREE=$(( $(date +%s) - DEBUT ))
printf '%s════════════════════════════════════════════════════════════════%s\n' "$GRAS" "$RST"
if [ ${#ECHECS[@]} -eq 0 ]; then
  printf '%s✓ VERDICT GLOBAL : TOUT EST VERT — le service fonctionne normalement%s\n' "$VERT" "$RST"
  printf '  durée totale : %s s — journaux : %s\n' "$DUREE" "$LOGDIR"
  printf '%s════════════════════════════════════════════════════════════════%s\n' "$GRAS" "$RST"
  exit 0
fi
printf '%s✗ VERDICT GLOBAL : %s contrôle(s) en échec%s\n' "$ROUGE" "${#ECHECS[@]}" "$RST"
for nom in "${ECHECS[@]}"; do printf '   %s✗%s %s\n' "$ROUGE" "$RST" "$nom"; done
printf '  durée totale : %s s — journaux : %s\n' "$DUREE" "$LOGDIR"
printf '%s════════════════════════════════════════════════════════════════%s\n' "$GRAS" "$RST"
exit 1
