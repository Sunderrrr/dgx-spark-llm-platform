#!/usr/bin/env python3
"""The passkey flows compared on the bench: each backend runs the same
software-authenticator scenario (wa_driver.py, inside bench-flask) from the
same restored database; the transcripts (challenges and nonces masked) and
every table of the database must match."""
import json
import subprocess
import sys
from pathlib import Path

import bench

DRIVER = Path(__file__).with_name('wa_driver.py').read_text()
USER = bench.USER
MASK_KEYS = {'challenge', 'nonce'}
NOISY = {'web_sessions', 'login_attempts', 'rate_limits', 'user_sessions', 'audit_log'}
IGNORE_COLS = {'nonce', 'challenge', 'created_at', 'expires_at', 'updated_at', 'id', 'sid', 'auth_at'}


def mask(v):
    if isinstance(v, dict):
        return {k: ('<r>' if k in MASK_KEYS else mask(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [mask(x) for x in v]
    return v


def run(base):
    bench.inside('restore', {'dbs': list(bench.DBS.values()), 'sql': []})
    r = subprocess.run(['docker', 'exec', '-i', 'bench-flask', 'python3', '-', base, USER, 'csrf-user'],
                       input=DRIVER, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f'driver on {base}: {r.stderr[-600:]}')
    steps = [json.loads(line) for line in r.stdout.splitlines() if line.startswith('{')]
    return [{'step': s['step'], 'status': s['status'], 'body': mask(s['body'])} for s in steps]


def dump(side):
    return bench.inside('dump', {side: bench.DBS[side]})[side]


def diff_tables(a, b):
    issues = []
    for t in sorted(set(a) | set(b)):
        if t in NOISY:
            continue
        def rows(d, table=t):
            return sorted(({k: bench.mask(v) for k, v in row.items() if k not in IGNORE_COLS} for row in d.get(table, [])),
                          key=lambda x: json.dumps(x, sort_keys=True, default=str))
        issues += bench.diff(rows(a), rows(b), f'db.{t}')
    return issues


flask_t = run(bench.BASES['flask'])
flask_db = dump('flask')
rust_t = run(bench.BASES['rust'])
rust_db = dump('rust')

issues = []
if len(flask_t) != len(rust_t):
    issues.append(f'{len(flask_t)} steps != {len(rust_t)}')
for f, r in zip(flask_t, rust_t):
    if f['status'] != r['status'] or f['body'] != r['body']:
        issues.append(f"{f['step']}: flask {f['status']} {json.dumps(f['body'])[:160]}\n      rust  {r['status']} {json.dumps(r['body'])[:160]}")
issues += diff_tables(flask_db, rust_db)
for step in flask_t:
    print(f"{step['status']} {step['step']}")
print('OK' if not issues else 'DIFF')
for i in issues[:30]:
    print('  ', i)

# ── A key created by Flask, used by Rust (the production case) ─────────────
# The Flask database after its scenario is copied to the Rust side, and each
# backend then runs the sign-in alone with a fresh assertion (counter 20).
flask_again = run(bench.BASES['flask'])  # restores both databases: the Flask state is rebuilt here
copy = ("import sqlite3; s = sqlite3.connect('/app/data/portal.db'); d = sqlite3.connect('/bench/rs/portal.db'); "
        "s.backup(d); d.close(); s.close()")
subprocess.run(['docker', 'exec', 'bench-flask', 'python3', '-c', copy], check=True)


def login_only(base):
    r = subprocess.run(['docker', 'exec', '-i', 'bench-flask', 'python3', '-', base, USER, 'csrf-user', 'login-only'],
                       input=DRIVER, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f'login-only on {base}: {r.stderr[-400:]}')
    return [json.loads(line) for line in r.stdout.splitlines() if line.startswith('{')]


cross_f = login_only(bench.BASES['flask'])
cross_r = login_only(bench.BASES['rust'])
cross = [(a['step'], a['status'], b['status']) for a, b in zip(cross_f, cross_r)]
for c in cross:
    print(f'cross {c[1]}/{c[2]} {c[0]}')
bad_cross = [c for c in cross if c[1] != c[2]]
print('OK (flask-made key)' if not bad_cross else f'DIFF cross {bad_cross}')
sys.exit(1 if issues or bad_cross else 0)
