"""A software authenticator and the passkey flows, run INSIDE bench-flask
(cbor2 and cryptography come with the Flask image). The host runs it once per
backend, against a restored database, and compares the transcripts.

    python3 - <base-url> <session-token> <csrf> < wa_driver.py

The key is fixed, so both backends see the same public key; the challenges
and nonces are random and are masked by the host. Each step prints one JSON
line: {step, status, body} (body parsed when it is JSON).
"""
import base64
import hashlib
import json
import sys
import urllib.error
import urllib.request

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

BASE, TOKEN, CSRF = sys.argv[1], sys.argv[2], sys.argv[3]
ORIGIN = 'https://example.org'          # the default DGX_WEB both backends read
RP_ID = 'example.org'
PASSWORD = 'bench-pass'
KEY = ec.derive_private_key(0x1F2E3D4C5B6A79880123456789ABCDEF00112233445566778899AABBCCDDEE, ec.SECP256R1())
PUB = KEY.public_key().public_numbers()
ALICE_KEY_A = b'bench-key-A-0123456'
ALICE_KEY_B = b'bench-key-B-0123456'


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def unb64(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def cose_key():
    return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: PUB.x.to_bytes(32, 'big'), -3: PUB.y.to_bytes(32, 'big')})


def rp_hash():
    return hashlib.sha256(RP_ID.encode()).digest()


def client_data(kind, challenge):
    return json.dumps({'type': kind, 'challenge': b64(challenge), 'origin': ORIGIN, 'crossOrigin': False},
                      separators=(',', ':')).encode()


def registration(challenge, cred_id, counter=1):
    auth = rp_hash() + bytes([0x45]) + counter.to_bytes(4, 'big') + bytes(16)
    auth += len(cred_id).to_bytes(2, 'big') + cred_id + cose_key()
    att = cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': auth})
    cd = client_data('webauthn.create', challenge)
    return {'id': b64(cred_id), 'rawId': b64(cred_id), 'type': 'public-key',
            'response': {'clientDataJSON': b64(cd), 'attestationObject': b64(att), 'transports': ['usb', 'nfc']},
            'clientExtensionResults': {}}


