#!/usr/bin/env python3
"""Parity bench: the same request to the bench's Flask and Rust backends.

    scripts/parity-bench/bench.py '[{"path": "/api/admin"}, ...]'
    scripts/parity-bench/bench.py cases.json

A case: `path`, `m` (method, GET), `as` (admin | ops | stale | guest | user | anon; admin — the owner; ops: another admin),
`csrf` (the token sent, the session's by default),
`body` (str), `ctype`, `ignore` (JSON keys left out of the comparison).
Status, redirect target and JSON body (else the raw body) must match, and
so must the calls each side made to the fake upstream (fake.py records them;
the probes are left out).

A case that is not a GET is a write: both databases are restored from the
seed first, and after the request every table is compared too (timestamps
masked; `ignore_tables` leaves tables out; `sql` statements run on both
restored databases first, to set up a case; `ignore_columns` leaves
columns out, e.g. a salted password hash). The bench runs the requests from
inside bench-flask, so both backends see the same client and Host.
"""
import json
import re
import subprocess
import sys

ADMIN = 'ADMINTOKEN' * 4 + 'ABC'
USER = 'USERTOKEN' * 4 + 'ABCDEFG'
OPS = 'OPSTOKEN' * 4 + 'ABCDEFGHIJK'    # bob: an admin through his group, not the owner
STALE = 'STALETOKEN' * 4 + 'ABC'        # erin: her session predates her admin right
GUEST = 'GUESTTOKEN' * 4 + 'ABC'        # signed out, with a CSRF token (the login page)
CSRF = {ADMIN: 'csrf-admin', USER: 'csrf-user', OPS: 'csrf-ops', STALE: 'csrf-stale', GUEST: 'csrf-guest'}
BASES = {'flask': 'http://localhost:5000', 'rust': 'http://bench-rs:5100'}
DBS = {'flask': '/app/data/portal.db', 'rust': '/bench/rs/portal.db'}
# Rows that every request touches on its own (session refresh, rate limits).
NOISY_TABLES = {'web_sessions', 'login_attempts', 'rate_limits'}

INSIDE = r'''
import json, sqlite3, sys, urllib.error, urllib.request
op, arg = sys.argv[1], json.loads(sys.argv[2])
if op == 'call':
    m, url, tok, csrf, body, ctype = arg
    h = {'Host': 'portal.test', 'X-Forwarded-Proto': 'https'}
    if tok: h['Cookie'] = 'session=' + tok
    if m != 'GET': h['X-CSRFToken'] = csrf; h['Content-Type'] = ctype
    class NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k): return None
    req = urllib.request.Request(url, data=body.encode() if body is not None else (b'' if m != 'GET' else None), headers=h, method=m)
    try:
        r = urllib.request.build_opener(NR).open(req, timeout=60); st, hd, b = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        st, hd, b = e.code, e.headers, e.read()
    print(json.dumps({'status': st, 'location': hd.get('Location'), 'body': b.decode('utf-8', 'replace')}))
elif op == 'upstream':
    print(urllib.request.urlopen('http://bench-fake:9000/__calls', timeout=10).read().decode())
elif op == 'restore':
    seed = sqlite3.connect('file:/bench/seed.db?mode=ro', uri=True)
    for p in arg['dbs']:
        dst = sqlite3.connect(p, timeout=30); seed.backup(dst)
        for q in arg['sql']:
            dst.execute(q)
        dst.commit(); dst.close()
elif op == 'dump':
    out = {}
    for name, p in arg.items():
        c = sqlite3.connect(p, timeout=30); c.row_factory = sqlite3.Row
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\_sqlx%' ESCAPE '\\'")]
        out[name] = {t: sorted((dict(r) for r in c.execute(f'SELECT * FROM "{t}"')), key=lambda d: json.dumps(d, sort_keys=True, default=str)) for t in tables}
    print(json.dumps(out, default=str))
'''


def inside(op, arg):
    r = subprocess.run(['docker', 'exec', 'bench-flask', 'python3', '-c', INSIDE, op, json.dumps(arg)],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f'bench-flask {op}: {r.stderr.strip()[-400:]}')
    return json.loads(r.stdout) if r.stdout.strip() else None


