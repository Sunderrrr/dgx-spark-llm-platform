"""Authentication guardrails and response rendering of the portal.

These tests cover what really broke in production in the past: the
open-redirect of the `next` parameter, the anti-brute-force lockout (which
lived in process memory and restarted at every redeploy), the CSRF
protection, and the possible collision between an MCP server tool and a
built-in privileged tool.
"""

import ast
import builtins
import io
import json
import os
import time
import unittest
from unittest.mock import patch

import app as portal
# _apply_session lives in auth.py: it decides the fate of the CSRF token
# when opening a session (see CsrfApresConnexionTest).
import auth
# The chat left the monolith for chat_routes.py (28/08): we target it in
# its own module.
import chat_routes as chat
# The support left the monolith for support.py (28/08): we target it in
# its own module.
import support as assistance
# These symbols left the monolith for websearch_tools.py (28/08): we
# target their own module rather than make app.py a facade.
import websearch_tools as web_tools

import admin_users  # noqa: E402
import admin_models  # noqa: E402


class SafeNextTest(unittest.TestCase):
    """`?next=` must never send back anywhere but the portal."""

    def _resolve(self, target):
        with portal.app.test_request_context():
            return portal._safe_next(target)

    def test_accepts_local_path(self):
        self.assertEqual(self._resolve('/keys'), '/keys')

    def test_blocks_external_redirects(self):
        for target in ('https://evil.com', '//evil.com', 'http://evil.com',
                      '/\\evil.com', '/\tevil', '/\nevil'):
            self.assertEqual(self._resolve(target), '/', target)

    def test_empty_target(self):
        self.assertEqual(self._resolve(''), '/')
        self.assertEqual(self._resolve(None), '/')


class LoginLockoutTest(unittest.TestCase):
    """The counter is in database: it must be shared across workers and
    survive a restart."""

    def setUp(self):
        self.ctx = portal.app.test_request_context()
        self.ctx.push()
        portal.get_db().execute("DELETE FROM login_attempts")
        portal.get_db().commit()

    def tearDown(self):
        portal.get_db().execute("DELETE FROM login_attempts")
        portal.get_db().commit()
        self.ctx.pop()

    def test_locks_after_threshold(self):
        key = 'ip-test|bob'
        for _ in range(portal.LOGIN_MAX_FAILS - 1):
            portal._login_fail(key)
        self.assertEqual(portal._login_locked(key), 0)
        portal._login_fail(key)
        self.assertGreater(portal._login_locked(key), 0)

    def test_persisted_in_db(self):
        portal._login_fail('ip-test|bob')
        row = portal.get_db().execute(
            "SELECT fails FROM login_attempts WHERE key='ip-test|bob'").fetchone()
        self.assertEqual(row['fails'], 1)

    def test_reset_clears_counter(self):
        key = 'ip-test|bob'
        for _ in range(portal.LOGIN_MAX_FAILS):
            portal._login_fail(key)
        portal._login_reset(key)
        self.assertEqual(portal._login_locked(key), 0)

    def test_sliding_window(self):
        key = 'ip-test|bob'
        portal._login_fail(key)
        # Backdates the first attempt beyond the window: the counter must
        # restart from zero rather than accumulate forever.
        portal.get_db().execute("UPDATE login_attempts SET first_at=? WHERE key=?",
                                (time.time() - portal.LOGIN_WINDOW - 10, key))
        portal.get_db().commit()
        portal._login_fail(key)
        row = portal.get_db().execute(
            "SELECT fails FROM login_attempts WHERE key=?", (key,)).fetchone()
        self.assertEqual(row['fails'], 1)


class LoginLockoutPerUserTest(unittest.TestCase):
    """The lockout threshold must also accumulate per username, not only per
    IP: a botnet changing IP at each try must not bypass the protection
    (each single IP stays under the threshold, the account itself locks)."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self.client = portal.app.test_client()
        with self.client.session_transaction() as s:
            s['csrf'] = 'test-csrf'

    def tearDown(self):
        with portal.app.test_request_context():
            portal.get_db().execute("DELETE FROM login_attempts")
            portal.get_db().commit()

    def _login(self, ip, username, password='x'):
        # Username with '@' (fails USERNAME_RE) so as not to depend on a
        # reachable LDAP: the failure path stays instant and purely local.
        return self.client.post('/login',
                                data={'username': username, 'password': password},
                                headers={'X-CSRFToken': 'test-csrf',
                                         'Cf-Connecting-Ip': ip})

    def test_invalid_username_writes_nothing_and_does_not_lock(self):
        """An out-of-format identifier is refused BEFORE any database write.

        `username` comes from an unauthenticated form and keys
        `login_attempts`: without a filter, a fresh 4 Mo name wrote ~8 Mo of
        rows per request (different key each time, thus never locked) and the
        table is only purged at startup.
        """
        r = self._login('198.51.100.250', 'x' * 300)
        self.assertEqual(r.status_code, 401)
        with portal.app.test_request_context():
            row = portal.get_db().execute(
                "SELECT 1 FROM login_attempts WHERE key LIKE 'user:x%'").fetchone()
            self.assertIsNone(row, "nothing must be written for a refused username")

    def test_botnet_rotating_ips_locks_user(self):
        # Username matching the format accepted everywhere (USERNAME_RE):
        # we exercise the per-account counter, not the input filter.
        u = 'bob.cible'
        for i in range(portal.LOGIN_MAX_FAILS - 1):  # threshold - 1, all different IPs
            self.assertEqual(self._login(f'198.51.100.{i+1}', u).status_code, 401)
        with portal.app.test_request_context():
            row = portal.get_db().execute(
                "SELECT fails FROM login_attempts WHERE key=?", ('user:bob.cible',)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['fails'], portal.LOGIN_MAX_FAILS - 1)
            self.assertEqual(portal._login_locked('user:bob.cible'), 0)
        # The threshold is reached on the account → locked, even from a new IP,
        # and the next attempt is blocked BEFORE being counted.
        self.assertEqual(self._login('203.0.113.201', u).status_code, 401)
        with portal.app.test_request_context():
            self.assertGreater(portal._login_locked('user:bob.cible'), 0)
            self._login('203.0.113.202', u)
            row = portal.get_db().execute(
                "SELECT fails FROM login_attempts WHERE key=?", ('user:bob.cible',)).fetchone()
            self.assertEqual(row['fails'], portal.LOGIN_MAX_FAILS)


class SessionRegistryTest(unittest.TestCase):
    """The server-side registry makes immediate revocation possible: a locked
    account / a revoked session expires at once, even if the signed cookie
    (stolen or replayed) is still valid."""

    def setUp(self):
        self.ctx = portal.app.test_request_context()
        self.ctx.push()
        portal.get_db().execute("DELETE FROM user_sessions")
        portal.get_db().commit()

    def tearDown(self):
        portal.get_db().execute("DELETE FROM user_sessions")
        portal.get_db().commit()
        self.ctx.pop()

    def test_apply_session_creates_registry(self):
        portal._apply_session('bob', 'Bob', False)
        self.assertIn('sid', portal.session)
        row = portal.get_db().execute(
            "SELECT username, revoked FROM user_sessions WHERE sid=?",
            (portal.session['sid'],)).fetchone()
        self.assertEqual(row['username'], 'bob')
        self.assertEqual(row['revoked'], 0)

    def test_valid_session_not_expired(self):
        portal._apply_session('bob', 'Bob', False)
        self.assertFalse(portal._session_expired())

    def test_revoked_session_expired(self):
        portal._apply_session('bob', 'Bob', False)
        portal._revoke_user_sessions('bob')
        self.assertTrue(portal._session_expired())

    def test_session_without_sid_not_expired(self):
        # Session without sid (later than the registry? no: earlier, or a
        # test one): we keep expiry by age, no forced revocation — this is
        # what avoids logging everyone out at migration time.
        portal.session['username'] = 'bob'
        portal.session['auth_at'] = int(time.time())
        self.assertFalse(portal._session_expired())
        # But a session without sid and too old does expire by age.
        portal.session['auth_at'] = int(time.time()) - portal.SESSION_MAX_AGE - 10
        self.assertTrue(portal._session_expired())


class CsrfTest(unittest.TestCase):
    """Every unsafe request must carry a valid CSRF token."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self.client = portal.app.test_client()

    def test_post_without_token_refused(self):
        self.assertEqual(self.client.post('/login', data={'username': 'x'}).status_code, 400)

    def test_post_with_wrong_token_refused(self):
        r = self.client.post('/login', data={'username': 'x'},
                             headers={'X-CSRFToken': 'faux'})
        self.assertEqual(r.status_code, 400)

    def test_get_needs_no_token(self):
        self.assertNotEqual(self.client.get('/api/config').status_code, 400)


