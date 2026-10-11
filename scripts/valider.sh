#!/usr/bin/env bash
# Former name of scripts/validate.sh, kept so existing hooks and habits work.
exec "$(dirname "${BASH_SOURCE[0]}")/validate.sh" "$@"
