#!/usr/bin/env python3
"""/internal/authcheck compared on the bench: maintenance off, then on, with an
admin's key, a user's key, a blocked admin's key, a key that does not exist,
and no key. Both backends answer from the same database."""
import json
import subprocess
import sys

import bench

ADMIN_KEY = 'sk-bench-admin-1'
ALICE_KEY = 'sk-bench-alice-1'
SEED = ["INSERT INTO api_keys (username, key_alias, key_value, created_at) VALUES ('zz-admin', 'admin', 'sk-bench-admin-1', '2026')"]
MAINT = "INSERT OR REPLACE INTO settings (key, value) VALUES ('maintenance_mode', '1')"
CASES = [
    ('maintenance off', [], None),
    ('maintenance on, no key', [MAINT], None),
    ('maintenance on, admin key', [MAINT], ADMIN_KEY),
    ('maintenance on, user key', [MAINT], ALICE_KEY),
    ('maintenance on, unknown key', [MAINT], 'sk-nope'),
    ('maintenance on, admin key, admin blocked', [MAINT, "INSERT INTO blocked_users (username, reason, blocked_by, blocked_at) VALUES ('zz-admin', NULL, 'x', '2026')"], ADMIN_KEY),
]
bad = 0
for label, sql, key in CASES:
    out = {}
    for side, base in bench.BASES.items():
        bench.inside('restore', {'dbs': list(bench.DBS.values()), 'sql': SEED + sql})
        # Both sides asked from their own container (loopback, a trusted peer):
        # Flask would otherwise see its own loopback and Rust a bench-network peer.
        head = ['-H', f'Authorization: Bearer {key}'] if key else []
        auth = f'Bearer {key}' if key else ''
        if side == 'flask':
            py = ("import sys, urllib.request, urllib.error\n"
                  "h = {'Authorization': sys.argv[1]} if sys.argv[1] else {}\n"
                  "try:\n    r = urllib.request.urlopen(urllib.request.Request('http://localhost:5000/internal/authcheck', headers=h), timeout=20); c, b = r.status, r.read().decode()\n"
                  "except urllib.error.HTTPError as e:\n    c, b = e.code, e.read().decode()\n"
                  "print(b); print(c)")
            raw = subprocess.run(['docker', 'exec', 'bench-flask', 'python3', '-c', py, auth], capture_output=True, text=True).stdout
        else:
            cmd = ['docker', 'exec', 'bench-rs', 'curl', '-s', '-w', '\n%{http_code}', *head, 'http://127.0.0.1:5100/internal/authcheck']
            raw = subprocess.run(cmd, capture_output=True, text=True).stdout
        text, _, code = raw.rstrip('\n').rpartition('\n')
        try:
            body = json.loads(text) if text.strip() else ''
        except ValueError:
            body = text
        out[side] = (int(code), body)
    same = out['flask'] == out['rust']
    bad += not same
    print('OK  ' if same else 'DIFF', label, out['flask'][0], out['flask'][1] if out['flask'][1] else '')
    if not same:
        print('      flask', out['flask'])
        print('      rust ', out['rust'])
print('diffs', bad, 'of', len(CASES))
sys.exit(1 if bad else 0)