class AuthGateTest(unittest.TestCase):
    """Protected endpoints must serve nothing without a session."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self.client = portal.app.test_client()

    def test_endpoints_proteges(self):
        for route in ('/api/settings', '/api/keys', '/api/whoami', '/api/admin'):
            r = self.client.get(route)
            self.assertIn(r.status_code, (302, 401, 403), f"{route} -> {r.status_code}")

    def test_api_session_says_if_browser_logged_in(self):
        # The Next.js proxy redirects to /login on a 401 and ONLY on a 401
        # (lib/session-gate.ts): a 302 here would serve the empty page again.
        self.assertEqual(self.client.get('/api/session').status_code, 401)
        with self.client.session_transaction() as s:
            s['username'] = 'demo'
            s['auth_at'] = int(time.time())
        r = self.client.get('/api/session')
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.data, b'')
        # An expired session answers 401 too, and Flask clears it.
        with self.client.session_transaction() as s:
            s['auth_at'] = 0
        self.assertEqual(self.client.get('/api/session').status_code, 401)


class McpToolNameTest(unittest.TestCase):
    """A hostile MCP server must not be able to mask a built-in tool."""

    def test_prefix_always_present(self):
        self.assertTrue(assistance._mcp_tool_name(3, 'search').startswith('mcp_3_'))

    def test_no_collision_with_builtin_tools(self):
        builtin = {'create_api_key', 'revoke_api_key', 'request_budget',
                    'request_model', 'launch_model', 'stop_model', 'use_skill'}
        for name in list(builtin) + ['../create_api_key', 'create api key']:
            self.assertNotIn(assistance._mcp_tool_name(1, name), builtin)

    def test_dangerous_characters_neutralized(self):
        generated = assistance._mcp_tool_name(1, 'a b/c\\d"e')
        self.assertTrue(all(c.isalnum() or c in '_-' for c in generated), generated)


class CleanReplyTest(unittest.TestCase):
    def test_removes_reasoning_block(self):
        self.assertEqual(assistance._clean_reply('<think>bla</think>Bonjour'), 'Bonjour')

    def test_keeps_what_follows_final_marker(self):
        self.assertEqual(assistance._clean_reply('cheminement...\n### Réponse\nVoilà'), 'Voilà')

    def test_plain_text_unchanged(self):
        self.assertEqual(assistance._clean_reply('Bonjour'), 'Bonjour')


class SseFramingTest(unittest.TestCase):
    """The frontend only reads `data:` lines; the framing must stay exact."""

    def test_text_frame(self):
        frame = chat._sse_text('salut')
        self.assertTrue(frame.startswith('data: '))
        self.assertTrue(frame.endswith('\n\n'))
        self.assertIn('salut', frame)

    def test_escapes_newlines(self):
        # A raw \n would cut the SSE frame in two and break the stream.
        self.assertNotIn('\n', chat._sse_text('a\nb')[:-2])

    def test_done_optional(self):
        self.assertIn('[DONE]', ''.join(chat._sse_chunks('x', done=True)))
        self.assertNotIn('[DONE]', ''.join(chat._sse_chunks('x', done=False)))
        # Structured notices share the switch: a mid-stream notice carries
        # ONLY its data line (the end path already sends the sentinel).
        self.assertIn('[DONE]', chat._sse_notice('x'))
        self.assertNotIn('[DONE]', chat._sse_notice('x', done=False))


class AvatarTest(unittest.TestCase):
    def test_strict_allowlist(self):
        # The id lands in an <img> src: no free input.
        self.assertIn('claude', portal.AVATAR_IDS)
        self.assertNotIn('../../etc/passwd', portal.AVATAR_IDS)
        self.assertNotIn('avatar-01', portal.AVATAR_IDS)  # old set removed


if __name__ == '__main__':
    unittest.main()


class SessionLifetimeTest(unittest.TestCase):
    """A session must not be eternal: the signed cookie carries `is_admin`,
    a cookie theft otherwise gave permanent access."""

    def test_fresh_session_valid(self):
        with portal.app.test_request_context():
            from flask import session
            session['username'] = 'bob'
            session['auth_at'] = time.time()
            self.assertFalse(portal._session_expired())

    def test_stale_session(self):
        with portal.app.test_request_context():
            from flask import session
            session['username'] = 'bob'
            session['auth_at'] = time.time() - portal.SESSION_MAX_AGE - 1
            self.assertTrue(portal._session_expired())

    def test_session_without_timestamp_is_stale(self):
        # Sessions issued before auth_at was added: we prefer forcing a
        # re-login rather than treat them as eternal.
        with portal.app.test_request_context():
            from flask import session
            session['username'] = 'bob'
            self.assertTrue(portal._session_expired())

    def test_anonymous_not_affected(self):
        with portal.app.test_request_context():
            self.assertFalse(portal._session_expired())


class GuardedToolsTest(unittest.TestCase):
    """The result of an MCP/skill tool is third-party text reinjected in the
    model's context: irreversible actions must be out of reach of a prompt
    injection."""

    def test_destructive_actions_are_guarded(self):
        for name in ('revoke_api_key', 'launch_model', 'stop_model'):
            self.assertIn(name, assistance.GUARDED_TOOLS)

    def test_harmless_actions_are_not(self):
        for name in ('request_budget', 'request_model', 'list_models'):
            self.assertNotIn(name, assistance.GUARDED_TOOLS)


class OidcUsernameTest(unittest.TestCase):
    """preferred_username/nickname/email are user-modifiable in many IdPs,
    and that value becomes the ownership key of all data: it must pass the
    same filter as the LDAP path."""

    def test_accepts_normal_username(self):
        for name in ('alice', 'jean.dupont', 'a-b_c', 'x' * 64):
            self.assertTrue(portal.USERNAME_RE.match(name), name)

    def test_rejects_forged_usernames(self):
        for name in ('', 'a' * 65, '../admin', 'bob@evil.com', 'bob bob',
                    'bob\nadmin', "bob'--", 'bob/../root'):
            self.assertIsNone(portal.USERNAME_RE.match(name), name)


class CsrfLazyTest(unittest.TestCase):
    """Regression: the token was created in before_request, so EVERY
    response set a Set-Cookie. On /login, /api/csrf and /api/whoami go out
    in parallel without a cookie: each created a fresh session with a
    different token, the last Set-Cookie overwrote the other, and the POST
    /login left with an orphan token → 400, displayed « Identifiants incorrects »."""

    def test_anonymous_request_creates_no_session(self):
        client = portal.app.test_client()
        response = client.get('/api/whoami')
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('Set-Cookie', response.headers)

    def test_api_csrf_creates_and_returns_token(self):
        client = portal.app.test_client()
        response = client.get('/api/csrf')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['token'])
        self.assertIn('Set-Cookie', response.headers)

    def test_token_stable_between_calls(self):
        client = portal.app.test_client()
        first = client.get('/api/csrf').get_json()['token']
        # An interleaved request must not rotate the token.
        client.get('/api/whoami')
        self.assertEqual(client.get('/api/csrf').get_json()['token'], first)

    def test_post_without_session_refused(self):
        client = portal.app.test_client()
        self.assertEqual(client.post('/logout', data={'csrf_token': 'inventé'}).status_code, 400)


class CsrfAfterLoginTest(unittest.TestCase):
    """Regression of 2026-09-14: opening a session REPLACED the CSRF token.

    The browser keeps the one it memorized at page load; from then on every
    kept POST left with the old one and got « 400 Bad Request —
    CSRF token manquant ou invalide ». Reported on the LOGOUT after an SSO
    login, i.e. the place where the user is left with no way out. The token
    lives in a signed and HttpOnly cookie: keeping it does not reopen CSRF
    fixation, and the fresh `sid` keeps assuring the protection against
    session fixation.
    """

    def test_client_token_survives_login(self):
        from flask import session as session_flask

        with portal.app.test_request_context('/'):
            session_flask['csrf'] = 'jeton-que-le-navigateur-connait'
            auth._apply_session('demo', 'demo', False, via_sso=True)
            self.assertEqual(session_flask['csrf'], 'jeton-que-le-navigateur-connait')

    def test_token_created_when_missing(self):
        from flask import session as session_flask

        with portal.app.test_request_context('/'):
            session_flask.clear()
            auth._apply_session('demo', 'demo', False)
            self.assertTrue(session_flask.get('csrf'))

    def test_logout_accepts_client_token(self):
        """The full path of the symptom: SSO session, client token, POST."""
        client = portal.app.test_client()
        with client.session_transaction() as s:
            s['csrf'] = 'jeton-du-client'
            s['username'] = 'demo'
            s['fullname'] = 'demo'
            s['is_admin'] = False
            s['sso'] = True
            s['auth_at'] = int(time.time())
        response = client.post('/logout', data={'csrf_token': 'jeton-du-client'})
        self.assertEqual(response.status_code, 302)

    def test_logout_always_refuses_invented_token(self):
        """The fix must not turn the guard into a sieve."""
        client = portal.app.test_client()
        with client.session_transaction() as s:
            s['csrf'] = 'jeton-du-client'
            s['username'] = 'demo'
            s['auth_at'] = int(time.time())
        self.assertEqual(
            client.post('/logout', data={'csrf_token': 'inventé'}).status_code, 400)


class ClientIpTest(unittest.TestCase):
    """The chain is client → Cloudflare → Traefik → Next.js → Flask:
    request.remote_addr was always the frontend container IP, the same for
    everyone. The global lock _login_locked(ip) thus added up the failures
    of ALL users and blocked the whole portal."""

    def _ip(self, headers):
        with portal.app.test_request_context(headers=headers,
                                             environ_base={'REMOTE_ADDR': '172.19.0.5'}):
            return portal._client_ip()

    def test_prefers_cf_connecting_ip(self):
        self.assertEqual(self._ip({'Cf-Connecting-Ip': '203.0.113.10',
                                   'X-Forwarded-For': '10.0.0.1, 172.19.0.5'}),
                         '203.0.113.10')

    def test_falls_back_to_last_x_forwarded_for(self):
        # The LAST element is the one OUR trusted hop appended; the first is
        # client-controlled and let a caller rotate a fake prefix to get a fresh
        # lockout key on every attempt (audit 2026-10-06, L2).
        self.assertEqual(self._ip({'X-Forwarded-For': '203.0.113.10, 172.19.0.5'}),
                         '172.19.0.5')

    def test_falls_back_to_remote_addr(self):
        self.assertEqual(self._ip({}), '172.19.0.5')

    def test_two_visitors_do_not_share_a_lock(self):
        a = self._ip({'Cf-Connecting-Ip': '203.0.113.10'})
        b = self._ip({'Cf-Connecting-Ip': '203.0.113.20'})
        self.assertNotEqual(a, b)


class ModelHistoryTest(unittest.TestCase):
    """What the playground sends back to the model must NEVER be amputated.

    The original bug: each message was truncated to 8 000 characters, so
    after a long answer the model re-read its own file cut mid-way and
    claimed to have interrupted itself — true from its point of view.
    """

    def _msgs(self, *sizes):
        return [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'x' * n}
                for i, n in enumerate(sizes)]

    def test_big_message_not_truncated(self):
        h = self._msgs(50, 57_000)
        out = chat._history_for_model(h, '', 262144)
        self.assertEqual(len(out[-1]['content']), 57_000)

    def test_unknown_context_touches_nothing(self):
        h = self._msgs(10, 900_000)
        self.assertEqual(chat._history_for_model(h, '', None), h)

    def test_overflow_drops_oldest(self):
        # budget = (32768 - 8192) * 3 = 73 728 characters
        h = self._msgs(40_000, 40_000, 40_000, 500)
        out = chat._history_for_model(h, '', 32768)
        self.assertLess(len(out), len(h))
        # the last exchange always survives, whole
        self.assertEqual(out[-1]['content'], h[-1]['content'])
        self.assertEqual(len(out[-2]['content']), 40_000)

    def test_never_fewer_than_two_messages(self):
        h = self._msgs(500_000, 500_000)
        out = chat._history_for_model(h, '', 32768)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(out[0]['content']), 500_000)

    def test_system_counts_in_budget(self):
        h = self._msgs(30_000, 30_000, 30_000, 100)
        without = chat._history_for_model(list(h), '', 32768)
        with_ = chat._history_for_model(list(h), 'y' * 20_000, 32768)
        self.assertLessEqual(len(with_), len(without))


class ToolsContextTest(unittest.TestCase):
    """The tool phase re-reads a SHORT version of the conversation.

    Passing it the whole history added a full preload before the answer:
    measured 30 s on 100 Ko of context, over 60 s beyond — and the client
    gave up on a slightly old conversation, while a fresh one worked.
    Deciding « faut-il chercher ? » does not call for re-reading a 65 Ko
    file.
    """

    def test_big_message_is_shortened(self):
        msgs = [{'role': 'user', 'content': 'x' * 65_000},
                {'role': 'user', 'content': 'et maintenant ?'}]
        out = web_tools._tools_context(msgs)
        self.assertTrue(all(len(m['content']) <= web_tools.TOOLS_MSG_MAX + 8 for m in out))

    def test_total_stays_bounded(self):
        msgs = [{'role': 'user', 'content': 'y' * 20_000} for _ in range(20)]
        out = web_tools._tools_context(msgs)
        self.assertLessEqual(sum(len(m['content']) for m in out),
                             web_tools.TOOLS_TOTAL_MAX + web_tools.TOOLS_MSG_MAX)

    def test_last_message_always_there(self):
        msgs = [{'role': 'user', 'content': 'z' * 30_000} for _ in range(10)]
        msgs.append({'role': 'user', 'content': 'CE QUE JE DEMANDE'})
        out = web_tools._tools_context(msgs)
        self.assertIn('CE QUE JE DEMANDE', out[-1]['content'])

    def test_system_kept_first(self):
        msgs = [{'role': 'system', 'content': 'consignes'},
                {'role': 'user', 'content': 'bonjour'}]
        out = web_tools._tools_context(msgs)
        self.assertEqual(out[0]['role'], 'system')
        self.assertIn('consignes', out[0]['content'])

    def test_start_and_end_kept_in_cut_message(self):
        # The request is often at the top, the last instruction at the tail: it is
        # the belly of the file that teaches nothing.
        msgs = [{'role': 'user', 'content': 'DEBUT' + 'm' * 60_000 + 'FIN'}]
        out = web_tools._tools_context(msgs)
        self.assertIn('DEBUT', out[-1]['content'])
        self.assertIn('FIN', out[-1]['content'])

    def test_short_conversation_passes_unchanged(self):
        msgs = [{'role': 'user', 'content': 'salut'},
                {'role': 'assistant', 'content': 'bonjour'}]
        self.assertEqual([m['content'] for m in web_tools._tools_context(msgs)],
                         ['salut', 'bonjour'])


class PertinenceRechercheTest(unittest.TestCase):
    """Search only goes out on an explicit directive.

    Five versions failed in production by guessing the intent from isolated
    words: « google » came from a font tag, « source » from a
    createBufferSource, « en ligne » from a « jeu d'échecs en ligne » — that
    one blocked one user six times in a row.
    """

    def _u(self, t):
        return [{'role': 'user', 'content': t}]

    def test_historical_false_triggers(self):
        for t in ("j'aurais besoin que tu regardes dans ce fichier, "
                  "c'est un jeu d'échecs en ligne, sauf que j'ai un problème",
                  "fais-moi un jeu d'échecs en ligne",
                  "tu peux me faire une dissertation sur la propagation du son",
                  "hello comment vas-tu ?",
                  '<link rel="preconnect" href="https://fonts.googleapis.com">',
                  "const source = ctx.createBufferSource();",
                  "quelles sont les dernières nouvelles ?",
                  "quel est le prix du bitcoin ?"):
            self.assertFalse(web_tools._search_relevant(self._u(t)), t)

    def test_explicit_directives(self):
        for t in ("cherche sur internet les règles du blackjack",
                  "cherche sur le web la doc de cette API",
                  "va voir sur internet ce que ça donne",
                  "regarde sur le web si c'est encore vrai",
                  "fais une recherche sur les ondes sonores",
                  "lance une recherche web",
                  "recherche web : propagation du son",
                  # « en ligne » is no longer a target: it describes a state more
                  # often than it names the web (« site en ligne »).
                  "renseigne-toi sur internet là-dessus"):
            self.assertTrue(web_tools._search_relevant(self._u(t)), t)

    def test_putting_online_does_not_trigger(self):
        # 2026-09: « en ligne » describes a state; « je cherche à mettre mon
        # site en ligne » opened a web search — that is over.
        for t in ("je cherche à mettre mon site en ligne",
                  "regarde pourquoi mon serveur n'est plus en ligne",
                  "fais-moi un jeu d'échecs en ligne"):
            self.assertFalse(web_tools._search_relevant(self._u(t)), t)

    def test_directive_works_even_with_pasted_file(self):
        h = [{'role': 'user', 'content': "```html\n" + ("x" * 5000)
              + "\n```\n\ncherche sur internet la doc de cette balise"}]
        self.assertTrue(web_tools._search_relevant(h))

    def test_empty_conversation(self):
        self.assertFalse(web_tools._search_relevant([]))


class StaleVersionsTest(unittest.TestCase):
    """Only the last version of each file goes back to the model.

    Measured on real conversations: 42 332 of the 72 182 characters of a
    thread were old versions of the same file, replayed at every message.
    """

    def _msg(self, role, content):
        return {'role': role, 'content': content}

    def _file(self, name, marker, n=3000):
        return f"Voici `{name}` :\n\n```html\n<!-- {marker} -->\n" + ("x" * n) + "\n```"

    def test_only_last_version_survives(self):
        h = [self._msg('user', 'fais un jeu'),
             self._msg('assistant', self._file('index.html', 'V1')),
             self._msg('user', 'corrige'),
             self._msg('assistant', self._file('index.html', 'V2'))]
        out = chat._without_stale_versions(h)
        self.assertNotIn('V1', out[1]['content'])
        self.assertIn('version précédente', out[1]['content'])
        self.assertIn('V2', out[3]['content'])

    def test_two_distinct_files_keep_their_version(self):
        h = [self._msg('assistant', self._file('index.html', 'HTML1')),
             self._msg('assistant', self._file('style.css', 'CSS1'))]
        out = chat._without_stale_versions(h)
        self.assertIn('HTML1', out[0]['content'])
        self.assertIn('CSS1', out[1]['content'])

    def test_user_pasted_code_never_touched(self):
        pasted = "```html\n<!-- COLLE -->\n" + ("y" * 3000) + "\n```"
        h = [self._msg('user', pasted),
             self._msg('assistant', self._file('index.html', 'V1'))]
        out = chat._without_stale_versions(h)
        self.assertIn('COLLE', out[0]['content'])

    def test_short_excerpt_stales_nothing(self):
        h = [self._msg('assistant', self._file('index.html', 'V1')),
             self._msg('assistant', "Regarde :\n\n```js\nconst a = 1;\n```")]
        out = chat._without_stale_versions(h)
        self.assertIn('V1', out[0]['content'])

    def test_prose_around_block_kept(self):
        h = [self._msg('assistant', self._file('index.html', 'V1') + "\n\nJ'ai ajouté le son."),
             self._msg('assistant', self._file('index.html', 'V2'))]
        out = chat._without_stale_versions(h)
        self.assertIn("J'ai ajouté le son.", out[0]['content'])

    def test_gain_is_real(self):
        h = [self._msg('assistant', self._file('index.html', f'V{i}', 20000)) for i in range(4)]
        before = sum(len(m['content']) for m in h)
        after = sum(len(m['content']) for m in chat._without_stale_versions(h))
        self.assertLess(after, before * 0.4)

    def test_conversation_without_code_unchanged(self):
        h = [self._msg('user', 'bonjour'), self._msg('assistant', 'salut')]
        self.assertEqual(chat._without_stale_versions(h), h)


class FindingsTest(unittest.TestCase):
    """What the search brings back is reinjected as TEXT, never as a `tool` role.

    Sending `tool_calls` and `tool`-role messages without declaring the
    tools gave a conversation the template cannot render: 35 tokens
    produced, no content received, « The model returned no response ».
    """

    def test_nothing_found_nothing_added(self):
        self.assertEqual(web_tools._findings_text([]), '')

    def test_content_included_and_framed_as_external(self):
        t = web_tools._findings_text([('recherche_web', '{"resultats": [{"url": "https://x.fr"}]}')])
        self.assertIn('https://x.fr', t)
        self.assertIn('externes', t)

    def test_text_stays_bounded(self):
        t = web_tools._findings_text([('lire_pages', 'x' * 200_000)])
        self.assertLessEqual(len(t), 40_000)

    def test_several_calls_all_included(self):
        t = web_tools._findings_text([('recherche_web', 'AAA'), ('lire_pages', 'BBB')])
        self.assertIn('AAA', t)
        self.assertIn('BBB', t)


class RouteGuardTest(unittest.TestCase):
    """Every portal route is authenticated, except an explicit list.

    Recounted on 2026-09-13 on the live `url_map`: 153 routes, 11 without
    guard, no omission (146 before the six account management routes added
    that day: blocking, unblocking, detail, sessions and self-service
    password — all guarded). This test freezes that result. It does NOT
    read the source — it walks Flask's `url_map` and asks the `_garde`
    marker set by login_required/admin_required, so it also sees a route
    registered other than by a literal `@app.route`.

    The real risk covered is not the current state of the code but its
    future: the OCR container runs third-party model code
    (`--trust-remote-code`) and shares `ocr_net` with the portal. A public
    route added by oversight would become reachable from that code. If
    this test fails, the question is not « how to make it pass » but
    « does this route really have to be public ».
    """

    # Each entry is public FOR A REASON. We add none without
    # savoir dire laquelle.
    PUBLIC_ROUTES = {
        'api_config':         "ne renvoie que {oidc_enabled}, lu avant connexion",
        # The deployment's logo, shown on the LOGIN page — there is no session
        # yet. It is branding (like the favicon), not user data: the route
        # serves only the bytes the admin uploaded, never a path.
        'api_branding_logo':  "logo de la plateforme, affiché sur la page de connexion",
        'login':              "point d'entrée de l'authentification",
        'login_sso':          "redirection vers le fournisseur OIDC",
        'oauth_callback':     "retour du fournisseur OIDC, hors session",
        'logout':             "doit marcher même sur une session déjà expirée",
        'api_csrf':           "délivre le jeton CSRF nécessaire pour se connecter",
        # WebAuthn 2nd factor: the user is NOT authenticated yet (the
        # password / LDAP was just validated upstream), this endpoint finishes
        # the login. It requires the CSRF token + a one-time challenge + a valid assertion.
        'webauthn.security_verify_login': "finalise un login 2FA (assertion passkey), hors session",
        # Prefix 'admin.' since the administration is a blueprint (28/08):
        # the PATHS did not move, only the endpoint names.
        'admin.internal_authcheck': "appelé par Traefik (forwardAuth), jamais par un "
                                    "navigateur ; ne renvoie aucune donnée",
        'static':             "fichiers statiques servis par Flask",
        'api_setup':          "owner creation on a fresh instance: nobody can be logged in "
                              "yet; gated by the setup token from the server log, and refused "
                              "as soon as anything administers the instance",
        'security_txt':       "RFC 9116 security contact: public by definition, "
                              "only the configured contact and canonical URL",
        'healthz':            "liveness publique (healthcheck / sonde) : ne renvoie "
                              "que {ok, time}, rien d'interné",
        'prom_metrics':       "métriques Prometheus (texte) pour Grafana : publique "
                              "par choix (pull), mais REFUSÉE (403) dès que la requête "
                              "traverse l'edge Cloudflare (en-tête Cf-Connecting-Ip) — "
                              "cf. test_metrics_refuse_via_cloudflare",
        'conversations.share_view': "vue publique, lecture seule, d'une conversation "
                                    "partagée (jeton opaque) : contenu échappé",
    }

    def test_no_unguarded_route_outside_list(self):
        unguarded = set()
        for rule in portal.app.url_map.iter_rules():
            view = portal.app.view_functions.get(rule.endpoint)
            if view is None or getattr(view, '_guard', None):
                continue
            unguarded.add(rule.endpoint)
        new_ones = unguarded - set(self.PUBLIC_ROUTES)
        self.assertEqual(new_ones, set(),
                         "route(s) without login_required/admin_required: "
                         f"{sorted(new_ones)} — publishing a route is a choice, "
                         "not a default: document it in PUBLIC_ROUTES or add a guard.")

    def test_public_list_does_not_rot(self):
        """An entry matching no route anymore must disappear."""
        known_set = {r.endpoint for r in portal.app.url_map.iter_rules()}
        self.assertEqual(set(self.PUBLIC_ROUTES) - known_set, set(),
                         "stale entry(ies) in PUBLIC_ROUTES")

    def test_marker_is_set(self):
        """Without a marker, the main test would pass while seeing nothing."""
        guarded = [r.endpoint for r in portal.app.url_map.iter_rules()
                   if getattr(portal.app.view_functions.get(r.endpoint), '_guard', None)]
        self.assertGreater(len(guarded), 90, "the _guard marker disappeared from the decorators")


class ResolvableNamesTest(unittest.TestCase):
    """No portal module loads a name defined nowhere.

    Python resolves globals AT CALL time. A name gone to another module
    during an extraction thus breaks neither the import, nor the tests, nor
    the route table comparison — only the user's request, in production.

    Lived twice on 28/08 during the split: `_read_uploaded_image` carried
    away with the video section while /api/ocr/extract used it, then
    `image_ready`/`get_music_model` left referenced by the sidecar
    dashboard. Both imported cleanly and would have raised a NameError on
    the first click. This test would have seen them; that is why it exists.
    """

    MODULES = [
        'app', 'auth', 'config', 'db', 'guards', 'comfyui_client', 'discord_notify',
        'websearch_tools', 'memory_routes', 'conversation_routes', 'video_routes',
        'image_routes', 'voice_routes', 'asr_routes', 'ocr_routes',
    ]

    def _unresolved_names(self, path):
        tree = ast.parse(io.open(path, encoding='utf-8').read())
        known = set(dir(builtins)) | {'__file__', '__name__', '__doc__'}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                known.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    known.add((a.asname or a.name).split('.')[0])
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                known.add(n.id)
            elif isinstance(n, ast.arg):
                known.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                known.add(n.name)
            elif isinstance(n, ast.Global):
                known.update(n.names)
        return sorted({n.id for n in ast.walk(tree)
                       if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                       and n.id not in known})

    def test_each_module_resolves_all_names(self):
        racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in self.MODULES:
            path = os.path.join(racine, f'{name}.py')
            if not os.path.exists(path):
                continue
            with self.subTest(module=name):
                self.assertEqual(
                    self._unresolved_names(path), [],
                    f"{name}.py loads a name defined nowhere — it will raise a "
                    "NameError when called. Re-import it from the module that "
                    "defines it now.")


class HealthRouteTest(unittest.TestCase):
    """/healthz (public liveness) and /api/health (aggregated state, logged-in)."""

    def setUp(self):
        portal.app.config["TESTING"] = True

    def test_public_healthz_returns_ok(self):
        c = portal.app.test_client()
        r = c.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_api_health_requires_session(self):
        c = portal.app.test_client()
        r = c.get("/api/health")
        self.assertEqual(r.status_code, 401)

    def test_api_health_returns_service_state(self):
        import unittest.mock as mock
        with mock.patch.object(portal, "_service_reachable", return_value=False), \
                mock.patch.object(portal, "get_running_models", return_value=[]), \
                mock.patch.object(portal, "comfyui_is_up", return_value=False), \
                mock.patch.object(portal, "get_ocr_model", return_value=None), \
                mock.patch.object(portal, "get_voice_model", return_value=None), \
                mock.patch.object(portal, "image_ready", return_value=False):
            c = portal.app.test_client()
            with c.session_transaction() as s:
                s["username"] = "demo"
                s["auth_at"] = int(time.time())
            r = c.get("/api/health")
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
            self.assertIn("services", body)
            self.assertFalse(body["services"]["runner"]["reachable"])
            self.assertFalse(body["ok"])

    def test_litellm_reachable_even_when_health_needs_key(self):
        """LiteLLM >= 1.102 answers 401 on `/health` (key required): the portal
        must not conclude the proxy is down. Regression of 2026-09-24 — the
        upgrade 1.92 → 1.102.1 made « LiteLLM injoignable » display on the
        dashboard while the proxy served requests."""
        import unittest.mock as mock

        class FakeResponse:
            def __init__(self, code):
                self.status_code = code

        def fake_get(url, timeout=None):
            if url.endswith("/health"):          # 1.102: authenticated
                return FakeResponse(401)
            if url.endswith("/health/liveliness"):  # public, the real probe
                return FakeResponse(200)
            return FakeResponse(200)                   # runner /status, etc.

        with mock.patch.object(portal.requests, "get", side_effect=fake_get), \
                mock.patch.object(portal, "get_running_models", return_value=["m"]), \
                mock.patch.object(portal, "comfyui_is_up", return_value=False), \
                mock.patch.object(portal, "get_ocr_model", return_value=None), \
                mock.patch.object(portal, "get_voice_model", return_value=None), \
                mock.patch.object(portal, "image_ready", return_value=False):
            c = portal.app.test_client()
            with c.session_transaction() as s:
                s["username"] = "demo"
                s["auth_at"] = int(time.time())
            body = c.get("/api/health").get_json()
        self.assertTrue(body["services"]["litellm"]["reachable"])
        self.assertTrue(body["services"]["runner"]["reachable"])
        self.assertTrue(body["ok"])


class PendingCountRouteTest(unittest.TestCase):
    """/api/pending-count: sidebar badge (model requests + pending budget)."""

    def setUp(self):
        portal.app.config["TESTING"] = True
        with portal.app.app_context():
            db = portal.get_db()
            for t in ("model_requests", "budget_requests"):
                db.execute(f"DELETE FROM {t}")
            db.commit()

    def test_requires_session(self):
        c = portal.app.test_client()
        self.assertEqual(c.get("/api/pending-count").status_code, 401)

    def test_counts_pending_requests(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO model_requests (username, fullname, model_id, created_at, status) "
                "VALUES ('demo','D','m1','2025-01-01','pending')")
            db.execute(
                "INSERT INTO model_requests (username, fullname, model_id, created_at, status) "
                "VALUES ('demo','D','m2','2025-01-01','approved')")
            db.execute(
                "INSERT INTO budget_requests (username, fullname, key_alias, created_at, status) "
                "VALUES ('other','O','k','2025-01-01','pending')")
            db.commit()
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
        # Non-admin user: only THEIR requests, and by TYPE — a budget
        # request must never light up the « modèle » badge.
        self.assertEqual(c.get("/api/pending-count").get_json(),
                         {'model': 1, 'budget': 0})
        # Admin: all (demo pending + other pending), by type.
        with c.session_transaction() as s:
            s["is_admin"] = True
        self.assertEqual(c.get("/api/pending-count").get_json(),
                         {'model': 1, 'budget': 1})


class DeletedModelAnnouncementTest(unittest.TestCase):
    """The announcement feed is a « what's new », not a journal: removing a
    model from the catalog must remove the announcement that presented it."""

    def setUp(self):
        portal.app.config["TESTING"] = True
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM model_configs WHERE name='ztest-modele'")
            db.execute("DELETE FROM announcements WHERE a='ztest-modele'")
            db.execute("INSERT INTO model_configs (name, hf_model_id, vllm_args, engine, added_at) "
                       "VALUES ('ztest-modele','org/modele','','llamacpp','2026-09-13')")
            db.execute("INSERT INTO announcements (kind, a, b, created_at) "
                       "VALUES ('model_add','ztest-modele','','2026-09-13')")
            db.commit()
            self.mid = portal.get_db().execute(
                "SELECT id FROM model_configs WHERE name='ztest-modele'").fetchone()['id']

    def test_deletion_also_removes_announcement(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "boss"
            s["auth_at"] = int(time.time())
            s["is_admin"] = True
            s["csrf"] = "test-csrf"
        with patch.object(admin_models, '_unregister_litellm_model', return_value=True), \
             patch.object(admin_models, 'runner_delete_files', return_value=(True, 0, '')):
            r = c.post(f"/admin/model/delete/{self.mid}",
                       headers={'X-CSRFToken': 'test-csrf'})
        # JSON contract: no more redirect. The `flash` was rendered by no
        # template and the redirect HTML response counted as a success on
        # the UI side (its `catch` treated non-JSON as « ok »).
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        with portal.app.app_context():
            db = portal.get_db()
            announcements = db.execute("SELECT COUNT(*) FROM announcements "
                                  "WHERE kind='model_add' AND a='ztest-modele'").fetchone()[0]
            configs = db.execute("SELECT COUNT(*) FROM model_configs WHERE id=?",
                                 (self.mid,)).fetchone()[0]
            trace = db.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0]
        self.assertEqual(announcements, 0)
        self.assertEqual(configs, 0)
        self.assertEqual(trace, 'model.delete')


class BudgetGrantsTest(unittest.TestCase):
    """Temporary grants (boost N days) + cap redefinition."""

    def setUp(self):
        portal.app.config["TESTING"] = True
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM budget_requests")
            db.execute("DELETE FROM budget_grants")
            db.execute(
                "INSERT INTO budget_requests (username, fullname, key_alias, current_budget, reason, status, created_at) "
                "VALUES ('demo','D','(compte)',200000000,'plus svp','pending','2025-01-01')")
            db.commit()
            self.req_id = portal.get_db().execute(
                "SELECT id FROM budget_requests").fetchone()['id']

    def _login(self, c, admin=True):
        with c.session_transaction() as s:
            s["username"] = "boss"
            s["auth_at"] = int(time.time())
            s["is_admin"] = admin
            s["csrf"] = "test-csrf"

    def test_temporary_approve_creates_grant(self):
        """Approve with duration: boosted cap + budget_grants row."""
        with patch.object(admin_users, '_litellm_user_info', return_value={'max_budget': 200000000}), \
             patch.object(admin_users, 'litellm_update_user_budget', return_value=True) as up:
            c = portal.app.test_client()
            self._login(c)
            r = c.post(f"/admin/budget/approve/{self.req_id}", data={"amount": "50000000", "grant_days": "3", "csrf_token": "test-csrf"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()['ok'])
        args = up.call_args
        self.assertEqual(args.args[1], 250000000)          # 200M + 50M
        with portal.app.app_context():
            g = portal.get_db().execute("SELECT * FROM budget_grants").fetchone()
            self.assertIsNotNone(g)
            self.assertEqual(g['username'], 'demo')
            self.assertEqual(g['base_budget'], 200000000)  # back to the base…
            self.assertEqual(g['current_budget'], 250000000)

    def test_expiry_returns_to_base(self):
        """At expiry: the account is brought back to the base cap."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) "
                "VALUES ('demo', 200000000, 250000000, '2020-01-01T00:00:00', '2020-01-01', '2020-01-01')")
            db.commit()
        with portal.app.app_context(), \
             patch.object(admin_users, '_litellm_user_info', return_value={'max_budget': 250000000}), \
             patch.object(admin_users, 'litellm_update_user_budget', return_value=True) as up:
            brought_back = admin_users.revert_expired_grants()
        self.assertEqual(brought_back, 1)
        args = up.call_args
        self.assertEqual(args.args[1], 200000000)          # …to the base, nothing else
        with portal.app.app_context():
            self.assertIsNone(portal.get_db().execute("SELECT * FROM budget_grants").fetchone())

    def test_expiry_does_not_override_admin_decision(self):
        """If the admin redefined the cap meanwhile, we do not overwrite."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) "
                "VALUES ('demo', 200000000, 250000000, '2020-01-01T00:00:00', '2020-01-01', '2020-01-01')")
            db.commit()
        with portal.app.app_context(), \
             patch.object(admin_users, '_litellm_user_info', return_value={'max_budget': 50000000}), \
             patch.object(admin_users, 'litellm_update_user_budget', return_value=True) as up:
            brought_back = admin_users.revert_expired_grants()
        self.assertEqual(brought_back, 0)
        up.assert_not_called()                             # the admin decision is authoritative

    def test_lowering_the_cap(self):
        """/budget/set sets an EXACT amount: 200M → 50M, and purges the boosts."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) "
                "VALUES ('demo', 200000000, 250000000, '2099-01-01T00:00:00', '2025-01-01', '2025-01-01')")
            db.commit()
        with patch.object(admin_users, 'litellm_update_user_budget', return_value=True) as up:
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/admin/users/demo/budget/set", data={"budget": "50000000", "csrf_token": "test-csrf"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()['ok'])
        args = up.call_args
        self.assertEqual(args.args[1], 50000000)           # exact amount, not +
        with portal.app.app_context():
            self.assertIsNone(portal.get_db().execute("SELECT * FROM budget_grants").fetchone())

    def test_set_refuses_unrealistic_amounts(self):
        with patch.object(admin_users, 'litellm_update_user_budget', return_value=True) as up:
            c = portal.app.test_client()
            self._login(c)
            c.post("/admin/users/demo/budget/set", data={"budget": "6666726666666", "csrf_token": "test-csrf"})
            up.assert_not_called()                         # unlimited-making typo refused


