import base64
import email
import email.policy
import json
import socketserver
import ssl
import threading
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
for k in ('ocr', 'voice', 'asr', 'video'):
    for a in ('start', 'stop'):
        ROUTES[f'/runner/{k}/{a}'] = {'ok': True}
ROUTES['/runner/image/start'] = ROUTES['/runner/image/stop'] = {'detail': 'container image missing'}
ROUTES['/litellm/model/info'] = {'data': [{'model_name': 'vllm-test', 'model_info': {'id': 'mid-7', 'db_model': True, 'mode': 'chat'},
                                           'litellm_params': {'model': 'openai/vllm-test'}}]}
ROUTES['/litellm/model/delete'] = {'ok': True}
for w in ('/runner/launch', '/runner/stop', '/runner/ocr/launch', '/runner/voice/launch'):
    ROUTES[w] = {'status': 'starting'}
for w in ('/discord/webhook', '/litellm/user/update', '/litellm/user/new', '/litellm/user/delete', '/litellm/key/update'):
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
        if self.command != 'GET' or not p.endswith(('/status', '/health', '/model-info', '/models', '/logs', '/system_stats')):
            CALLS.append([self.command, self.path, body])
        if p == '/litellm/key/delete':
            ok = not any('fail' in k for k in (body or {}).get('keys', []))
            b = json.dumps({'deleted_keys': body.get('keys')} if ok else {'error': 'no'}).encode()
            self.send_response(200 if ok else 400); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            return
        if p == '/litellm/user/update' and 'fail' in str((body or {}).get('user_id', '')):
            self.send_response(500); self.end_headers()
            return
        if p == '/litellm/model/new' and 'fail' in str((body or {}).get('model_name', '')):
            self.send_response(400); self.end_headers()
            return
        if p == '/litellm/model/new':
            b = b'{"ok": true}'; self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            return
        if p == '/runner/models/delete-files':
            hf = str((body or {}).get('hf_model_id', ''))
            code, out = (404, {'absent': True}) if 'absent' in hf else (500, {'error': 'disk busy'}) if 'busy' in hf else (200, {'bytes': 3 * 2**30 + 5})
            b = json.dumps(out).encode(); self.send_response(code); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            return
        if p == '/runner/stream':
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            for line in ('INFO loading weights', '172.19.0.1 - "GET /v1/models HTTP/1.1" 200', 'ready é', '"GET /metrics HTTP/1.1"'):
                self.wfile.write(('data: ' + json.dumps({'line': line}) + '\n\n').encode()); self.wfile.flush()
            return
        if p == '/runner/launch' and 'refuse' in str((body or {}).get('model_name', '')):
            b = b'{"error": "flag not allowed: --x"}'; self.send_response(400); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            return
        if p == '/runner/ocr/launch' and 'fail' in str((body or {}).get('hf_model_id', '')):
            b = b'{"detail": "bad flag --y"}'; self.send_response(500); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            return
        if p.startswith('/runner/image/'):
            b = json.dumps(ROUTES.get(p, {})).encode(); self.send_response(500); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            return
        if p in ROUTES:
            b = json.dumps(ROUTES[p]).encode(); self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self.end_headers()
    do_POST = do_GET
    def log_message(self, *a): pass
class SMTP(socketserver.StreamRequestHandler):
    """A minimal SMTP server with STARTTLS and AUTH: each message received is
    recorded like an HTTP call (sender, recipients, subject, text part).
    The account « fail » is refused, for the failure path."""

    def say(self, line):
        self.wfile.write((line + '\r\n').encode()); self.wfile.flush()

    def handle(self):
        tls, sender, rcpts = False, None, []
        self.say('220 bench-fake ESMTP')
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            cmd = line.split(' ', 1)[0].upper()
            if cmd in ('EHLO', 'HELO'):
                self.say('250-bench-fake'); self.say('250-AUTH PLAIN LOGIN')
                self.say('250 STARTTLS' if not tls else '250 8BITMIME')
            elif cmd == 'STARTTLS':
                self.say('220 ready')
                ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                ctx.load_cert_chain('/certs/server.pem', '/certs/server.key')
                self.connection = ctx.wrap_socket(self.connection, server_side=True)
                self.rfile = self.connection.makefile('rb'); self.wfile = self.connection.makefile('wb')
                tls = True
            elif cmd == 'AUTH':
                parts = line.split()
                if parts[1].upper() == 'PLAIN':
                    blob = parts[2] if len(parts) > 2 else (self.say('334 ') or self.rfile.readline().decode().strip())
                    user = base64.b64decode(blob).split(b'\0')[1].decode()
                else:
                    if len(parts) > 2:              # the username as an initial response
                        user = base64.b64decode(parts[2]).decode()
                    else:
                        self.say('334 VXNlcm5hbWU6'); user = base64.b64decode(self.rfile.readline().strip()).decode()
                    self.say('334 UGFzc3dvcmQ6'); self.rfile.readline()
                self.say('535 no' if user == 'fail' else '235 ok')
            elif cmd == 'MAIL':
                sender, rcpts = line.split(':', 1)[1].strip(), []; self.say('250 ok')
            elif cmd == 'RCPT':
                rcpts.append(line.split(':', 1)[1].strip()); self.say('250 ok')
            elif cmd == 'DATA':
                self.say('354 go'); data = b''
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b'.\r\n', b'.\n', b''):
                        break
                    data += chunk[1:] if chunk.startswith(b'..') else chunk
                msg = email.message_from_bytes(data, policy=email.policy.default)
                text = msg.get_body(('plain',))
                CALLS.append(['SMTP', 'mail', {'from': str(msg['From']), 'to': str(msg['To']), 'envelope': [sender, rcpts],
                                                'subject': str(msg['Subject']), 'text': text.get_content().replace('\r\n', '\n').rstrip('\n') if text else None}])
                self.say('250 queued')
            elif cmd == 'QUIT':
                self.say('221 bye'); return
            else:
                self.say('250 ok')


class TS(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


threading.Thread(target=TS(('0.0.0.0', 2525), SMTP).serve_forever, daemon=True).start()


class S(ThreadingHTTPServer):
    request_queue_size = 256
S(("0.0.0.0", 9000), H).serve_forever()
