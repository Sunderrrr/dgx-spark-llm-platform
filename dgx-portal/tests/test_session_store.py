"""Server-side sessions (session_store.py): the cookie is an opaque token, the
data lives in `web_sessions`, keyed by the token's hash.

What these lock: no data in the cookie; the token is never stored in clear;
an old signed cookie keeps its owner signed in (converted once); logging in
renews the token (session fixation); logging out deletes the row; an unknown
or expired token is an anonymous visitor.
"""
import time
import unittest

from werkzeug.security import generate_password_hash

import app as portal
import session_store

USER = 'ztest-sess'
PASSWORD = 'Ztest-session-pw-42'


def cookie(client):
    c = client.get_cookie('session')
    return c.value if c else None


class SessionStoreTest(unittest.TestCase):

    def setUp(self):
        portal.app.config['TESTING'] = True
        self.addCleanup(self._clean)
        self._clean()
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO local_users (username, password_hash, fullname, is_admin, enabled, created_at) "
                       "VALUES (?,?,?,0,1,?)", (USER, generate_password_hash(PASSWORD), USER, time.time()))
            db.commit()

    def _clean(self):
        with portal.app.app_context():
            db = portal.get_db()
            for t in ('local_users', 'user_sessions', 'user_sources', 'login_attempts'):
                try:
                    db.execute(f"DELETE FROM {t} WHERE username=?", (USER,))
                except Exception:                                # noqa: BLE001
                    pass
            db.commit()

    def _row(self, token):
        with portal.app.app_context():
            return portal.get_db().execute("SELECT data FROM web_sessions WHERE token_hash=?",
                                           (session_store.token_hash(token),)).fetchone()

    def _login(self, c):
        c.get('/api/csrf')
        with c.session_transaction() as s:
            csrf = s['csrf']
        r = c.post('/login', data={'username': USER, 'password': PASSWORD, 'csrf_token': csrf},
                   headers={'X-CSRFToken': csrf})
        self.assertIn(r.status_code, (200, 302), r.get_data(as_text=True)[:200])
        return csrf

    def test_cookie_is_an_opaque_token_and_data_stays_server_side(self):
        c = portal.app.test_client()
        self._login(c)
        token = cookie(c)
        self.assertIsNotNone(token)
        self.assertNotIn('.', token, "a signed cookie carries data; the token must not")
        self.assertNotIn(USER, token)
        row = self._row(token)
        self.assertIsNotNone(row)
        self.assertIn(USER, row['data'])
        with portal.app.app_context():
            stored = portal.get_db().execute("SELECT 1 FROM web_sessions WHERE token_hash=?", (token,)).fetchone()
        self.assertIsNone(stored, "the token itself is never stored, only its hash")
        self.assertEqual(c.get('/api/whoami').get_json().get('username'), USER)

    def test_login_renews_the_token(self):
        c = portal.app.test_client()
        c.get('/api/csrf')
        before = cookie(c)
        self.assertIsNotNone(before, "an anonymous visitor gets a session for its CSRF token")
        self._login(c)
        after = cookie(c)
        self.assertNotEqual(before, after)
        self.assertIsNone(self._row(before), "the pre-login token must be gone")

    def test_logout_deletes_the_row(self):
        c = portal.app.test_client()
        csrf = self._login(c)
        token = cookie(c)
        c.post('/logout', data={'csrf_token': csrf}, headers={'X-CSRFToken': csrf})
        row = self._row(token)
        self.assertTrue(row is None or USER not in row['data'])
        self.assertNotEqual(c.get('/api/whoami').status_code, 200)

    def test_legacy_signed_cookie_is_converted(self):
        legacy = portal.app.session_interface._legacy.get_signing_serializer(portal.app)
        value = legacy.dumps({'username': USER, 'fullname': USER, 'is_admin': False,
                              'auth_at': int(time.time()), 'csrf': 'tok'})
        c = portal.app.test_client()
        c.set_cookie('session', value)
        r = c.get('/api/whoami')
        self.assertEqual(r.get_json().get('username'), USER, "nobody is logged out by the switch")
        token = cookie(c)
        self.assertNotIn('.', token)
        self.assertIsNotNone(self._row(token))

    def test_anonymous_rows_are_short_lived(self):
        c = portal.app.test_client()
        c.get('/api/csrf')
        with portal.app.app_context():
            exp = portal.get_db().execute("SELECT expires_at FROM web_sessions WHERE token_hash=?",
                                          (session_store.token_hash(cookie(c)),)).fetchone()['expires_at']
        self.assertLessEqual(exp - time.time(), session_store.ANONYMOUS_LIFETIME_S + 5)

    def test_unknown_or_expired_token_is_anonymous(self):
        c = portal.app.test_client()
        c.set_cookie('session', 'x' * 43)
        self.assertNotEqual(c.get('/api/whoami').status_code, 200)
        c2 = portal.app.test_client()
        self._login(c2)
        token = cookie(c2)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("UPDATE web_sessions SET expires_at=? WHERE token_hash=?",
                       (time.time() - 1, session_store.token_hash(token)))
            db.commit()
        self.assertNotEqual(c2.get('/api/whoami').status_code, 200)


if __name__ == '__main__':
    unittest.main()
