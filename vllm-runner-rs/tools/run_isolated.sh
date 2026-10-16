#!/bin/sh
# Runs a command in an empty network namespace (loopback only): no production
# service can be reached from it. Usage: run_isolated.sh <cmd...>
set -e
exec unshare -n sh -c 'ip link set lo up && exec "$@"' sh "$@"