class BudgetPeriodTest(unittest.TestCase):
    """Budget window slicing + remaining quota computation."""

    def setUp(self):
        portal._BUDGET_CACHE.clear()

    def test_budget_period_days_parse(self):
        self.assertEqual(portal._budget_period_days('1d'), 1)
        self.assertEqual(portal._budget_period_days('7d'), 7)
        self.assertEqual(portal._budget_period_days('30d'), 30)
        self.assertEqual(portal._budget_period_days('3 mois'), 30)
        self.assertEqual(portal._budget_period_days('xyz'), 1)

    def test_budget_remaining_uses_real_tokens(self):
        import unittest.mock as mock
        with mock.patch.object(portal, "_real_tokens_by_user", return_value={'budget-test': 1200}):
            used, remaining = portal._budget_remaining('budget-test', 10000, '7d')
            self.assertEqual(used, 1200)
            self.assertEqual(remaining, 8800)
        # Floor: never negative once over budget. We empty the cache
        # between the two (else the TTL returns the previous value).
        portal._BUDGET_CACHE.clear()
        with mock.patch.object(portal, "_real_tokens_by_user", return_value={'budget-test': 99999}):
            used, remaining = portal._budget_remaining('budget-test', 1000, '7d')
            self.assertEqual(remaining, 0)


