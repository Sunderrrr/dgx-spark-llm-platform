import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
ROUTES = {
 '/runner/status': {'status': 'running', 'model': 'qwen-test', 'pid': 42},
 '/runner/logs': {'logs': ['INFO start', '172.19.0.1 - "GET /v1/models HTTP/1.1" 200', 'load model é', '"GET /metrics HTTP/1.1"', 'ready']},
 '/runner/ocr/status': {'status': 'running'}, '/runner/video/status': {'status': 'stopped'}, '/runner/voice/status': {'status': 'running'},
 '/runner/asr/status': {'status': 'running'}, '/runner/image/status': {'status': 'exited'},
 '/runner/ocr/logs': {'logs': ['ocr line 1', 'ocr line 2']}, '/runner/voice/logs': {'logs': []},
 '/engine/v1/models': {'data': [{'id': 'qwen-test'}]},
 '/ocr/v1/models': {'data': [{'id': 'datalab-to/chandra-ocr-2'}]},
 '/voice/api/model-info': {'engine': 'kyutai', 'type': 'kyutai', 'loaded': True},
 '/asr/api/model-info': {'loaded': False, 'error': 'CUDA error: out of memory'},
 '/image/health': {'ready': True}, '/image/model-info': {'model': 'FLUX.2 Klein 4B'},
 '/litellm/user/list': {'users': [{'user_id': 'alice', 'spend': 1234.5, 'max_budget': 2000000}, {'user_id': 'nokeys', 'spend': 1, 'max_budget': None}, {'user_id': 'bob', 'spend': 0, 'max_budget': None}]},
 '/litellm/user/info': {'user_info': {'spend': 10, 'max_budget': 5000, 'budget_reset_at': '2026-10-20T00:00:00'}},
}
# Writes LiteLLM accepts; a key containing "fail" is refused (the failure path).
for w in ('/litellm/user/update', '/litellm/user/new', '/litellm/user/delete', '/litellm/key/update'):
    ROUTES[w] = {'ok': True}
# Every call but the bench's own, for bench.py to compare the two backends' upstream traffic.
CALLS = []


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path).path
        if p == '/__calls':
            b = json.dumps(CALLS).encode(); CALLS.clear()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            return
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode('utf-8', 'replace')
        if self.command != 'GET' or not p.endswith(('/status', '/health', '/model-info', '/models', '/logs')):
            CALLS.append([self.command, self.path, body])
        if p == '/litellm/key/delete':
            ok = not any('fail' in k for k in (body or {}).get('keys', []))
            b = json.dumps({'deleted_keys': body.get('keys')} if ok else {'error': 'no'}).encode()
            self.send_response(200 if ok else 400); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            return
        if p == '/litellm/user/update' and 'fail' in str((body or {}).get('user_id', '')):
            self.send_response(500); self.end_headers()
            return
        if p in ROUTES:
            b = json.dumps(ROUTES[p]).encode(); self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self.end_headers()
    do_POST = do_GET
    def log_message(self, *a): pass
class S(ThreadingHTTPServer):
    request_queue_size = 256
S(("0.0.0.0", 9000), H).serve_forever()
