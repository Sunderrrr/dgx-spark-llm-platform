#!/bin/bash
# Pose (ou retire) le filtre L2 qui empeche le conteneur OCR d'ouvrir une
# connexion vers le portail. Voir cronos-ocr-restrict.service pour le pourquoi.
#
# Les adresses sont relues A CHAUD : docker les reattribue a chaque recreation
# du conteneur, et une regle epinglee sur une ancienne IP echouerait EN SILENCE
# (elle ne bloquerait plus rien sans que rien ne le signale). C'est pour la meme
# raison que ocr-recreate.sh rappelle ce script apres chaque `docker run`.
set -uo pipefail
ACTION="${1:-add}"
NET=ai-platform_ocr_net

ID=$(docker network inspect "$NET" -f '{{.Id}}' 2>/dev/null) || exit 0
[ -n "$ID" ] || exit 0
BR="br-${ID:0:12}"
SRC=$(docker inspect ocr -f "{{index .NetworkSettings.Networks \"$NET\" \"IPAddress\"}}" 2>/dev/null)
DST=$(docker inspect dgx-portal -f "{{index .NetworkSettings.Networks \"$NET\" \"IPAddress\"}}" 2>/dev/null)

# Purge des regles devenues obsoletes sur ce bridge. Suppression PAR INDEX, en
# partant de la fin : `ebtables -D` exige la specification exacte de la regle,
# or on ne connait plus les IP d'une precedente incarnation du conteneur.
# `ebtables -L --Ln` prefixe chaque regle de son index : on lit CE champ, pas un
# numero de ligne (grep -n donnerait le sien, decale par les lignes d'en-tete).
for i in $(ebtables -L FORWARD --Ln 2>/dev/null \
           | awk -v br="$BR" '$0 ~ ("logical-in " br) && /--ip-dport 5000/ {print $1}' \
           | sort -rn); do
  ebtables -D FORWARD "$i" 2>/dev/null || true
done

[ "$ACTION" = del ] && exit 0
[ -n "$SRC" ] && [ -n "$DST" ] || { echo "ocr ou dgx-portal absent de $NET — rien a poser" >&2; exit 0; }

ebtables -A FORWARD --logical-in "$BR" -p IPv4 --ip-src "$SRC" --ip-dst "$DST" \
         --ip-proto tcp --ip-dport 5000 -j DROP
echo "filtre pose : $SRC -> $DST:5000 sur $BR"
