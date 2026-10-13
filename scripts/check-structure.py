#!/usr/bin/env python3
"""One file, one domain: no source file grows into a monolith again.

app.py once held every route of the portal (5 000+ lines) and admin_routes.py
grew to 2 273 lines mixing ten domains before it was split by domain
(2026-10-13). The rule enforced here: a source file stays under MAX_LINES.
The few files still above it are FROZEN at their current size (a ratchet):
they may shrink, never grow — new code goes into the module of its domain,
not into the biggest file around.

When a frozen file shrinks, lower its ceiling here (the check says so), so
the ratchet keeps the gain.

Usage: python3 scripts/check-structure.py
"""
import subprocess
import sys

MAX_LINES = 1200

# Files above MAX_LINES on 2026-10-13, frozen at their size that day.
FROZEN = {
    'dgx-portal-frontend/app/(app)/playground/PlaygroundPage.tsx': 2593,
    'dgx-portal/chat_routes.py': 1680,
    'dgx-portal/app.py': 1722,
    'vllm-runner/runner.py': 1492,
}

SOURCE_EXT = ('.py', '.ts', '.tsx', '.js', '.mjs', '.sh')
# Data and test files are not modules: a dictionary or a test suite is long
# by nature, and splitting it buys nothing.
EXEMPT_PARTS = ('/tests/', 'tests/', 'lib/i18n.tsx', 'node_modules/')


def main():
    files = subprocess.run(['git', 'ls-files'], capture_output=True, text=True,
                           check=True).stdout.split('\n')
    failures, notes = [], []
    for path in files:
        if not path.endswith(SOURCE_EXT) or any(p in path for p in EXEMPT_PARTS):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                n = sum(1 for _ in f)
        except (OSError, UnicodeDecodeError):
            continue
        ceiling = FROZEN.get(path, MAX_LINES)
        if n > ceiling:
            why = (f"frozen at {ceiling}" if path in FROZEN else f"limit {MAX_LINES}")
            failures.append(f"{path}: {n} lines ({why}) — move the new code into the "
                            f"module of its domain instead of growing this file")
        elif path in FROZEN and n < ceiling - 50:
            notes.append(f"{path}: {n} lines, frozen at {ceiling} — lower its ceiling "
                         f"in scripts/check-structure.py to keep the gain")
    for path in FROZEN:
        if path not in files:
            notes.append(f"{path}: no longer exists — remove it from FROZEN")
    for line in notes:
        print(f"· {line}")
    for line in failures:
        print(f"✗ {line}")
    if failures:
        return 1
    print(f"✓ every source file is under {MAX_LINES} lines or within its frozen ceiling")
    return 0


if __name__ == '__main__':
    sys.exit(main())
