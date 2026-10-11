#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Closes the sessions the validation opened for the demo account.

Every journey signs the demo account in and only j01 signs out: each run of
validate.sh left ~15 sessions alive for 12 h, and the platform status read
« 238 active sessions » for 2 people. validate.sh calls this script on exit,
failure included, with its start time: only the demo account's sessions opened
DURING that run are revoked (portal_sql refuses any statement that does not
target the demo account).

    close_sessions.py <start epoch>
"""
import sys

import common as c


def main() -> int:
    since = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    where = f"username='demo' AND revoked=0 AND created_at >= {since:.0f}"
    # Two statements (portal_sql commits before it reads, which RETURNING refuses).
    n = int(c.portal_sql(f"SELECT COUNT(*) FROM user_sessions WHERE {where}").strip("[](),") or 0)
    c.portal_sql(f"UPDATE user_sessions SET revoked=1 WHERE {where}")
    print(f"✓ {n} demo-account session(s) closed (opened by this run)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — a failed cleanup never fails the validation
        print(f"⚠ demo-account sessions not closed: {e}")
        sys.exit(0)