class PromMetricsTest(unittest.TestCase):
    """/metrics (Prometheus): text for a LAN scrape — and REFUSED as soon as
    the request arrives via the Cloudflare edge (the frontend rewrite exposed it)."""

    def test_metrics_public_on_lan(self):
        c = portal.app.test_client()
        r = c.get("/metrics")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("dgx_cpu_pct", body)
        self.assertIn("dgx_model_online", body)
        self.assertIn("dgx_gpu_util_pct", body)

    def test_metrics_refuse_via_cloudflare(self):
        """Cloudflare sets Cf-Connecting-Ip on EVERY request crossing the
        edge: its presence proves the call comes from the internet
        (the public web domain), not the LAN/netbird, so we serve no host metrics."""
        c = portal.app.test_client()
        r = c.get("/metrics", headers={"Cf-Connecting-Ip": "203.0.113.7"})
        self.assertEqual(r.status_code, 403)
        body = r.get_data(as_text=True)
        self.assertNotIn("dgx_cpu_pct", body)
        self.assertNotIn("dgx_gpu_util_pct", body)


class MediaCancelTest(unittest.TestCase):
    """Cancellation of an image/video generation (« Arrêter » button)."""

    CSRF = "test-csrf"

    def setUp(self):
        portal.app.config["TESTING"] = True
        with portal.app.app_context():
            db = portal.get_db()
            for t in ("image_jobs", "video_jobs"):
                db.execute(f"DELETE FROM {t}")
            db.commit()

    def _login(self, c, username="demo"):
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF

    def _headers(self):
        return {"X-CSRFToken": self.CSRF}

    def test_post_without_token_refused(self):
        # before_request checks the CSRF first thing (defence in depth).
        c = portal.app.test_client(); self._login(c)
        self.assertEqual(c.post("/api/image/cancel/p1").status_code, 400)

    def test_img_cancel_unknown_404(self):
        c = portal.app.test_client(); self._login(c)
        self.assertEqual(c.post("/api/image/cancel/inconnu", headers=self._headers()).status_code, 404)

    def test_img_cancel_noop_if_done(self):
        with portal.app.app_context():
            portal.get_db().execute(
                "INSERT INTO image_jobs (username,prompt_id,prompt,status,count,done_count,created_at) "
                "VALUES ('demo','p1','x','done',1,1,'2025-01-01')")
            portal.get_db().commit()
        c = portal.app.test_client(); self._login(c)
        r = c.post("/api/image/cancel/p1", headers=self._headers())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_img_cancel_cancels_running_job(self):
        with portal.app.app_context():
            portal.get_db().execute(
                "INSERT INTO image_jobs (username,prompt_id,prompt,status,count,done_count,created_at) "
                "VALUES ('demo','p2','x','running',4,0,'2025-01-01')")
            portal.get_db().commit()
        c = portal.app.test_client(); self._login(c)
        self.assertTrue(c.post("/api/image/cancel/p2", headers=self._headers()).get_json()["ok"])
        with portal.app.app_context():
            st = portal.get_db().execute(
                "SELECT status FROM image_jobs WHERE prompt_id='p2'").fetchone()["status"]
        self.assertEqual(st, "cancelled")

    def test_video_cancel_cancels(self):
        with portal.app.app_context():
            portal.get_db().execute(
                "INSERT INTO video_jobs (username,prompt_id,prompt,status,created_at,req_duration_s) "
                "VALUES ('demo','v1','x','running','2025-01-01',5)")
            portal.get_db().commit()
        c = portal.app.test_client(); self._login(c)
        self.assertTrue(c.post("/api/video/cancel/v1", headers=self._headers()).get_json()["ok"])


