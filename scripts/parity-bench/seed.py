import sqlite3, json, hashlib, time
from werkzeug.security import generate_password_hash
c = sqlite3.connect('/app/data/portal.db')
def ins(table, vals):
    for _, name, typ, notnull, dflt, pk in c.execute(f'pragma table_info({table})').fetchall():
        if notnull and dflt is None and not pk and name not in vals:
            vals[name] = '2026-10-01T00:00:00' if 'at' in name or 'date' in name else ('' if 'TEXT' in typ.upper() or not typ else 0)
    c.execute(f"INSERT INTO {table} ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
now = time.time()
def iso(t): return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.123456'
h = generate_password_hash('bench-pass', method='pbkdf2:sha256:1000')
cols = [r[1] for r in c.execute('pragma table_info(local_users)')]
ins('user_groups', {'name': 'ops', 'max_budget': 5000000, 'is_admin': 1})
ins('user_groups', {'name': 'team', 'max_budget': 1000000, 'is_admin': 0})
for u, adm, grp, en in (('zz-admin', 1, None, 1), ('alice', 0, 'team', 1), ('bob', 0, 'ops', 1), ('carol', 0, None, 0)):
    c.execute("INSERT INTO local_users (username, password_hash, fullname, is_admin, group_name, max_budget, enabled, created_at) VALUES (?,?,?,?,?,?,?,?)",
              (u, h, u.title() + ' Bench', adm, grp, 2000000 if u == 'alice' else None, en, iso(now - 86400 * 3)))
c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('owner_username', 'zz-admin')")
c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('default_key_budget', '300000000')")
c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('platform_name', 'Bench')")
for u, src, adm in (('zz-admin', 'local', 1), ('alice', 'local', 0), ('dave', 'ldap', 0), ('erin', 'ldap,sso', 1)):
    c.execute("INSERT INTO user_sources (username, sources, fullname, last_source, last_seen, last_is_admin) VALUES (?,?,?,?,?,?)", (u, src, u.title(), src.split(',')[-1], iso(now - 3600), adm))
c.execute("INSERT INTO blocked_users (username, reason, blocked_by, blocked_at) VALUES ('dave', 'abus', 'zz-admin', ?)", (iso(now - 7200),))
for i, (u, m, st) in enumerate((('alice', 'Qwen/Qwen3-8B', 'pending'), ('dave', 'meta/Llama-3', 'done'), ('alice', 'x/y', 'rejected'), ('erin', 'a/b', 'pending'))):
    c.execute("INSERT INTO model_requests (username, fullname, model_id, reason, status, created_at) VALUES (?,?,?,?,?,?)", (u, u.title(), m, 'parce que é', st, iso(now - 1000 * (i + 1))))
for i, (u, st) in enumerate((('alice', 'pending'), ('bob', 'approved'))):
    c.execute("INSERT INTO budget_requests (username, fullname, key_alias, current_budget, reason, status, created_at) VALUES (?,?,?,?,?,?,?)", (u, u.title(), '(compte)', 1000000.0, 'plus', st, iso(now - 500 * (i + 1))))
for name, args, eng in (('qwen-test', '--ctx-size 262144 --parallel 4 --n-predict 65536 --mmproj m.gguf', 'llamacpp'), ('vllm-test', '--max-model-len 32768 --enable-auto-tool-choice', 'vllm')):
    ins('model_configs', {'name': name, 'hf_model_id': 'org/' + name, 'vllm_args': args, 'engine': eng})
for t in ('ocr_configs', 'voice_configs'):
    tc = [r[1] for r in c.execute(f'pragma table_info({t})')]
    vals = {k: v for k, v in (('name', t + '-a'), ('hf_model_id', 'org/x'), ('repo_id', 'org/x'), ('vllm_args', ''), ('created_at', iso(now))) if k in tc}
    ins(t, vals)
for i in range(12):
    c.execute("INSERT INTO audit_log (username, action, detail, created_at) VALUES (?,?,?,?)", (['zz-admin', 'alice', 'système'][i % 3], ['login.ok', 'sidecar.start', 'account.password'][i % 3], f'détail {i}', iso(now - 60 * i)))
for i, v in enumerate((1, -1, -1, 1)):
    c.execute("INSERT INTO support_feedback (username, vote, comment, question, answer, model, created_at) VALUES (?,?,?,?,?,?,?)", (['alice', 'bob'][i % 2], v, f'c{i}', 'q', 'a', 'm', iso(now - 30 * i)))
c.execute("INSERT INTO api_keys (username, key_alias, key_value, created_at) VALUES ('alice', 'laptop', 'sk-bench-alice-1', ?)", (iso(now),))
c.execute("INSERT INTO api_keys (username, key_alias, key_value, created_at) VALUES ('alice', 'old', 'sk-bench-alice-fail', ?)", (iso(now),))
c.execute("INSERT INTO api_keys (username, key_alias, key_value, created_at) VALUES ('dave', 'cli', 'sk-bench-dave-1', ?)", (iso(now),))
def session(tok, data):
    c.execute("INSERT INTO web_sessions (token_hash, data, expires_at, updated_at) VALUES (?,?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), json.dumps(data, sort_keys=True, separators=(',', ':')), now + 43200, now))
    c.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, created_at, ip, user_agent) VALUES (?,?,?,?,0,?,?,?)", (data['sid'], data['username'], data['auth_at'], data['auth_at'] + 43200, now, '10.0.0.1', 'bench'))
session('ADMINTOKEN' * 4 + 'ABC', {'auth_at': int(now), 'csrf': 'csrf-admin', 'fullname': 'Zz Admin', 'is_admin': True, 'sid': 'sid-admin', 'sso': False, 'username': 'zz-admin'})
session('USERTOKEN' * 4 + 'ABCDEFG', {'auth_at': int(now), 'csrf': 'csrf-user', 'fullname': 'Alice', 'is_admin': False, 'sid': 'sid-user', 'sso': False, 'username': 'alice'})
session('OPSTOKEN' * 4 + 'ABCDEFGHIJK', {'auth_at': int(now), 'csrf': 'csrf-ops', 'fullname': 'Bob Bench', 'is_admin': True, 'sid': 'sid-ops', 'sso': False, 'username': 'bob'})
# erin's session predates her admin right: the next request refreshes the role.
session('STALETOKEN' * 4 + 'ABC', {'auth_at': int(now), 'csrf': 'csrf-stale', 'fullname': 'Erin', 'is_admin': False, 'sid': 'sid-stale', 'sso': False, 'username': 'erin'})
# dave holds an active temporary grant; « failuser »'s envelope cannot be written (fake.py).
for u in ('dave', 'failuser'):
    c.execute("INSERT INTO budget_requests (username, fullname, key_alias, current_budget, reason, status, created_at) VALUES (?,?,?,?,?,?,?)", (u, u.title(), '(compte)', 1000.0, 'plus', 'pending', iso(now - 100)))
c.execute("INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) VALUES ('dave', 4000.0, 6000.0, ?, ?, ?)", (iso(now + 86400 * 2), iso(now), iso(now)))
c.execute("INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) VALUES ('alice', 1.0, 2.0, '2020-01-01T00:00:00', '2020-01-01', '2020-01-01')")
# Directory memberships: dave (LDAP) in « team », erin (SSO) in « ops » and « Famille ».
ins('user_groups', {'name': 'Famille', 'max_budget': None, 'is_admin': 0, 'sources': 'ldap,sso'})
for u, g, src in (('dave', 'team', 'ldap'), ('erin', 'ops', 'sso'), ('erin', 'Famille', 'sso')):
    c.execute("INSERT INTO user_directory_groups (username, group_name, source, seen_at) VALUES (?,?,?,?)", (u, g, src, iso(now)))
c.execute("UPDATE local_users SET group_name='team' WHERE username='carol'")
c.commit(); print('seeded')
