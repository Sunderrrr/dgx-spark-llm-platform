#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Closes the sessions the validation opened for the demo account.

Every journey signs the demo account in and only j01 signs out: each run of
validate.sh left ~15 sessions alive for 12 h, and the platform status read
« 238 active sessions » for 2 people. validate.sh calls this script on exit,
failure included, with its start time: only the demo account's sessions opened
DURING that run are revoked (sql_portail refuses any statement that does not
target the demo account).

    fermer_sessions.py <start epoch>
"""
import sys

import commun as c


def main() -> int:
    depuis = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    filtre = f"username='demo' AND revoked=0 AND created_at >= {depuis:.0f}"
    # Two statements (sql_portail commits before it reads, which RETURNING refuses).
    n = int(c.sql_portail(f"SELECT COUNT(*) FROM user_sessions WHERE {filtre}").strip("[](),") or 0)
    c.sql_portail(f"UPDATE user_sessions SET revoked=1 WHERE {filtre}")
    print(f"✓ {n} demo-account session(s) closed (opened by this run)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — a failed cleanup never fails the validation
        print(f"⚠ demo-account sessions not closed: {e}")
        sys.exit(0)
