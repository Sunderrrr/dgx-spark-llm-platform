#!/bin/bash
# Lay down (or remove) the L2 filter that prevents the OCR container from
# opening a connection to the portal. See dgx-ocr-restrict.service for the
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

# Purge of rules that became obsolete on this bridge. Deletion BY INDEX,
# starting from the end: `ebtables -D` requires the exact rule specification,
# and we no longer know the IPs of a previous incarnation of the container.
# `ebtables -L --Ln` prefixes each rule with its index: we read THAT field, not
# a line number (grep -n would give its own, shifted by the header lines).
for i in $(ebtables -L FORWARD --Ln 2>/dev/null \
           | awk -v br="$BR" '$0 ~ ("logical-in " br) && /--ip-dport 5100/ {print $1}' \
           | sort -rn); do
  ebtables -D FORWARD "$i" 2>/dev/null || true
done

[ "$ACTION" = del ] && exit 0
[ -n "$SRC" ] || { echo "ocr missing from $NET — nothing to set" >&2; exit 1; }

# 5100 is the Rust portal's port (main.rs binds PORTAL_RUST_PORT, 5100 in compose).
# The Flask port 5100 is only the FRONTEND's host publish, and the frontend is not
# on ocr_net: the old rule matched no listener and left the portal reachable from
# arbitrary OCR model code (audit 2026-11-23, finding 4).
#
# The DESTINATION is the bridge itself, not the portal container's IP: that IP is
# reassigned when portal-rs is recreated, and the rule is re-applied only after an
# OCR recreation, so a deploy left the rule matching nothing (audit 2026-11-26).
# Matching the whole ocr_net bridge keeps the containment across every
# recreation: the only other member of that network is the portal.
ebtables -A FORWARD --logical-in "$BR" -p IPv4 --ip-src "$SRC" \
         --ip-proto tcp --ip-dport 5100 -j DROP
echo "filtre pose : $SRC -> :5100 sur $BR"
