#!/usr/bin/env python3
# HISTORICAL Flask-vs-Rust parity harness: it compares the removed Flask backend
# (dgx-portal/, git history at commit 3d7b8aa) with portal-rs. It cannot run on the
# current tree; kept as a record of the migration (docs/rust-migration.md).
"""SSO sign-in compared on the bench (bench.py cannot: the state and nonce are
random). Each side runs the whole flow from a restored database — /login/sso,
then the provider's answer with a code naming the identity to issue — and the
final status, redirect target and every table are compared."""
import base64
import json
import sys
from urllib.parse import parse_qs, urlparse

import bench

CASES = [
    {'sub': 'sub-erin', 'email': 'erin@bench.test', 'groups': ['ops', 'Famille']},
    {'sub': 'unknown', 'email': 'dave@bench.test', 'name': 'D'},
    {'sub': 'unknown', 'email': 'dave@bench.test', 'email_verified': False},
    {'sub': 'nobody', 'email': 'nobody@bench.test'},
    {'sub': 'sub-erin', 'groups': ['admins']},
    {'sub': 'sub-erin', 'next': '/playground'},
    {'sub': 'sub-erin', 'bad_state': True},
    {'sub': 'sub-erin', 'blocked': True},
]


def run(side, base, case):
    sql = ["INSERT INTO blocked_users (username, reason, blocked_by, blocked_at) VALUES ('erin', NULL, 'x', '2026')"] if case.get('blocked') else []
    bench.inside('restore', {'dbs': list(bench.DBS.values()), 'sql': sql})
    nxt = '?next=' + case['next'] if case.get('next') else ''
    r = bench.inside('call', ['GET', f'{base}/login/sso{nxt}', bench.GUEST, '', None, ''])
    q = parse_qs(urlparse(r['location'] or '').query)
    ask = {k: v for k, v in case.items() if k in ('sub', 'email', 'email_verified', 'groups', 'name')}
    ask['nonce'] = q.get('nonce', [''])[0]
    code = base64.urlsafe_b64encode(json.dumps(ask).encode()).decode().rstrip('=')
    state = 'wrong' if case.get('bad_state') else q.get('state', [''])[0]
    r2 = bench.inside('call', ['GET', f'{base}/api/oauth2-redirect?state={state}&code={code}', bench.GUEST, '', None, ''])
    dump = bench.inside('dump', {side: bench.DBS[side]})[side]
    return r['status'], bench.strip_host(r2['location']), r2['status'], dump


bad = 0
for case in CASES:
    out = {side: run(side, base, case) for side, base in bench.BASES.items()}
    f, r = out['flask'], out['rust']
    issues = []
    if f[:3] != r[:3]:
        issues.append(f'{f[:3]} != {r[:3]}')
    for t in sorted(set(f[3]) | set(r[3])):
        if t in bench.NOISY_TABLES or t == 'user_sessions':
            continue
        a = sorted(({k: bench.mask(v) for k, v in row.items()} for row in f[3].get(t, [])), key=lambda d: json.dumps(d, sort_keys=True))
        b = sorted(({k: bench.mask(v) for k, v in row.items()} for row in r[3].get(t, [])), key=lambda d: json.dumps(d, sort_keys=True))
        issues += bench.diff(a, b, f'db.{t}')
    print('OK  ' if not issues else 'DIFF', json.dumps({k: v for k, v in case.items()}), f[1], f[2])
    for i in issues[:12]:
        print('     ', i)
    bad += bool(issues)
print('diffs', bad, 'of', len(CASES))
sys.exit(1 if bad else 0)