TS = re.compile(r'\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?')


def mask(v):
    """Timestamps differ by the time between the two requests: mask them."""
    if isinstance(v, str):
        return TS.sub('<ts>', v)
    if isinstance(v, float) and v > 1e9:
        return '<t>'
    if isinstance(v, int) and 1_000_000_000 < v < 10_000_000_000:
        return '<t>'
    return v


def deep_mask(v):
    if isinstance(v, dict):
        return {k: deep_mask(x) for k, x in v.items()}
    if isinstance(v, list):
        return [deep_mask(x) for x in v]
    return mask(v)


def norm(v, ignore):
    if isinstance(v, dict):
        return {k: norm(x, ignore) for k, x in v.items() if k not in ignore}
    if isinstance(v, list):
        return [norm(x, ignore) for x in v]
    return v


def diff(a, b, path='$', out=None):
    out = [] if out is None else out
    if type(a) != type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append(f'{path}: {a!r:.120} != {b!r:.120}')
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f'{path}.{k}: only in {"rust" if k in b else "flask"}')
            else:
                diff(a[k], b[k], f'{path}.{k}', out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f'{path}: len {len(a)} != {len(b)}')
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f'{path}[{i}]', out)
    elif a != b:
        out.append(f'{path}: {a!r:.120} != {b!r:.120}')
    return out


def strip_host(loc):
    return re.sub(r'^https?://[^/]+', '', loc or '')


def compare(case):
    method = case.get('m', 'GET').upper()
    tok = {'admin': ADMIN, 'ops': OPS, 'stale': STALE, 'guest': GUEST, 'user': USER, 'anon': None}[case.get('as', 'admin')]
    write = method != 'GET'
    if write:
        inside('restore', {'dbs': list(DBS.values()), 'sql': case.get('sql', [])})
    res, calls = {}, {}
    for side, base in BASES.items():
        inside('upstream', None)
        res[side] = inside('call', [method, base + case['path'], tok, case.get('csrf', CSRF.get(tok, '')), case.get('body'),
                                    case.get('ctype', 'application/x-www-form-urlencoded')])
        calls[side] = inside('upstream', None)
    f, r = res['flask'], res['rust']
    issues = []
    if f['status'] != r['status']:
        issues.append(f"status {f['status']} != {r['status']}")
    if strip_host(f['location']) != strip_host(r['location']):
        issues.append(f"location {f['location']} != {r['location']}")
    ignore = set(case.get('ignore', []))
    try:
        issues += diff(norm(json.loads(f['body']), ignore), norm(json.loads(r['body']), ignore))
    except ValueError:
        if f['body'] != r['body']:
            issues.append(f"body {f['body'][:100]!r} != {r['body'][:100]!r}")
    # The upstream calls (LiteLLM, runner…) each side made, in order.
    issues += diff(deep_mask(calls['flask']), deep_mask(calls['rust']), 'upstream')
    if write:
        dump = inside('dump', DBS)
        skip = NOISY_TABLES | set(case.get('ignore_tables', []))
        for t in sorted(set(dump['flask']) | set(dump['rust'])):
            if t in skip:
                continue
            cols = set(case.get('ignore_columns', []))
            a = [{k: mask(v) for k, v in row.items() if k not in cols} for row in dump['flask'].get(t, [])]
            b = [{k: mask(v) for k, v in row.items() if k not in cols} for row in dump['rust'].get(t, [])]
            a.sort(key=lambda d: json.dumps(d, sort_keys=True))
            b.sort(key=lambda d: json.dumps(d, sort_keys=True))
            issues += diff(a, b, f'db.{t}')
    who = case.get('as', 'admin')
    print(('OK  ' if not issues else 'DIFF'), method, case['path'], who, f['status'])
    for i in issues[:15]:
        print('     ', i)
    return not issues


if __name__ == '__main__':
    arg = sys.argv[1]
    cases = json.loads(open(arg).read() if not arg.lstrip().startswith('[') else arg)
    bad = sum(not compare(c) for c in cases)
    print('diffs', bad, 'of', len(cases))
    sys.exit(1 if bad else 0)