def assertion(challenge, cred_id, key=KEY, counter=5):
    auth = rp_hash() + bytes([0x05]) + counter.to_bytes(4, 'big')
    cd = client_data('webauthn.get', challenge)
    sig = key.sign(auth + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
    return {'id': b64(cred_id), 'rawId': b64(cred_id), 'type': 'public-key',
            'response': {'clientDataJSON': b64(cd), 'authenticatorData': b64(auth), 'signature': b64(sig), 'userHandle': None},
            'clientExtensionResults': {}}


def call(method, path, body=None, cookie=None, csrf=CSRF, form=None):
    headers = {'X-CSRFToken': csrf} if method != 'GET' else {}
    if cookie:
        headers['Cookie'] = 'session=' + cookie
    data = None
    if form is not None:
        data = form.encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(req, timeout=60)
        status, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, e.headers, e.read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = raw.decode('utf-8', 'replace')[:80]
    cookies = [c.split(';')[0].split('=', 1)[1] for c in hdrs.get_all('Set-Cookie') or [] if c.startswith('session=')]
    return status, parsed, (cookies[-1] if cookies else None)


def step(name, res):
    status, body, _ = res
    print(json.dumps({'step': name, 'status': status, 'body': body}), flush=True)


MODE = sys.argv[4] if len(sys.argv) > 4 else 'full'
ALICE = TOKEN
if MODE == 'full':
    # ── 1. a passkey added with the password, then a second one ───────────────
    step('begin without password', call('POST', '/api/security/register/begin', {}, ALICE))
    step('begin wrong password', call('POST', '/api/security/register/begin', {'password': 'nope'}, ALICE))
    st, begin_a, _ = call('POST', '/api/security/register/begin', {'password': PASSWORD}, ALICE)
    step('begin', (st, begin_a, None))
    chal_a = unb64(begin_a['publicKey']['challenge'])
    step('finish bad nonce', call('POST', '/api/security/register/finish',
                                 {'credential': registration(chal_a, ALICE_KEY_A), 'nonce': 'nope', 'label': 'x'}, ALICE))
    step('finish key A', call('POST', '/api/security/register/finish',
                             {'credential': registration(chal_a, ALICE_KEY_A), 'nonce': begin_a['nonce'], 'label': 'Ma clé'}, ALICE))
    st, begin_b, _ = call('POST', '/api/security/register/begin', {'password': PASSWORD}, ALICE)
    chal_b = unb64(begin_b['publicKey']['challenge'])
    step('finish key B (wrong challenge)', call('POST', '/api/security/register/finish',
                                               {'credential': registration(b'x' * 64, ALICE_KEY_B), 'nonce': begin_b['nonce']}, ALICE))
    st, begin_b, _ = call('POST', '/api/security/register/begin', {'password': PASSWORD}, ALICE)
    chal_b = unb64(begin_b['publicKey']['challenge'])
    step('finish key B', call('POST', '/api/security/register/finish',
                             {'credential': registration(chal_b, ALICE_KEY_B), 'nonce': begin_b['nonce']}, ALICE))
    step('toggle off', call('POST', '/api/security/toggle', {'enabled': False, 'password': PASSWORD}, ALICE))
    step('toggle on without password', call('POST', '/api/security/toggle', {'enabled': True}, ALICE))
    step('toggle on', call('POST', '/api/security/toggle', {'enabled': True, 'password': PASSWORD}, ALICE))
    step('remove key A, wrong password', call('POST', '/api/security/remove',
                                              {'credential_id': b64(ALICE_KEY_A), 'password': 'nope'}, ALICE))
    step('remove key A', call('POST', '/api/security/remove', {'credential_id': b64(ALICE_KEY_A), 'password': PASSWORD}, ALICE))

# ── 2. the second factor of the sign-in ──────────────────────────────────
_, csrf_body, GUEST = call('GET', '/api/csrf', cookie=None)
GUEST_CSRF = csrf_body['token']
st, login, _ = call('POST', '/login', cookie=GUEST, csrf=GUEST_CSRF, form='username=alice&password=' + PASSWORD)
step('login password', (st, login if st == 200 else None, None))
nonce = login.get('nonce') if isinstance(login, dict) else None
chal = unb64(login['publicKey']['challenge']) if isinstance(login, dict) else b''
step('verify wrong nonce', call('POST', '/api/security/verify-login',
                               {'nonce': 'nope', 'credential': assertion(chal, ALICE_KEY_B)}, GUEST, GUEST_CSRF))
bad_key = ec.generate_private_key(ec.SECP256R1())
step('verify forged signature', call('POST', '/api/security/verify-login',
                                    {'nonce': nonce, 'credential': assertion(chal, ALICE_KEY_B, key=bad_key)}, GUEST, GUEST_CSRF))
st, login, _ = call('POST', '/login', cookie=GUEST, csrf=GUEST_CSRF, form='username=alice&password=' + PASSWORD)
nonce = login['nonce']
chal = unb64(login['publicKey']['challenge'])
good = assertion(chal, ALICE_KEY_B, counter=9 if MODE == 'full' else 20)
st, body, new_token = call('POST', '/api/security/verify-login', {'nonce': nonce, 'credential': good}, GUEST, GUEST_CSRF)
step('verify good assertion', (st, body, None))
who = call('GET', '/api/whoami', cookie=new_token) if new_token else (0, None, None)
step('whoami after the second factor', (who[0], who[1] if isinstance(who[1], dict) and who[1].get('username') else None, None))
# The sign-in replaced the anonymous session: a fresh one for what follows.
st, csrf_body, guest2 = call('GET', '/api/csrf', cookie=None)
guest2 = guest2 or GUEST
GUEST2_CSRF = csrf_body['token']
step('replay the same assertion', call('POST', '/api/security/verify-login', {'nonce': nonce, 'credential': good}, guest2, GUEST2_CSRF))
st, login, _ = call('POST', '/login', cookie=guest2, csrf=GUEST2_CSRF, form='username=alice&password=wrong-pass')
step('login wrong password', (st, None, None))
