#!/usr/bin/env python3
"""The media migration compared on the bench: each side starts from the same
local files and bucket (the fake's S3), moves them, and the final status, the
bucket and the local files must match."""
import json
import sys
import time

import bench

S3SQL = ["INSERT OR REPLACE INTO settings (key, value) VALUES ('cfg_s3_endpoint','http://bench-fake:9000/s3'),"
         "('cfg_s3_bucket','b'),('cfg_s3_access_key','a'),('cfg_s3_secret_key','s'),('cfg_s3_region','r')"]
ROOTS = {'flask': '/app/data', 'rust': '/bench/rs'}
SH = r'''
import json, os, sys
root, files = sys.argv[1], json.loads(sys.argv[2])
for kind in ('image_files', 'video_files', 'ocr_images', 'voice_audio'):
    d = os.path.join(root, kind)
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
for path, text in files.items():
    with open(os.path.join(root, path), 'w') as f:
        f.write(text)
for f in ('.media_migration.json',):
    try: os.remove(os.path.join(root, f))
    except OSError: pass
'''
LS = r'''
import json, os, sys
root = sys.argv[1]
out = {}
for kind in ('image_files', 'video_files', 'ocr_images', 'voice_audio'):
    d = os.path.join(root, kind)
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        out[kind + '/' + f] = open(os.path.join(d, f)).read()
st = {}
try: st = json.load(open(os.path.join(root, '.media_migration.json')))
except Exception: pass
print(json.dumps({'files': out, 'status': st}))
'''


def inside_py(code, *args):
    import subprocess
    r = subprocess.run(['docker', 'exec', 'bench-flask', 'python3', '-c', code, *args], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr[-400:])
    return r.stdout


S3CALL = r'''
import json, sys, urllib.request
m, data = sys.argv[1], sys.argv[2]
req = urllib.request.Request('http://bench-fake:9000/__s3', data=data.encode() if m == 'POST' else None, method=m)
print(urllib.request.urlopen(req).read().decode())
'''


def run(side, base, target, local, bucket):
    bench.inside('restore', {'dbs': list(bench.DBS.values()), 'sql': S3SQL})
    inside_py(SH, ROOTS[side], json.dumps(local))
    inside_py(S3CALL, 'POST', json.dumps(bucket))
    r = bench.inside('call', ['POST', base + '/admin/storage/migrate', bench.ADMIN, 'csrf-admin', f'target={target}', 'application/x-www-form-urlencoded'])
    for _ in range(60):
        state = json.loads(inside_py(LS, ROOTS[side]))
        if not state['status'].get('running', True):
            break
        time.sleep(0.5)
    store = json.loads(inside_py(S3CALL, 'GET', ''))
    st = {k: v for k, v in state['status'].items() if k not in ('started', 'finished')}
    return r['status'], st, state['files'], store


CASES = [
    ('s3', {'image_files/a_0.png': 'AAAA', 'voice_audio/v.wav': 'VV'}, {}),
    ('local', {}, {'media/image_files/b_0.png': 'BBB', 'media/ocr_images/o.png': 'O'}),
    ('local', {'image_files/a_0.png': 'keep'}, {}),
]
bad = 0
for target, local, bucket in CASES:
    out = {side: run(side, base, target, local, bucket) for side, base in bench.BASES.items()}
    same = out['flask'] == out['rust']
    bad += not same
    print('OK  ' if same else 'DIFF', target, out['flask'][:2])
    if not same:
        print('      flask', out['flask'])
        print('      rust ', out['rust'])
print('diffs', bad, 'of', len(CASES))