class ShareConversationTest(unittest.TestCase):
    """Sharing a conversation: read-only link, RESERVED TO LOGGED-IN USERS
    (requirement of 2026-09-09: a leaked link shows nothing to an anonymous)."""

    CSRF = "test-csrf"

    def setUp(self):
        portal.app.config["TESTING"] = True
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM conversations")
            db.execute("DELETE FROM conversation_shares")
            db.commit()

    def _login(self, c, username="demo"):
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF

    def test_post_without_token_refused(self):
        # No session → before_request answers 400 (missing token) before any
        # other consideration: we never create an anonymous share.
        self.assertEqual(
            portal.app.test_client().post("/conversations/share", data={"client_id": "x"}).status_code,
            400)

    def test_share_then_read_only_view(self):
        with portal.app.app_context():
            portal.get_db().execute(
                "INSERT INTO conversations (username,client_id,title,model,messages,updated_at) "
                "VALUES ('demo','c1','Titre','m1','[{\"role\":\"user\",\"content\":\"Bonjour\"}]','2025-01-01')")
            portal.get_db().commit()
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        r = c.post("/conversations/share", data={"client_id": "c1"}, headers={"X-CSRFToken": self.CSRF})
        self.assertEqual(r.status_code, 200)
        token = r.get_json()["token"]
        self.assertTrue(token)
        view = c.get(f"/c/{token}")
        self.assertEqual(view.status_code, 200)
        self.assertIn("Bonjour", view.get_data(as_text=True))

    def test_share_accepts_json(self):
        """The playground sends JSON (sendJSON), not form: accepted."""
        with portal.app.app_context():
            portal.get_db().execute(
                "INSERT INTO conversations (username,client_id,title,model,messages,updated_at) "
                "VALUES ('demo','c1','Titre','m1','[{\"role\":\"user\",\"content\":\"Bonjour\"}]','2025-01-01')")
            portal.get_db().commit()
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        r = c.post("/conversations/share", json={"client_id": "c1"},
                   headers={"X-CSRFToken": self.CSRF})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_anonymous_view_redirects_to_login(self):
        """Anonymous + valid token → login redirect, NEVER the conversation."""
        with portal.app.app_context():
            portal.get_db().execute(
                "INSERT INTO conversations (username,client_id,title,model,messages,updated_at) "
                "VALUES ('demo','c1','Titre','m1','[{\"role\":\"user\",\"content\":\"Bonjour\"}]','2025-01-01')")
            portal.get_db().commit()
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        r = c.post("/conversations/share", data={"client_id": "c1"}, headers={"X-CSRFToken": self.CSRF})
        token = r.get_json()["token"]
        anon = portal.app.test_client()
        view = anon.get(f"/c/{token}", follow_redirects=False)
        self.assertIn(view.status_code, (301, 302))
        self.assertIn("/login", view.headers.get("Location", ""))

    def test_unknown_view_404(self):
        # Logged in + unknown token → 404. (Anonymous, a login redirect
        # comes first: we do not even reveal the link exists.)
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
        self.assertEqual(c.get("/c/nimportequoi").status_code, 404)


class AuditLogTest(unittest.TestCase):
    """Audit log: write + admin read."""

    def setUp(self):
        portal.app.config["TESTING"] = True
        with portal.app.app_context():
            portal.get_db().execute("DELETE FROM audit_log")
            portal.get_db().commit()

    def test_audit_without_session_refused(self):
        c = portal.app.test_client()
        self.assertIn(c.get("/admin/audit").status_code, (302, 401, 403))

    def test_log_then_list(self):
        from db import log_audit
        log_audit("ops", "model.launch", "lancement de test")
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "ops"
            s["auth_at"] = int(time.time())
            s["is_admin"] = True
        rows = c.get("/admin/audit").get_json()
        self.assertTrue(any(r["action"] == "model.launch" and r["username"] == "ops" for r in rows))


