#!/bin/sh
# Filter host ports published by Docker, by source address (DOCKER-USER).
#
#   dgx-docker-restrict.sh start|stop
#
# The allowed sources are SITE configuration, not repository content: they
# come from DGX_PUBLISHED_ALLOW, read by the unit from /etc/default/dgx-firewall
# (template: systemd/dgx-firewall.example). Format, space-separated:
#
#   DGX_PUBLISHED_ALLOW="3000=172.19.0.0/16,127.0.0.1 8080=10.8.0.0/16"
#
# Each listed port gets a DROP (appended) and an ACCEPT per source (inserted
# above it). A port that is not listed is left as Docker publishes it.
#
# CAREFUL: DOCKER-USER sees the traffic AFTER address translation, so the port
# is the CONTAINER port: the web UI published as 5000->3000 is filtered on
# 3000. A rule on 5000 would never match and protect nothing.
#
# IPv6: docker-proxy also publishes on [::], and that traffic never meets the
# IPv4 chain. The sources are IPv4, so over IPv6 each listed port only keeps
# loopback.
set -u

action=${1:-start}

for entry in ${DGX_PUBLISHED_ALLOW:-}; do
  port=${entry%%=*}
  sources=$(printf '%s' "${entry#*=}" | tr ',' ' ')
  case "$port" in
    ''|*[!0-9]*) echo "dgx-docker-restrict: ignoring malformed entry '$entry'" >&2; continue ;;
  esac
  if [ "$action" = start ]; then
    iptables -C DOCKER-USER -p tcp --dport "$port" -j DROP 2>/dev/null \
      || iptables -A DOCKER-USER -p tcp --dport "$port" -j DROP
    for s in $sources; do
      iptables -C DOCKER-USER -p tcp --dport "$port" -s "$s" -j ACCEPT 2>/dev/null \
        || iptables -I DOCKER-USER -p tcp --dport "$port" -s "$s" -j ACCEPT
    done
    ip6tables -C DOCKER-USER -p tcp --dport "$port" -j DROP 2>/dev/null \
      || ip6tables -A DOCKER-USER -p tcp --dport "$port" -j DROP
    ip6tables -C DOCKER-USER -p tcp --dport "$port" -s ::1/128 -j ACCEPT 2>/dev/null \
      || ip6tables -I DOCKER-USER -p tcp --dport "$port" -s ::1/128 -j ACCEPT
  else
    iptables -D DOCKER-USER -p tcp --dport "$port" -j DROP 2>/dev/null || true
    for s in $sources; do
      iptables -D DOCKER-USER -p tcp --dport "$port" -s "$s" -j ACCEPT 2>/dev/null || true
    done
    ip6tables -D DOCKER-USER -p tcp --dport "$port" -j DROP 2>/dev/null || true
    ip6tables -D DOCKER-USER -p tcp --dport "$port" -s ::1/128 -j ACCEPT 2>/dev/null || true
  fi
done
exit 0
