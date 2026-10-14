"""Server-side sessions: the cookie carries an opaque random token, the data
lives in the portal database.

Flask's default session is a SIGNED cookie holding the data itself (account,
admin flag, CSRF token…). Two reasons to move away from it (2026-10-14,
phase 0 of the Rust migration, docs/rust-migration.md):
  - another service (the Rust portal, route by route) must read the same
    sessions during the cohabitation — reproducing Flask's signature and
    serialisation format there would be a trap; reading a database row by
    the hash of a token is not;
  - a cookie that carries data is only as fresh as its last write: logging an
    account out everywhere meant a revocation registry beside it
    (user_sessions). With the data server-side, a deleted row IS a logout.

Format, for any implementation:
  cookie `session` = 43 url-safe characters (secrets.token_urlsafe(32));
  table `web_sessions`: token_hash = sha256(token) hex, data = JSON (Flask's
  tagged serialiser: plain JSON for the values the portal stores), expires_at
  = epoch seconds. Only the hash is stored: a database leak gives no cookie.

A legacy signed cookie (contains '.') is read once and converted: nobody is
logged out by the switch.
"""
import hashlib
import os
import random
import secrets
import time

from flask.json.tag import TaggedJSONSerializer
from flask.sessions import SecureCookieSessionInterface, SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict

from db import get_db

TOKEN_BYTES = 32
# A visitor who never logs in (the login page fetches a CSRF token) gets a
# short-lived row: bots hitting the login page must not fill the table for
# the 12 h of a real session.
ANONYMOUS_LIFETIME_S = 2 * 3600
_serializer = TaggedJSONSerializer()


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS web_sessions (
        token_hash TEXT PRIMARY KEY,
        data       TEXT NOT NULL,
        expires_at REAL NOT NULL,
        updated_at REAL NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_web_sessions_expires ON web_sessions(expires_at)")


class ServerSession(CallbackDict, SessionMixin):
    """A session whose data is server-side; `token` is None until saved."""

    def __init__(self, initial=None, token=None, new=False):
        def on_update(self):
            self.modified = True
        super().__init__(initial, on_update)
        self.token = token
        self.new = new
        self.modified = False
        self.rotate = False

    def regenerate(self):
        """A new token for the same data — at every login (session fixation:
        a token known before authentication must not survive it)."""
        self.rotate = True
        self.modified = True


class DbSessionInterface(SessionInterface):
    """Flask SessionInterface over the `web_sessions` table."""

    # The longest a session lives without being written. Authenticated
    # sessions also carry `auth_at` and expire after SESSION_MAX_AGE whatever
    # their activity (auth._session_expired) — this bound only sweeps rows.
    def __init__(self, lifetime_s):
        self.lifetime_s = lifetime_s
        self._legacy = SecureCookieSessionInterface()

    def open_session(self, app, request):
        raw = request.cookies.get(self.get_cookie_name(app))
        if not raw:
            return ServerSession(new=True)
        if '.' in raw:
            # Legacy signed cookie: its data becomes a server session, saved
            # (and the cookie replaced) at the end of this request.
            legacy = self._legacy.open_session(app, request)
            s = ServerSession(dict(legacy or {}), new=True)
            s.modified = bool(s)
            return s
        row = get_db().execute(
            "SELECT data FROM web_sessions WHERE token_hash=? AND expires_at>?",
            (token_hash(raw), time.time())).fetchone()
        if row is None:
            return ServerSession(new=True)        # unknown, expired or logged out
        try:
            data = _serializer.loads(row['data'])
        except ValueError:
            data = {}
        return ServerSession(data, token=raw)

    def save_session(self, app, session, response):
        name = self.get_cookie_name(app)
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        db = get_db()
        if not session:
            # Emptied (logout) or never filled: no row, no cookie.
            if session.token:
                db.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_hash(session.token),))
                db.commit()
            if session.modified or session.token:
                response.delete_cookie(name, domain=domain, path=path,
                                       secure=self.get_cookie_secure(app),
                                       httponly=self.get_cookie_httponly(app),
                                       samesite=self.get_cookie_samesite(app))
            return
        if not (session.modified or session.token is None):
            return
        now = time.time()
        if session.rotate and session.token:
            db.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_hash(session.token),))
            session.token = None
        if session.token is None:
            session.token = secrets.token_urlsafe(TOKEN_BYTES)
        db.execute(
            "INSERT INTO web_sessions (token_hash, data, expires_at, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(token_hash) DO UPDATE SET data=excluded.data, expires_at=excluded.expires_at, "
            "updated_at=excluded.updated_at",
            (token_hash(session.token), _serializer.dumps(dict(session)),
             now + (self.lifetime_s if 'username' in session else ANONYMOUS_LIFETIME_S), now))
        # Sweep the expired rows now and then (no thread: a write is the cue).
        if random.random() < 0.02:
            db.execute("DELETE FROM web_sessions WHERE expires_at<=?", (now,))
        db.commit()
        response.set_cookie(name, session.token, expires=self.get_expiration_time(app, session),
                            httponly=self.get_cookie_httponly(app), domain=domain, path=path,
                            secure=self.get_cookie_secure(app), samesite=self.get_cookie_samesite(app))


_WEAK_SECRETS = {'changeme', 'secret', 'dev', 'test', 'password', 'changeme!'}


def install(app):
    """The portal's session setup: the signing key, then the server store.

    The key still signs the legacy cookies being converted, Authlib's state
    and the CSRF comparisons, and it is the portal's most critical secret:
    with it, a legacy cookie `{'username': 'admin', 'is_admin': True}` can be
    forged. `os.environ[...]` already failed when it was missing but accepted
    any value — including the `changeme` of `.env.example`, with which a
    forged session was accepted (verified on a throwaway app). So the portal
    refuses to start, loudly, rather than at exploitation time."""
    key = os.environ['SECRET_KEY']
    if len(key) < 32 or key.strip().lower() in _WEAK_SECRETS:
        raise RuntimeError(
            "SECRET_KEY absente ou non aléatoire : il faut au moins 32 caractères "
            "aléatoires (elle est générée par setup.sh dans .env — ne jamais "
            "recopier la valeur d'exemple).")
    app.secret_key = key
    app.session_interface = DbSessionInterface(int(os.environ.get('SESSION_MAX_AGE', 12 * 3600)))