class NotificationsTest(unittest.TestCase):
    """Centrale de notifications : liste (cloche) + marquage lu + page /docs."""

    def setUp(self):
        portal.app.config["TESTING"] = True
        with portal.app.app_context():
            portal.get_db().execute("DELETE FROM notifications")
            portal.get_db().commit()

    def _login(self, username="alice", is_admin=False):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["is_admin"] = is_admin
            s["csrf"] = "test-csrf"
        return c

    def test_list_and_counter(self):
        from db import add_notification
        add_notification("alice", "image", "Génération image terminée (2/2).")
        add_notification("alice", "request", "Budget accordé : +100 tokens.")
        c = self._login()
        data = c.get("/api/notifications").get_json()
        self.assertEqual(data["unread"], 2)
        self.assertEqual(len(data["items"]), 2)
        self.assertTrue(all(not i["seen"] for i in data["items"]))

    def test_mark_read(self):
        from db import add_notification
        add_notification("alice", "image", "Génération image terminée (1/1).")
        c = self._login()
        self.assertEqual(c.get("/api/notifications").get_json()["unread"], 1)
        r = c.post("/api/notifications/seen", headers={"X-CSRFToken": "test-csrf"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/notifications").get_json()["unread"], 0)

    def test_without_session_refused(self):
        c = portal.app.test_client()
        self.assertIn(c.get("/api/notifications").status_code, (302, 401, 403))
        # POST without CSRF → 400 (before_request CSRF before login_required).
        self.assertEqual(c.post("/api/notifications/seen").status_code, 400)

    def test_docs_require_login(self):
        c = portal.app.test_client()
        self.assertIn(c.get("/docs").status_code, (302, 401, 403))
        c2 = self._login()
        r = c2.get("/docs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("application", r.get_data(as_text=True).lower())


class PlaygroundTitleSummarizeTest(unittest.TestCase):
    """Playground auto-title / summary routes: POST login_required, CSRF."""

    def _paths(self):
        rules = {str(r) for r in portal.app.url_map.iter_rules()}
        return rules

    def test_routes_registered(self):
        self.assertIn('/api/playground/title', self._paths())
        self.assertIn('/api/playground/summarize', self._paths())

    def test_post_without_csrf_refused(self):
        c = portal.app.test_client()
        # before_request CSRF runs before login_required → 400 on POST.
        self.assertEqual(c.post('/api/playground/title').status_code, 400)
        self.assertEqual(c.post('/api/playground/summarize').status_code, 400)

    def test_no_session_refused(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "test-csrf"
        r = c.post('/api/playground/title', headers={"X-CSRFToken": "test-csrf"}, json={"messages": [{"role": "user", "content": "Bonjour"}]})
        self.assertIn(r.status_code, (200, 302, 401, 403, 409, 503))


class PlaygroundTitleSummarizeMockTest(unittest.TestCase):
    """Auto-title/summary routes with mocked model: expected JSON response."""

    CSRF = "test-csrf"

    def _login(self, c, username="demo"):
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF

    def test_title_generated(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "_non_stream", return_value=("Titre court", None)):
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/api/playground/title", headers={"X-CSRFToken": self.CSRF},
                       json={"model": "fake-model", "messages": [{"role": "user", "content": "Bonjour"}]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["title"], "Titre court")

    def test_title_without_model_409(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=[]):
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/api/playground/title", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "user", "content": "Bonjour"}]})
            self.assertEqual(r.status_code, 409)

    def test_empty_title_returns_empty(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "_non_stream", return_value=("", None)):
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/api/playground/title", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "user", "content": "Salut"}]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["title"], "")

    def test_summary_generated(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "_non_stream", return_value=("Résumé du contexte", None)):
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/api/playground/summarize", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "assistant", "content": "Réponse"}]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["summary"], "Résumé du contexte")

    def test_maintenance_stops_title_and_summary(self):
        """In maintenance, these two routes must refuse BEFORE calling the model.

        They called LiteLLM with no guard at all: the chat answered the
        maintenance message while the auto-title went out anyway (measured:
        30 calls → 30×200 with real calls). A model being released was thus
        solicited exactly during the window we want to protect.
        """
        import unittest.mock as mock
        from db import set_setting
        with portal.app.app_context():
            set_setting('maintenance_mode', '1')
        try:
            with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
                 mock.patch.object(chat, "_non_stream") as non_stream:
                c = portal.app.test_client()
                self._login(c)
                for route in ("/api/playground/title", "/api/playground/summarize"):
                    r = c.post(route, headers={"X-CSRFToken": self.CSRF},
                               json={"messages": [{"role": "user", "content": "Bonjour"}]})
                    self.assertEqual(r.status_code, 503, route)
                # The guard passes BEFORE the call: nothing went to LiteLLM.
                non_stream.assert_not_called()
        finally:
            with portal.app.app_context():
                set_setting('maintenance_mode', '0')

    def test_rate_cap_applies_to_title(self):
        """Without a cap, a client-side loop held the gunicorn threads.

        The cap is PROPER to the route (`rl-titre`), not shared with the
        chat: sharing it would halve the user's message budget, since the
        title goes out after every first message.
        """
        import unittest.mock as mock
        import guards
        c = portal.app.test_client()
        # Dedicated account: the bucket is in SQLite and shared by the test
        # process, neighbouring tests of the file already consume `rl-titre`.
        self._login(c, username="plafond-titre")
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "_non_stream", return_value=("Titre", None)), \
             mock.patch.object(guards, "CHAT_RATE_MAX", 2):
            codes = [c.post("/api/playground/title", headers={"X-CSRFToken": self.CSRF},
                            json={"messages": [{"role": "user", "content": "Bonjour"}]}).status_code
                     for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


class SupportBillingTest(unittest.TestCase):
    """The Support assistant spends the GPU ON BEHALF of the user: it must
    go through THEIR key (the one carrying the LiteLLM quota), never the
    master key — else the budget is bypassed and the consumption appears in
    no account (security audit)."""

    CSRF = "test-csrf"

    class _FakeStream:
        """Minimal SSE response: one text fragment, then [DONE]."""

        ok = True
        status_code = 200

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def iter_lines(self, decode_unicode=False):
            yield 'data: {"choices":[{"delta":{"content":"Bonjour"}}]}'
            yield "data: [DONE]"

    def _client(self, username="demo"):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        return c

    def test_support_uses_user_key(self):
        import unittest.mock as mock
        seen = []

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            seen.append(headers or {})
            return self._FakeStream()

        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             mock.patch.object(chat, "quota_exceeded_reset", return_value=None), \
             mock.patch.object(chat, "_finish_support"), \
             mock.patch.object(chat.requests, "post", side_effect=_post):
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "Salut"}]})
            self.assertEqual(r.status_code, 200)
            r.get_data(as_text=True)      # forces the execution of the SSE generator
        self.assertTrue(seen, "aucun appel au modèle n'a été fait")
        self.assertEqual(seen[0].get("Authorization"), "Bearer sk-user-123")

    def test_support_without_key_notifies_and_skips_model(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[]), \
             mock.patch.object(chat, "_finish_support"), \
             mock.patch.object(chat.requests, "post", side_effect=AssertionError("appel modèle interdit")):
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "Salut"}]})
            self.assertEqual(r.status_code, 200)
            self.assertIn("no_api_key", r.get_data(as_text=True))

    def test_support_quota_exceeded_notifies(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             mock.patch.object(chat, "quota_exceeded_reset", return_value=3600), \
             mock.patch.object(chat, "_finish_support"), \
             mock.patch.object(chat.requests, "post", side_effect=AssertionError("appel modèle interdit")):
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "Salut"}]})
            self.assertEqual(r.status_code, 200)
            body = r.get_data(as_text=True)
            self.assertIn("quota_exceeded", body)
            self.assertIn("3600", body)


class SupportStructuredNoticesTest(unittest.TestCase):
    """The Support chat SSE ERROR texts are STRUCTURED notices (`dgx_notice`):
    the server sends an id (+ `wait`/`status` arguments), the frontend writes
    the sentence. The ids are a CONTRACT with `texteNotice` (an unknown id
    displays as raw id), so each one is locked byte-identical here.

    Tool labels and tool results are not error texts and stay as they are.
    """

    CSRF = "test-csrf"

    class _Flux:
        """Fake upstream stream: a list of frames, or an error to raise."""

        def __init__(self, frames, ok=True, status_code=200, error_text=None):
            self.frames = list(frames)
            self.ok = ok
            self.status_code = status_code
            self.error_text = error_text

        def close(self):
            pass

        def iter_lines(self, decode_unicode=False):
            if self.error_text is not None:
                raise self.error_text
            yield from self.frames

    @staticmethod
    def _text_frame(t):
        return "data: " + json.dumps({"choices": [{"delta": {"content": t}}]})

    @staticmethod
    def _tool_frame(name, args, call_id="call_1"):
        delta = {"tool_calls": [{"index": 0, "id": call_id,
                                 "function": {"name": name,
                                              "arguments": json.dumps(args)}}]}
        return "data: " + json.dumps({"choices": [{"delta": delta}]})

    def _client(self, username="demo"):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        return c

    @staticmethod
    def _notices(body):
        """The notice payloads of the stream (`[DONE]` and tool events aside)."""
        out = []
        for line in body.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                payload = json.loads(line[6:])
                if "dgx_notice" in payload:
                    out.append(payload)
        return out

    def _flux(self, messages=(("user", "Salut"),), running=("fake-model",), wait=0,
              post=None):
        """Runs /support/chat and returns (status code, SSE body)."""
        msgs = [{"role": role, "content": content} for role, content in messages]
        with patch.object(chat, "get_running_models", return_value=list(running)), \
             patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             patch.object(chat, "quota_exceeded_reset", return_value=None), \
             patch.object(chat, "_chat_rate_limited", return_value=wait), \
             patch.object(chat, "_user_extra_tools", return_value=([], {})), \
             patch.object(chat, "_exec_support_tool", return_value=("ok", True)), \
             patch.object(chat, "_finish_support"), \
             patch.object(chat.requests, "post",
                          side_effect=post or AssertionError("appel interdit")):
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": msgs})
            return r.status_code, r.get_data(as_text=True)

    def test_empty_message_notice(self):
        code, body = self._flux(messages=())
        self.assertEqual(code, 400)
        self.assertEqual(self._notices(body),
                         [{"dgx_notice": {"id": "empty_message"}}])

    def test_wait_cap_notice(self):
        code, body = self._flux(wait=7)
        self.assertEqual(self._notices(body),
                         [{"dgx_notice": {"id": "chat_rate_limited", "wait": 7}}])
        self.assertNotIn("Trop de", body)

    def test_no_model_notice(self):
        code, body = self._flux(running=())
        self.assertEqual(self._notices(body),
                         [{"dgx_notice": {"id": "no_model_running"}}])

    def test_upstream_unreachable_notice(self):
        import requests as _rq
        code, body = self._flux(post=_rq.exceptions.ConnectionError("coupé"))
        self.assertEqual(self._notices(body),
                         [{"dgx_notice": {"id": "model_unreachable"}}])
        self.assertIn("data: [DONE]", body)

    def test_upstream_error_notice_status(self):
        code, body = self._flux(
            post=lambda *a, **k: self._Flux([], ok=False, status_code=500))
        self.assertEqual(self._notices(body),
                         [{"dgx_notice": {"id": "model_replied_error",
                                             "status": 500}}])

    def test_empty_reply_notice(self):
        code, body = self._flux(post=lambda *a, **k: self._Flux(["data: [DONE]"]))
        self.assertEqual(self._notices(body),
                         [{"dgx_notice": {"id": "empty_reply"}}])

    def test_lecture_timeout_notice(self):
        import requests as _rq
        code, body = self._flux(
            post=lambda *a, **k: self._Flux([], error_text=_rq.exceptions.ReadTimeout()))
        self.assertEqual(self._notices(body),
                         [{"dgx_notice": {"id": "model_timeout"}}])

    def _tool_loop(self, final):
        """4 tool rounds, then the forced final turn served by `final`."""
        calls = {"n": 0}
        tool = self._Flux([self._tool_frame("request_budget", {"alias": "a"}),
                            "data: [DONE]"])

        def _post(*a, **k):
            calls["n"] += 1
            return tool if calls["n"] <= 4 else final
        return self._flux(post=_post)

    @staticmethod
    def _reasoning_frame(t):
        """A thinking frame, as LiteLLM produces it (`reasoning_content`)."""
        return "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": t}}]})

    def test_reasoning_is_relayed(self):
        """The model THINKS (adaptive, like the playground): what it produces is
        generated, so billed — and it was dropped by this relay. It must reach
        the client in `reasoning_content`, the same contract the playground
        uses, so the same block can render it."""
        # ASCII only: `json.dumps` escapes accents (je p\u00e8se…), and this
        # test is about the FRAME SHAPE, not about the encoding.
        frames = [self._reasoning_frame("je pese les options"),
                  self._text_frame("la reponse"),
                  "data: [DONE]"]
        code, body = self._flux(post=lambda *a, **k: self._Flux(frames))
        self.assertEqual(code, 200)
        self.assertIn('"reasoning_content": "je pese les options"', body)
        self.assertIn('"content": "la reponse"', body)

    def test_think_tag_in_content_stays_hidden(self):
        """A think tag COUPLED INTO the content is a parasite, not a structured
        reasoning: it stays hidden while the answer streams."""
        def _c(t):
            return "data: " + json.dumps({"choices": [{"delta": {"content": t}}]})

        frames = [_c("<think>"), _c("mon raisonnement interne"),
                  _c("</think>"), _c("la reponse finale"), "data: [DONE]"]
        code, body = self._flux(post=lambda *a, **k: self._Flux(frames))
        self.assertEqual(code, 200)
        self.assertNotIn("mon raisonnement interne", body)
        self.assertIn("la reponse finale", body)

    def test_model_busy_after_tool_loop(self):
        code, body = self._tool_loop(self._Flux([], ok=False, status_code=503))
        self.assertEqual(self._notices(body),
                         [{"dgx_notice": {"id": "model_busy"}}])

    def test_rephrase_after_tool_loop(self):
        code, body = self._tool_loop(self._Flux(["data: [DONE]"]))
        self.assertEqual(self._notices(body),
                         [{"dgx_notice": {"id": "reformulate"}}])


