#!/bin/bash
# Lay down (or remove) the L2 filter that prevents the OCR container from
# opening a connection to the portal. See cronos-ocr-restrict.service for the
# why.
#
# The addresses are re-read AT RUNTIME: docker reassigns them at every container
# recreation, and a rule pinned to an old IP would fail SILENTLY (it would stop
# blocking anything with nothing reporting it). Same reason why ocr-recreate.sh
# calls this script again after every `docker run`.
set -uo pipefail
ACTION="${1:-add}"
NET=ai-platform_ocr_net

ID=$(docker network inspect "$NET" -f '{{.Id}}' 2>/dev/null) || exit 0
[ -n "$ID" ] || exit 0
BR="br-${ID:0:12}"
SRC=$(docker inspect ocr -f "{{index .NetworkSettings.Networks \"$NET\" \"IPAddress\"}}" 2>/dev/null)
DST=$(docker inspect dgx-portal -f "{{index .NetworkSettings.Networks \"$NET\" \"IPAddress\"}}" 2>/dev/null)

# Purge of rules that became obsolete on this bridge. Deletion BY INDEX,
# starting from the end: `ebtables -D` requires the exact rule specification,
# and we no longer know the IPs of a previous incarnation of the container.
# `ebtables -L --Ln` prefixes each rule with its index: we read THAT field, not
# a line number (grep -n would give its own, shifted by the header lines).
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
