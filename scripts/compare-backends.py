#!/usr/bin/env python3
"""Comparison bench of the Rust migration: the same requests on two backends.

Replays the GET routes of a migration phase (docs/rust-routes.md) on backend A
(Flask) and backend B (Rust), with ONE session — sessions are server-side, the
same cookie is valid on both — and reports every difference of status code
or JSON body. Fields that change on their own between two calls (clocks,
durations, live gauges) are ignored by name.

  python3 scripts/compare-backends.py --a http://localhost:5000 --b http://localhost:5100 \
      --phase 2 --user demo --password-file /path/to/demo-password

Read-only by construction: only GET routes, and only those without a path
parameter unless `--param name=value` supplies one. Exit status 1 when a
difference is found. A run with A == B checks the bench itself: whatever it
reports then is noise to add to --ignore.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Values that move between two identical calls.
DEFAULT_IGNORE = {'time', 'now', 'ts', 'timestamp', 'generated_at', 'updated_at', 'age', 'age_s', 'uptime',
                  'uptime_s', 'duration_ms', 'latency_ms', 'ttft', 'tps', 'tps_prefill', 'depuis_s',
                  'derniere_s', 'level', 'free_gib', 'libre_gib', 'mem_available', 'cpu', 'gpu', 'load',
                  'cpu_pct', 'ram', 'swap', 'exported_at'}
# GET routes with a side effect (an OAuth round trip starts or ends, a session
# closes): never replayed.
SKIP = re.compile(r'/(callback|link|logout|sso|oauth2-redirect)\b')


def inventory():
    spec = importlib.util.spec_from_file_location('routes_inventory', os.path.join(ROOT, 'scripts', 'routes-inventory.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.routes()


def _session_cookie(headers):
    for value in headers.get_all('Set-Cookie') or []:
        m = re.match(r'session=([^;]+)', value)
        if m:
            return m.group(1)
    return None


def login(base, user, password):
    """A session on backend A; its token is then sent to both.

    The cookie is handled by hand: it carries `Secure`, and a cookie jar
    would refuse to send it back to a plain-HTTP local backend."""
    req = urllib.request.Request(f'{base}/api/csrf', headers={'User-Agent': 'dgx-compare-backends'})
    with urllib.request.urlopen(req, timeout=15) as r:
        csrf = json.load(r).get('token', '')
        cookie = _session_cookie(r.headers)
    data = urllib.parse.urlencode({'username': user, 'password': password}).encode()
    req = urllib.request.Request(f'{base}/login', data=data, headers={
        'X-CSRFToken': csrf, 'Cookie': f'session={cookie}', 'User-Agent': 'dgx-compare-backends',
        'Content-Type': 'application/x-www-form-urlencoded'})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        r = urllib.request.build_opener(NoRedirect).open(req, timeout=15)
        headers = r.headers
    except urllib.error.HTTPError as e:
        if e.code not in (302, 303):
            raise
        headers = e.headers
    cookie = _session_cookie(headers) or cookie     # the token is renewed at login
    if not cookie:
        sys.exit('login failed: no session cookie')
    return cookie


def fetch(base, path, cookie):
    req = urllib.request.Request(f'{base}{path}', headers={'Cookie': f'session={cookie}',
                                 'User-Agent': 'dgx-compare-backends', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, ctype, body = r.status, r.headers.get('Content-Type', ''), r.read()
    except urllib.error.HTTPError as e:
        status, ctype, body = e.code, e.headers.get('Content-Type', ''), e.read()
    if 'json' in ctype:
        try:
            return status, json.loads(body)
        except ValueError:
            pass
    # Not JSON (a page, a file): only the type is compared — an HTML page
    # differs on every load (CSP nonces).
    return status, {'_type': ctype.split(';')[0]}


def diff(a, b, ignore, path='$'):
    """Differences between two JSON values, as 'path: A != B' lines."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k in ignore:
                continue
            if k not in a or k not in b:
                out.append(f'{path}.{k}: present only in {"A" if k in a else "B"}')
            else:
                out += diff(a[k], b[k], ignore, f'{path}.{k}')
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f'{path}: {len(a)} items != {len(b)} items']
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, ignore, f'{path}[{i}]')]
    return [] if a == b else [f'{path}: {json.dumps(a)[:80]} != {json.dumps(b)[:80]}']


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--a', required=True, help='backend A (reference), e.g. Flask')
    ap.add_argument('--b', required=True, help='backend B, e.g. the Rust portal')
    ap.add_argument('--phase', type=int, help='only the routes of this migration phase')
    ap.add_argument('--route', action='append', default=[], help='an explicit path (repeatable)')
    ap.add_argument('--param', action='append', default=[], help='name=value for a path parameter')
    ap.add_argument('--ignore', action='append', default=[], help='a JSON key to ignore (repeatable)')
    ap.add_argument('--user')
    ap.add_argument('--password-file')
    ap.add_argument('--session', help='an existing session token instead of --user')
    args = ap.parse_args()

    params = dict(p.split('=', 1) for p in args.param)
    paths = list(args.route)
    if args.phase:
        for r in inventory():
            if r['phase'] != args.phase or 'GET' not in r['methods'].split('/'):
                continue
            path = r['rule']
            if SKIP.search(path):
                continue
            for name in re.findall(r'<(?:\w+:)?(\w+)>', path):
                if name not in params:
                    path = None
                    break
                path = re.sub(rf'<(?:\w+:)?{name}>', params[name], path)
            if path:
                paths.append(path)
    if not paths:
        sys.exit('no route to compare (use --phase or --route)')
    cookie = args.session or login(args.a, args.user, open(args.password_file).read().strip())
    ignore = DEFAULT_IGNORE | set(args.ignore)
    differences = 0
    for path in sorted(set(paths)):
        sa, ja = fetch(args.a, path, cookie)
        sb, jb = fetch(args.b, path, cookie)
        lines = ([f'status {sa} != {sb}'] if sa != sb else []) + diff(ja, jb, ignore)
        if lines:
            # Confirmed on a second pair of calls: a live gauge that moved
            # between A and B is not a difference of implementation.
            sa, ja = fetch(args.a, path, cookie)
            sb, jb = fetch(args.b, path, cookie)
            again = ([f'status {sa} != {sb}'] if sa != sb else []) + diff(ja, jb, ignore)
            lines = [ln for ln in again if ln.split(':')[0] in {x.split(':')[0] for x in lines}]
        mark = '✓' if not lines else '✗'
        print(f'{mark} {path} ({sa})')
        for ln in lines[:8]:
            print(f'    {ln}')
        if len(lines) > 8:
            print(f'    … {len(lines) - 8} more')
        differences += bool(lines)
    print(f'{len(set(paths))} route(s), {differences} with differences')
    return 1 if differences else 0


if __name__ == '__main__':
    sys.exit(main())