class SupportServicesTest(unittest.TestCase):
    """The Support can SEE the platform's services and RELAUNCH one (« je suis
    chaud que il puisse regarder si tout fonctionne et que si un service est
    stop il puisse relancer »).

    The state comes from facts (`_sidecar_status`), never from the model's
    word, and a relaunch is a SENSITIVE action: same confirmation flow as
    launching a model, and admin-only.
    """

    def test_service_in_context(self):
        """The context lists every service, so « ça ne marche pas » is answered
        from the actual state."""
        # The snapshot is cached 15 s (probing the services is not free): a
        # previous test filled it, this one must see its own values.
        assistance._services_cache['at'] = 0.0
        with patch.object(assistance, "_sidecar_status",
                          side_effect=lambda k: {"ocr": "stopped", "asr": "running"}.get(k, "running")), \
             patch.object(assistance, "runner_status", return_value={"status": "running"}), \
             patch.object(assistance, "get_db") as gdb:
            gdb.return_value.execute.return_value.fetchall.return_value = []
            ctx = assistance._support_context("demo", True, user_msg="l'OCR ne marche pas")
        self.assertIn("- ocr : stopped", ctx)
        self.assertIn("- asr : running", ctx)
        self.assertIn("Services de la plateforme", ctx)

    def test_tool_offered_only_to_admins(self):
        admin_names = {t["function"]["name"] for t in assistance._support_tools(True)}
        names = {t["function"]["name"] for t in assistance._support_tools(False)}
        self.assertIn("manage_service", admin_names)
        self.assertNotIn("manage_service", names)

    def test_tool_guarded_for_confirmation(self):
        """manage_service is guarded: the model only SUBMITS it."""
        self.assertIn("manage_service", assistance.GUARDED_TOOLS)
        self.assertIn("manage_service", assistance.TOOLS_REFUSED_IF_EXTERNAL)

    def _exec(self, args, is_admin):
        with patch.object(assistance, "get_db"):
            return assistance._exec_support_tool(
                "manage_service", args, "demo", "Demo", is_admin)

    def test_execution_admin_only(self):
        res, ok = self._exec({"service": "ocr", "action": "start"}, False)
        self.assertFalse(ok)
        self.assertIn("administrateurs", res)

    def test_start_returns_new_state(self):
        with patch.object(assistance, "_sidecar_action", return_value=(True, "")) as act, \
             patch.object(assistance, "_sidecar_status", return_value="starting"), \
             patch.object(assistance, "_mem_guard", return_value=None):
            res, ok = self._exec({"service": "ocr", "action": "start"}, True)
        self.assertTrue(ok)
        self.assertIn("ocr : starting", res)
        act.assert_called_once_with("ocr", "start")

    def test_restart_stops_then_starts(self):
        with patch.object(assistance, "_sidecar_action", return_value=(True, "")) as act, \
             patch.object(assistance, "_sidecar_status", return_value="running"), \
             patch.object(assistance, "_mem_guard", return_value=None):
            res, ok = self._exec({"service": "asr", "action": "restart"}, True)
        self.assertTrue(ok)
        self.assertEqual([c.args for c in act.call_args_list],
                         [("asr", "stop"), ("asr", "start")])

    def test_memory_guard_blocks_start(self):
        """On unified memory a sidecar that overflows takes the chat model with
        it: the guard refuses BEFORE the start."""
        with patch.object(assistance, "_mem_guard", return_value="Mémoire insuffisante"), \
             patch.object(assistance, "_sidecar_action") as act:
            res, ok = self._exec({"service": "image", "action": "start"}, True)
        self.assertFalse(ok)
        self.assertIn("Mémoire insuffisante", res)
        act.assert_not_called()

    def test_logs_read_on_demand(self):
        """`read_logs` pulls the tail of a service — the logs are NOT in the
        prompt (« pas que dans le system prompt »)."""
        with patch.object(assistance, "get_db"), \
             patch.object(assistance, "sidecar_logs",
                          return_value=["erreur: poids introuvable", "fin"]) as read:
            res, ok = assistance._exec_support_tool(
                "read_logs", {"service": "asr", "lines": 50}, "demo", "Demo", True)
        self.assertTrue(ok)
        self.assertIn("erreur: poids introuvable", res)
        read.assert_called_once_with("asr", 50)

    def test_litellm_logs_read_without_runner(self):
        """The proxy writes to the SHARED volume: its logs are read from the
        file, no runner involved (a runner restart would kill the model)."""
        with patch.object(assistance, "get_db"), \
             patch("sidecars.open", create=True) as open_:
            open_.side_effect = FileNotFoundError
            res, ok = assistance._exec_support_tool(
                "read_logs", {"service": "litellm"}, "demo", "Demo", True)
        self.assertTrue(ok)          # missing = « no log available », not an error
        self.assertIn("litellm", res)
        self.assertIn("read_logs", {t["function"]["name"] for t in assistance._support_tools(True)})
        self.assertIn("litellm", [t["function"]["parameters"]["properties"]["service"]["enum"]
                                  for t in assistance._support_tools(True)
                                  if t["function"]["name"] == "read_logs"][0])

    def test_logs_admin_only(self):
        with patch.object(assistance, "get_db"):
            res, ok = assistance._exec_support_tool(
                "read_logs", {"service": "model"}, "demo", "Demo", False)
        self.assertFalse(ok)
        self.assertIn("administrateurs", res)
        self.assertIn("read_logs", {t["function"]["name"]
                                    for t in assistance._support_tools(True)})
        self.assertNotIn("read_logs", {t["function"]["name"]
                                       for t in assistance._support_tools(False)})

    def test_logs_no_longer_injected_in_context(self):
        """The context only points at the tool: a turn must not carry a blind
        log tail it did not ask for."""
        assistance._services_cache['at'] = 0.0
        with patch.object(assistance, "_sidecar_status", return_value="running"), \
             patch.object(assistance, "runner_status", return_value={"status": "running"}), \
             patch.object(assistance, "runner_logs", create=True,
                          return_value=["LIGNE_DE_LOG_QUI_NE_DOIT_PAS_APPARAITRE"]), \
             patch.object(assistance, "get_db") as gdb:
            gdb.return_value.execute.return_value.fetchall.return_value = []
            ctx = assistance._support_context(
                "demo", True, user_msg="le modèle renvoie une erreur 500")
        self.assertNotIn("LIGNE_DE_LOG_QUI_NE_DOIT_PAS_APPARAITRE", ctx)
        self.assertIn("read_logs", ctx)

    def test_unknown_service_or_action(self):
        res, ok = self._exec({"service": "imprimerie", "action": "start"}, True)
        self.assertFalse(ok)
        self.assertIn("imprimerie", res)
        res2, ok2 = self._exec({"service": "ocr", "action": "démolir"}, True)
        self.assertFalse(ok2)


class SupportSensibleActionsTest(unittest.TestCase):
    """Support sensitive actions only run on confirmation.

    Before: the « always ask for confirmation » instruction lived in the
    prompt, and nothing on the server side stopped the model from revoking
    a key or stopping the served model on its own initiative. Now the chat
    loop SUBMITS a request, the UI shows a button, and /support/confirm is
    the only execution path — opaque token, bound to the user, single-use.
    """

    CSRF = "test-csrf"

    class _Flux:
        """Simulated SSE stream (successive calls receive different frames)."""

        ok = True
        status_code = 200

        def __init__(self, frames):
            self.frames = frames

        def close(self):
            pass

        def iter_lines(self, decode_unicode=False):
            yield from self.frames

    @staticmethod
    def _tool_frame(name, args, call_id="call_1"):
        delta = {"tool_calls": [{"index": 0, "id": call_id,
                                 "function": {"name": name,
                                              "arguments": json.dumps(args)}}]}
        return "data: " + json.dumps({"choices": [{"delta": delta}]})

    @staticmethod
    def _text_frame(text):
        return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})

    def _client(self, username="demo"):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        return c

    def _clean(self):
        with portal.app.app_context():
            db = portal.get_db()
            for t in ("pending_actions", "support_feedback", "support_thread",
                      "audit_log", "login_attempts"):
                db.execute(f"DELETE FROM {t}")
            db.commit()

    def setUp(self):
        self._wait_finish_threads()
        self._clean()

    tearDown = setUp

    @staticmethod
    def _wait_finish_threads():
        """A turn saves its thread from a daemon thread started AFTER the
        response: left running, it writes into the NEXT test's database."""
        import threading
        for t in threading.enumerate():
            if getattr(t, '_target', None) is chat._finish_support_body:
                t.join(timeout=5)

    def _create_request(self, username="demo", tool="revoke_api_key",
                       args=None, label="Révocation de clé", target="test"):
        with portal.app.app_context():
            return assistance.create_pending_action(username, tool, args or {"alias": "test"},
                                                      label, target)

    def test_sensitive_action_not_run_without_confirmation(self):
        import unittest.mock as mock
        frames = [[self._tool_frame("revoke_api_key", {"alias": "test"}), "data: [DONE]"],
                  [self._text_frame("Je peux révoquer cette clé — confirme ci-dessous."),
                   "data: [DONE]"]]
        calls = {"n": 0}

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            if not stream:
                # A background call (memory extraction of an EARLIER test's
                # thread) lands here too: it must not consume a chat frame.
                raise RuntimeError("non-streaming call outside this test")
            i = min(calls["n"], len(frames) - 1)
            calls["n"] += 1
            return self._Flux(frames[i])

        # `_finish_support` is neutralized: it is a thread started after the
        # response (saved thread + memory extraction) that calls requests.post
        # once the round is over and would steal a simulated frame from the next test.
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             mock.patch.object(chat, "quota_exceeded_reset", return_value=None), \
             mock.patch.object(chat, "requests") as mock_requests, \
             mock.patch.object(chat, "_finish_support"), \
             mock.patch.object(chat, "_exec_support_tool") as exec_tool:
            mock_requests.post.side_effect = _post
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "révoque ma clé test"}]})
            self.assertEqual(r.status_code, 200)
            body = r.get_data(as_text=True)
        # Nothing was executed, and the UI receives what it needs to show the button.
        exec_tool.assert_not_called()
        self.assertIn("dgx_confirm", body)
        self.assertIn("revoke_api_key", body)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT tool, args, status, target FROM pending_actions WHERE username=?",
                ("demo",)).fetchone()
        self.assertIsNotNone(row, "la demande de confirmation doit être enregistrée")
        self.assertEqual(row["tool"], "revoke_api_key")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["target"], "test")
        self.assertIn("test", row["args"])

    def test_confirmation_runs_once(self):
        import unittest.mock as mock
        tok = self._create_request()
        c = self._client()
        with mock.patch.object(chat, "_exec_support_tool",
                               return_value=("Clé « test » révoquée.", True)) as exec_tool:
            r = c.post("/support/confirm", headers={"X-CSRFToken": self.CSRF}, json={"token": tok})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertTrue(r.get_json()["ok"])
            self.assertIn("révoquée", r.get_json()["message"])
            # Second click (or replay): the request is no longer « pending ».
            r2 = c.post("/support/confirm", headers={"X-CSRFToken": self.CSRF}, json={"token": tok})
            self.assertEqual(r2.status_code, 404)
            self.assertEqual(exec_tool.call_count, 1)

    def test_cancel_runs_nothing(self):
        import unittest.mock as mock
        tok = self._create_request()
        c = self._client()
        with mock.patch.object(chat, "_exec_support_tool") as exec_tool:
            r = c.post("/support/confirm", headers={"X-CSRFToken": self.CSRF},
                       json={"token": tok, "cancel": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["ok"])
            exec_tool.assert_not_called()
        with portal.app.app_context():
            st = portal.get_db().execute("SELECT status FROM pending_actions WHERE token=?",
                                         (tok,)).fetchone()["status"]
        self.assertEqual(st, "cancelled")

    def test_other_account_token_refused(self):
        tok = self._create_request(username="bob")
        with patch.object(chat, "_exec_support_tool", return_value=("jamais", False)) as exec_tool:
            r = self._client("demo").post("/support/confirm",
                                          headers={"X-CSRFToken": self.CSRF}, json={"token": tok})
            self.assertEqual(r.status_code, 404)
            exec_tool.assert_not_called()

    def test_unknown_token_refused(self):
        r = self._client().post("/support/confirm", headers={"X-CSRFToken": self.CSRF},
                                json={"token": "n-importe-quoi"})
        self.assertEqual(r.status_code, 404)
        r2 = self._client().post("/support/confirm", headers={"X-CSRFToken": self.CSRF}, json={})
        self.assertEqual(r2.status_code, 400)

    def test_non_sensitive_action_not_delayed(self):
        """create_api_key runs directly: no button for the non-destructive."""
        import unittest.mock as mock
        frames = [[self._tool_frame("create_api_key", {"alias": "ma-cle"}), "data: [DONE]"],
                  [self._text_frame("C'est fait."), "data: [DONE]"]]
        calls = {"n": 0}

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            i = min(calls["n"], len(frames) - 1)
            calls["n"] += 1
            return self._Flux(frames[i])

        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             mock.patch.object(chat, "quota_exceeded_reset", return_value=None), \
             mock.patch.object(chat, "requests") as mock_requests, \
             mock.patch.object(chat, "_exec_support_tool",
                               return_value=("Clé créée.", True)) as exec_tool:
            mock_requests.post.side_effect = _post
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "crée une clé"}]})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("dgx_confirm", r.get_data(as_text=True))
        self.assertEqual(exec_tool.call_count, 1)

    def test_context_contains_recent_actions(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO audit_log (username, action, detail, created_at) "
                       "VALUES (?,?,?,?)",
                       ("demo", "model.launch_échec", "lancement de laguna (OOM)", "2026-09-13T10:00"))
            db.commit()
            ctx = assistance._support_context("demo", False)
        self.assertIn("laguna (OOM)", ctx)
        self.assertIn("Ses dernières actions", ctx)

    def test_feedback_recorded(self):
        c = self._client()
        r = c.post("/support/feedback", headers={"X-CSRFToken": self.CSRF},
                   json={"vote": -1, "comment": "réponse à côté", "question": "ma clé ?",
                         "answer": "je ne sais pas", "model": "fake"})
        self.assertEqual(r.status_code, 200)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT username, vote, comment, question FROM support_feedback").fetchone()
        self.assertEqual(row["vote"], -1)
        self.assertEqual(row["username"], "demo")
        self.assertEqual(row["comment"], "réponse à côté")
        self.assertEqual(row["question"], "ma clé ?")

    def test_support_thread_kept_and_cleared(self):
        c = self._client()
        self.assertEqual(c.get("/api/support/thread").get_json()["messages"], [])
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO support_thread (username, messages, updated_at) VALUES (?,?,?)",
                       ("demo", json.dumps([{"role": "user", "content": "bonjour"}]), time.time()))
            db.commit()
        self.assertEqual(c.get("/api/support/thread").get_json()["messages"][0]["content"], "bonjour")
        r = c.post("/support/thread/clear", headers={"X-CSRFToken": self.CSRF})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/support/thread").get_json()["messages"], [])


class SupportInjectionGuardTest(unittest.TestCase):
    """External content (MCP/skill) read → sensitive actions are refused.

    This guard pre-existed the token confirmation, and the chat loop was
    modified right next to it: without this test, a regression would go unnoticed.
    """

    CSRF = "test-csrf"

    class _Flux:
        ok = True
        status_code = 200

        def __init__(self, frames):
            self.frames = frames

        def close(self):
            pass

        def iter_lines(self, decode_unicode=False):
            yield from self.frames

    @staticmethod
    def _tool(name, args, call_id="call_1"):
        delta = {"tool_calls": [{"index": 0, "id": call_id,
                                 "function": {"name": name, "arguments": json.dumps(args)}}]}
        return "data: " + json.dumps({"choices": [{"delta": delta}]})

    @staticmethod
    def _text(t):
        return "data: " + json.dumps({"choices": [{"delta": {"content": t}}]})

    def test_sensitive_action_refused_after_external_content(self):
        import unittest.mock as mock
        frames = [
            [self._tool("mcp_server_tool", {}), "data: [DONE]"],       # external read
            [self._tool("revoke_api_key", {"alias": "prod"}, "call_2"), "data: [DONE]"],
            [self._text("Je ne peux pas faire ça dans ce tour."), "data: [DONE]"],
        ]
        n = {"i": 0}

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            i = min(n["i"], len(frames) - 1)
            n["i"] += 1
            return self._Flux(frames[i])

        routing = {"mcp_server_tool": {"kind": "mcp", "server_id": 1,
                                         "server_name": "srv", "tool_name": "tool"}}
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user"}]), \
             mock.patch.object(chat, "quota_exceeded_reset", return_value=None), \
             mock.patch.object(chat, "_user_extra_tools", return_value=([], routing)), \
             mock.patch.object(chat, "_exec_mcp_tool", return_value=("texte d'un tiers", True)), \
             mock.patch.object(chat, "requests") as mock_requests, \
             mock.patch.object(chat, "_finish_support"), \
             mock.patch.object(chat, "_exec_support_tool") as exec_tool:
            mock_requests.post.side_effect = _post
            r = c.post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "user", "content": "résume cette page"}]})
            body = r.get_data(as_text=True)
        # No execution, no confirmation proposal: the refusal is clean.
        exec_tool.assert_not_called()
        self.assertNotIn("dgx_confirm", body)
        # The SSE body is JSON: accents are escaped there (\u00e9), so we
        # check the ASCII part of the message.
        self.assertIn("Action bloqu", body)
        with portal.app.app_context():
            n_pending = portal.get_db().execute(
                "SELECT COUNT(*) c FROM pending_actions WHERE username='demo'").fetchone()["c"]
        self.assertEqual(n_pending, 0)

    def test_key_creation_refused_after_external_content(self):
        """`create_api_key` is not destructive, but it DELIVERS a secret.

        It thus runs directly in the normal case (product choice, see
        `test_une_action_non_sensible_n_est_pas_retardee`) — except after
        reading external content: else a hostile page had a key created in
        the logged-in user's name, and its value entered the model's
        context (thus the kept thread) without anyone asking for it.
        """
        import unittest.mock as mock
        frames = [
            [self._tool("mcp_server_tool", {}), "data: [DONE]"],        # external read
            [self._tool("create_api_key", {"alias": "ma-cle"}, "call_2"), "data: [DONE]"],
            [self._text("Je ne crée pas de clé dans ce tour."), "data: [DONE]"],
        ]
        n = {"i": 0}

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            i = min(n["i"], len(frames) - 1)
            n["i"] += 1
            return self._Flux(frames[i])

        routing = {"mcp_server_tool": {"kind": "mcp", "server_id": 1,
                                         "server_name": "srv", "tool_name": "tool"}}
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user"}]), \
             mock.patch.object(chat, "quota_exceeded_reset", return_value=None), \
             mock.patch.object(chat, "_user_extra_tools", return_value=([], routing)), \
             mock.patch.object(chat, "_exec_mcp_tool", return_value=("texte d'un tiers", True)), \
             mock.patch.object(chat, "requests") as mock_requests, \
             mock.patch.object(chat, "_finish_support"), \
             mock.patch.object(chat, "_exec_support_tool") as exec_tool:
            mock_requests.post.side_effect = _post
            r = c.post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "user", "content": "résume cette page"}]})
            body = r.get_data(as_text=True)
        # No execution, no confirmation proposal: the refusal is clean.
        exec_tool.assert_not_called()
        self.assertNotIn("dgx_confirm", body)
        self.assertIn("Action bloqu", body)
        with portal.app.app_context():
            db = portal.get_db()
            n_keys = db.execute(
                "SELECT COUNT(*) c FROM api_keys WHERE username='demo'").fetchone()["c"]
            n_pending = db.execute(
                "SELECT COUNT(*) c FROM pending_actions WHERE username='demo'").fetchone()["c"]
        self.assertEqual(n_keys, 0, "aucune clé ne doit avoir été créée")
        self.assertEqual(n_pending, 0)


class SupportEndOfTurnTest(unittest.TestCase):
    """End of Support round: the thread is kept, a CUT answer is not.

    The thread serves to resume the conversation after a page reload:
    freezing it on an interrupted answer would bring back a truncated
    sentence at every visit, and the memory extraction false facts.
    """

    def _clean(self):
        with portal.app.app_context():
            db = portal.get_db()
            for t in ("support_thread", "audit_log"):
                db.execute(f"DELETE FROM {t}")
            db.commit()

    setUp = _clean
    tearDown = _clean

    HIST = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "réponse"}]

    def _thread(self):
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT messages FROM support_thread WHERE username='demo'").fetchone()
        return json.loads(row["messages"]) if row else None

    def test_complete_reply_saves_thread(self):
        chat._finish_support_body("demo", self.HIST, "réponse finale", "fake", "sk-x",
                                False, portal.app)
        thread = self._thread()
        self.assertEqual([m["content"] for m in thread], ["question", "réponse", "réponse finale"])

    def test_interrupted_reply_saves_nothing(self):
        chat._finish_support("demo", self.HIST, ["réponse à moitié écri"], "fake", "sk-x",
                          False, False, portal.app)
        self.assertIsNone(self._thread(), "a cut reply must not become the thread")


class RunningModelsLegerTest(unittest.TestCase):
    """The media pages only need « what runs »: /api/running-models answers
    that without the spend aggregates and the LiteLLM lookup of /api/home."""

    def test_list_only_without_home_aggregates(self):
        import unittest.mock as mock
        with mock.patch.object(portal, "get_running_models", return_value=["chat-m"]), \
                mock.patch.object(portal, "comfyui_is_up", return_value=True), \
                mock.patch.object(portal, "get_ocr_model", return_value=None), \
                mock.patch.object(portal, "get_voice_model", return_value=None), \
                mock.patch.object(portal, "image_ready", return_value=False), \
                mock.patch.object(portal, "_litellm_user_info") as litellm, \
                mock.patch.object(portal, "_tokens_by_model") as aggregate:
            c = portal.app.test_client()
            self.assertEqual(c.get("/api/running-models").status_code, 401)
            with c.session_transaction() as s:
                s["username"] = "demo"
                s["auth_at"] = int(time.time())
            r = c.get("/api/running-models")
        self.assertEqual(r.status_code, 200)
        kinds = {m["kind"]: m["name"] for m in r.get_json()["running_models"]}
        self.assertEqual(kinds, {"chat": "chat-m", "video": "MiniMax-H3"})
        litellm.assert_not_called()
        aggregate.assert_not_called()


class ComfyuiProbeTest(unittest.TestCase):
    """With video off the probe is a full timeout: only a worker's FIRST call
    may wait on it, later ones get the last answer and refresh behind."""

    def setUp(self):
        import comfyui_client
        self.cc = comfyui_client
        self.addCleanup(self.cc._comfyui_up_cache.update, t=0.0, v=False, refreshing=False)

    def test_first_call_waits_later_ones_do_not(self):
        import threading
        import unittest.mock as mock
        self.cc._comfyui_up_cache.update(t=0.0, v=False, refreshing=False)
        with mock.patch.object(self.cc, "_probe_comfyui", return_value=True) as probe:
            self.assertTrue(self.cc.comfyui_is_up())
            self.assertEqual(probe.call_count, 1)
        # Expired: the stale value comes back at once, the refresh runs behind.
        self.cc._comfyui_up_cache['t'] = time.time() - 60
        finished = threading.Event()
        def slow():
            finished.wait(5)
            return False
        with mock.patch.object(self.cc, "_probe_comfyui", side_effect=slow):
            t0 = time.perf_counter()
            self.assertTrue(self.cc.comfyui_is_up())
            self.assertTrue(self.cc.comfyui_is_up())   # one refresh at a time
            self.assertLess(time.perf_counter() - t0, 0.5)
            finished.set()
            for _ in range(100):
                if not self.cc._comfyui_up_cache['refreshing']:
                    break
                time.sleep(0.02)
        self.assertFalse(self.cc._comfyui_up_cache['v'])
