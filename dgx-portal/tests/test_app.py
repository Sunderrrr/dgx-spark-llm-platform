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
import websearch_tools as outils_web

import admin_routes


class SafeNextTest(unittest.TestCase):
    """`?next=` must never send back anywhere but the portal."""

    def _resolve(self, target):
        with portal.app.test_request_context():
            return portal._safe_next(target)

    def test_accepte_un_chemin_local(self):
        self.assertEqual(self._resolve('/keys'), '/keys')

    def test_bloque_les_redirections_externes(self):
        for cible in ('https://evil.com', '//evil.com', 'http://evil.com',
                      '/\\evil.com', '/\tevil', '/\nevil'):
            self.assertEqual(self._resolve(cible), '/', cible)

    def test_cible_vide(self):
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

    def test_verrouille_apres_le_seuil(self):
        cle = 'ip-test|bob'
        for _ in range(portal.LOGIN_MAX_FAILS - 1):
            portal._login_fail(cle)
        self.assertEqual(portal._login_locked(cle), 0)
        portal._login_fail(cle)
        self.assertGreater(portal._login_locked(cle), 0)

    def test_persiste_en_base(self):
        portal._login_fail('ip-test|bob')
        row = portal.get_db().execute(
            "SELECT fails FROM login_attempts WHERE key='ip-test|bob'").fetchone()
        self.assertEqual(row['fails'], 1)

    def test_reset_efface_le_compteur(self):
        cle = 'ip-test|bob'
        for _ in range(portal.LOGIN_MAX_FAILS):
            portal._login_fail(cle)
        portal._login_reset(cle)
        self.assertEqual(portal._login_locked(cle), 0)

    def test_fenetre_glissante(self):
        cle = 'ip-test|bob'
        portal._login_fail(cle)
        # Backdates the first attempt beyond the window: the counter must
        # restart from zero rather than accumulate forever.
        portal.get_db().execute("UPDATE login_attempts SET first_at=? WHERE key=?",
                                (time.time() - portal.LOGIN_WINDOW - 10, cle))
        portal.get_db().commit()
        portal._login_fail(cle)
        row = portal.get_db().execute(
            "SELECT fails FROM login_attempts WHERE key=?", (cle,)).fetchone()
        self.assertEqual(row['fails'], 1)


class LoginLockoutParUserTest(unittest.TestCase):
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

    def test_identifiant_invalide_n_ecrit_rien_et_ne_verrouille_pas(self):
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
            self.assertIsNone(row, "rien ne doit être écrit pour un identifiant refusé")

    def test_botnet_ip_rotatives_verrouille_le_user(self):
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

    def test_apply_session_cree_le_registre(self):
        portal._apply_session('bob', 'Bob', False)
        self.assertIn('sid', portal.session)
        row = portal.get_db().execute(
            "SELECT username, revoked FROM user_sessions WHERE sid=?",
            (portal.session['sid'],)).fetchone()
        self.assertEqual(row['username'], 'bob')
        self.assertEqual(row['revoked'], 0)

    def test_session_valide_pas_expire(self):
        portal._apply_session('bob', 'Bob', False)
        self.assertFalse(portal._session_expired())

    def test_session_revoquee_expire(self):
        portal._apply_session('bob', 'Bob', False)
        portal._revoke_user_sessions('bob')
        self.assertTrue(portal._session_expired())

    def test_session_sans_sid_nest_pas_expire(self):
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

    def test_post_sans_jeton_refuse(self):
        self.assertEqual(self.client.post('/login', data={'username': 'x'}).status_code, 400)

    def test_post_avec_mauvais_jeton_refuse(self):
        r = self.client.post('/login', data={'username': 'x'},
                             headers={'X-CSRFToken': 'faux'})
        self.assertEqual(r.status_code, 400)

    def test_get_ne_demande_pas_de_jeton(self):
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


class McpToolNameTest(unittest.TestCase):
    """A hostile MCP server must not be able to mask a built-in tool."""

    def test_prefixe_toujours_present(self):
        self.assertTrue(assistance._mcp_tool_name(3, 'search').startswith('mcp_3_'))

    def test_pas_de_collision_avec_les_outils_integres(self):
        integres = {'create_api_key', 'revoke_api_key', 'request_budget',
                    'request_model', 'launch_model', 'stop_model', 'use_skill'}
        for nom in list(integres) + ['../create_api_key', 'create api key']:
            self.assertNotIn(assistance._mcp_tool_name(1, nom), integres)

    def test_caracteres_dangereux_neutralises(self):
        genere = assistance._mcp_tool_name(1, 'a b/c\\d"e')
        self.assertTrue(all(c.isalnum() or c in '_-' for c in genere), genere)


class CleanReplyTest(unittest.TestCase):
    def test_retire_le_bloc_de_raisonnement(self):
        self.assertEqual(assistance._clean_reply('<think>bla</think>Bonjour'), 'Bonjour')

    def test_garde_ce_qui_suit_le_marqueur_final(self):
        self.assertEqual(assistance._clean_reply('cheminement...\n### Réponse\nVoilà'), 'Voilà')

    def test_texte_simple_inchange(self):
        self.assertEqual(assistance._clean_reply('Bonjour'), 'Bonjour')


class SseFramingTest(unittest.TestCase):
    """The frontend only reads `data:` lines; the framing must stay exact."""

    def test_trame_texte(self):
        trame = chat._sse_text('salut')
        self.assertTrue(trame.startswith('data: '))
        self.assertTrue(trame.endswith('\n\n'))
        self.assertIn('salut', trame)

    def test_echappe_les_sauts_de_ligne(self):
        # A raw \n would cut the SSE frame in two and break the stream.
        self.assertNotIn('\n', chat._sse_text('a\nb')[:-2])

    def test_done_optionnel(self):
        self.assertIn('[DONE]', ''.join(chat._sse_chunks('x', done=True)))
        self.assertNotIn('[DONE]', ''.join(chat._sse_chunks('x', done=False)))
        # Structured notices share the switch: a mid-stream notice carries
        # ONLY its data line (the end path already sends the sentinel).
        self.assertIn('[DONE]', chat._sse_notice('x'))
        self.assertNotIn('[DONE]', chat._sse_notice('x', done=False))


class AvatarTest(unittest.TestCase):
    def test_liste_blanche_stricte(self):
        # The id lands in an <img> src: no free input.
        self.assertIn('claude', portal.AVATAR_IDS)
        self.assertNotIn('../../etc/passwd', portal.AVATAR_IDS)
        self.assertNotIn('avatar-01', portal.AVATAR_IDS)  # old set removed


if __name__ == '__main__':
    unittest.main()


class SessionLifetimeTest(unittest.TestCase):
    """A session must not be eternal: the signed cookie carries `is_admin`,
    a cookie theft otherwise gave permanent access."""

    def test_session_fraiche_valide(self):
        with portal.app.test_request_context():
            from flask import session
            session['username'] = 'bob'
            session['auth_at'] = time.time()
            self.assertFalse(portal._session_expired())

    def test_session_perimee(self):
        with portal.app.test_request_context():
            from flask import session
            session['username'] = 'bob'
            session['auth_at'] = time.time() - portal.SESSION_MAX_AGE - 1
            self.assertTrue(portal._session_expired())

    def test_session_sans_horodatage_est_perimee(self):
        # Sessions issued before auth_at was added: we prefer forcing a
        # re-login rather than treat them as eternal.
        with portal.app.test_request_context():
            from flask import session
            session['username'] = 'bob'
            self.assertTrue(portal._session_expired())

    def test_anonyme_non_concerne(self):
        with portal.app.test_request_context():
            self.assertFalse(portal._session_expired())


class GuardedToolsTest(unittest.TestCase):
    """The result of an MCP/skill tool is third-party text reinjected in the
    model's context: irreversible actions must be out of reach of a prompt
    injection."""

    def test_les_actions_destructives_sont_gardees(self):
        for nom in ('revoke_api_key', 'launch_model', 'stop_model'):
            self.assertIn(nom, assistance.GUARDED_TOOLS)

    def test_les_actions_inoffensives_ne_le_sont_pas(self):
        for nom in ('request_budget', 'request_model', 'list_models'):
            self.assertNotIn(nom, assistance.GUARDED_TOOLS)


class OidcUsernameTest(unittest.TestCase):
    """preferred_username/nickname/email are user-modifiable in many IdPs,
    and that value becomes the ownership key of all data: it must pass the
    same filter as the LDAP path."""

    def test_accepte_un_identifiant_normal(self):
        for nom in ('alice', 'jean.dupont', 'a-b_c', 'x' * 64):
            self.assertTrue(portal.USERNAME_RE.match(nom), nom)

    def test_rejette_les_identifiants_forges(self):
        for nom in ('', 'a' * 65, '../admin', 'bob@evil.com', 'bob bob',
                    'bob\nadmin', "bob'--", 'bob/../root'):
            self.assertIsNone(portal.USERNAME_RE.match(nom), nom)


class CsrfLazyTest(unittest.TestCase):
    """Regression: the token was created in before_request, so EVERY
    response set a Set-Cookie. On /login, /api/csrf and /api/whoami go out
    in parallel without a cookie: each created a fresh session with a
    different token, the last Set-Cookie overwrote the other, and the POST
    /login left with an orphan token → 400, displayed « Identifiants incorrects »."""

    def test_une_requete_anonyme_ne_cree_pas_de_session(self):
        client = portal.app.test_client()
        reponse = client.get('/api/whoami')
        self.assertEqual(reponse.status_code, 401)
        self.assertNotIn('Set-Cookie', reponse.headers)

    def test_api_csrf_cree_le_jeton_et_le_rend(self):
        client = portal.app.test_client()
        reponse = client.get('/api/csrf')
        self.assertEqual(reponse.status_code, 200)
        self.assertTrue(reponse.get_json()['token'])
        self.assertIn('Set-Cookie', reponse.headers)

    def test_le_jeton_est_stable_entre_deux_appels(self):
        client = portal.app.test_client()
        premier = client.get('/api/csrf').get_json()['token']
        # An interleaved request must not rotate the token.
        client.get('/api/whoami')
        self.assertEqual(client.get('/api/csrf').get_json()['token'], premier)

    def test_post_sans_session_refuse(self):
        client = portal.app.test_client()
        self.assertEqual(client.post('/logout', data={'csrf_token': 'inventé'}).status_code, 400)


class CsrfApresConnexionTest(unittest.TestCase):
    """Regression of 2026-09-14: opening a session REPLACED the CSRF token.

    The browser keeps the one it memorized at page load; from then on every
    kept POST left with the old one and got « 400 Bad Request —
    CSRF token manquant ou invalide ». Reported on the LOGOUT after an SSO
    login, i.e. the place where the user is left with no way out. The token
    lives in a signed and HttpOnly cookie: keeping it does not reopen CSRF
    fixation, and the fresh `sid` keeps assuring the protection against
    session fixation.
    """

    def test_le_jeton_du_client_survit_a_l_ouverture_de_session(self):
        from flask import session as session_flask

        with portal.app.test_request_context('/'):
            session_flask['csrf'] = 'jeton-que-le-navigateur-connait'
            auth._apply_session('demo', 'demo', False, via_sso=True)
            self.assertEqual(session_flask['csrf'], 'jeton-que-le-navigateur-connait')

    def test_un_jeton_est_cree_quand_il_en_manque(self):
        from flask import session as session_flask

        with portal.app.test_request_context('/'):
            session_flask.clear()
            auth._apply_session('demo', 'demo', False)
            self.assertTrue(session_flask.get('csrf'))

    def test_la_deconnexion_accepte_le_jeton_du_client(self):
        """The full path of the symptom: SSO session, client token, POST."""
        client = portal.app.test_client()
        with client.session_transaction() as s:
            s['csrf'] = 'jeton-du-client'
            s['username'] = 'demo'
            s['fullname'] = 'demo'
            s['is_admin'] = False
            s['sso'] = True
            s['auth_at'] = int(time.time())
        reponse = client.post('/logout', data={'csrf_token': 'jeton-du-client'})
        self.assertEqual(reponse.status_code, 302)

    def test_la_deconnexion_refuse_toujours_un_jeton_invente(self):
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

    def test_prefere_cf_connecting_ip(self):
        self.assertEqual(self._ip({'Cf-Connecting-Ip': '203.0.113.10',
                                   'X-Forwarded-For': '10.0.0.1, 172.19.0.5'}),
                         '203.0.113.10')

    def test_retombe_sur_le_dernier_x_forwarded_for(self):
        # The LAST element is the one OUR trusted hop appended; the first is
        # client-controlled and let a caller rotate a fake prefix to get a fresh
        # lockout key on every attempt (audit 2026-10-06, L2).
        self.assertEqual(self._ip({'X-Forwarded-For': '203.0.113.10, 172.19.0.5'}),
                         '172.19.0.5')

    def test_retombe_sur_remote_addr(self):
        self.assertEqual(self._ip({}), '172.19.0.5')

    def test_deux_visiteurs_ne_partagent_pas_le_meme_verrou(self):
        a = self._ip({'Cf-Connecting-Ip': '203.0.113.10'})
        b = self._ip({'Cf-Connecting-Ip': '203.0.113.20'})
        self.assertNotEqual(a, b)


class HistoriqueModeleTest(unittest.TestCase):
    """What the playground sends back to the model must NEVER be amputated.

    The original bug: each message was truncated to 8 000 characters, so
    after a long answer the model re-read its own file cut mid-way and
    claimed to have interrupted itself — true from its point of view.
    """

    def _msgs(self, *tailles):
        return [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'x' * n}
                for i, n in enumerate(tailles)]

    def test_un_gros_message_n_est_pas_tronque(self):
        h = self._msgs(50, 57_000)
        out = chat._history_for_model(h, '', 262144)
        self.assertEqual(len(out[-1]['content']), 57_000)

    def test_sans_contexte_connu_on_ne_touche_a_rien(self):
        h = self._msgs(10, 900_000)
        self.assertEqual(chat._history_for_model(h, '', None), h)

    def test_le_debordement_retire_les_plus_anciens(self):
        # budget = (32768 - 8192) * 3 = 73 728 characters
        h = self._msgs(40_000, 40_000, 40_000, 500)
        out = chat._history_for_model(h, '', 32768)
        self.assertLess(len(out), len(h))
        # the last exchange always survives, whole
        self.assertEqual(out[-1]['content'], h[-1]['content'])
        self.assertEqual(len(out[-2]['content']), 40_000)

    def test_jamais_moins_de_deux_messages(self):
        h = self._msgs(500_000, 500_000)
        out = chat._history_for_model(h, '', 32768)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(out[0]['content']), 500_000)

    def test_le_systeme_compte_dans_le_budget(self):
        h = self._msgs(30_000, 30_000, 30_000, 100)
        sans = chat._history_for_model(list(h), '', 32768)
        avec = chat._history_for_model(list(h), 'y' * 20_000, 32768)
        self.assertLessEqual(len(avec), len(sans))


class ContexteOutilsTest(unittest.TestCase):
    """The tool phase re-reads a SHORT version of the conversation.

    Passing it the whole history added a full preload before the answer:
    measured 30 s on 100 Ko of context, over 60 s beyond — and the client
    gave up on a slightly old conversation, while a fresh one worked.
    Deciding « faut-il chercher ? » does not call for re-reading a 65 Ko
    file.
    """

    def test_un_gros_message_est_raccourci(self):
        msgs = [{'role': 'user', 'content': 'x' * 65_000},
                {'role': 'user', 'content': 'et maintenant ?'}]
        out = outils_web._contexte_outils(msgs)
        self.assertTrue(all(len(m['content']) <= outils_web.OUTILS_MSG_MAX + 8 for m in out))

    def test_le_total_reste_borne(self):
        msgs = [{'role': 'user', 'content': 'y' * 20_000} for _ in range(20)]
        out = outils_web._contexte_outils(msgs)
        self.assertLessEqual(sum(len(m['content']) for m in out),
                             outils_web.OUTILS_TOTAL_MAX + outils_web.OUTILS_MSG_MAX)

    def test_le_dernier_message_est_toujours_la(self):
        msgs = [{'role': 'user', 'content': 'z' * 30_000} for _ in range(10)]
        msgs.append({'role': 'user', 'content': 'CE QUE JE DEMANDE'})
        out = outils_web._contexte_outils(msgs)
        self.assertIn('CE QUE JE DEMANDE', out[-1]['content'])

    def test_le_systeme_est_conserve_en_tete(self):
        msgs = [{'role': 'system', 'content': 'consignes'},
                {'role': 'user', 'content': 'bonjour'}]
        out = outils_web._contexte_outils(msgs)
        self.assertEqual(out[0]['role'], 'system')
        self.assertIn('consignes', out[0]['content'])

    def test_debut_et_fin_conserves_dans_un_message_coupe(self):
        # The request is often at the top, the last instruction at the tail: it is
        # the belly of the file that teaches nothing.
        msgs = [{'role': 'user', 'content': 'DEBUT' + 'm' * 60_000 + 'FIN'}]
        out = outils_web._contexte_outils(msgs)
        self.assertIn('DEBUT', out[-1]['content'])
        self.assertIn('FIN', out[-1]['content'])

    def test_conversation_courte_passe_telle_quelle(self):
        msgs = [{'role': 'user', 'content': 'salut'},
                {'role': 'assistant', 'content': 'bonjour'}]
        self.assertEqual([m['content'] for m in outils_web._contexte_outils(msgs)],
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

    def test_les_faux_declencheurs_historiques(self):
        for t in ("j'aurais besoin que tu regardes dans ce fichier, "
                  "c'est un jeu d'échecs en ligne, sauf que j'ai un problème",
                  "fais-moi un jeu d'échecs en ligne",
                  "tu peux me faire une dissertation sur la propagation du son",
                  "hello comment vas-tu ?",
                  '<link rel="preconnect" href="https://fonts.googleapis.com">',
                  "const source = ctx.createBufferSource();",
                  "quelles sont les dernières nouvelles ?",
                  "quel est le prix du bitcoin ?"):
            self.assertFalse(outils_web._recherche_pertinente(self._u(t)), t)

    def test_les_directives_explicites(self):
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
            self.assertTrue(outils_web._recherche_pertinente(self._u(t)), t)

    def test_mettre_en_ligne_ne_declenche_pas(self):
        # 2026-09: « en ligne » describes a state; « je cherche à mettre mon
        # site en ligne » opened a web search — that is over.
        for t in ("je cherche à mettre mon site en ligne",
                  "regarde pourquoi mon serveur n'est plus en ligne",
                  "fais-moi un jeu d'échecs en ligne"):
            self.assertFalse(outils_web._recherche_pertinente(self._u(t)), t)

    def test_la_directive_marche_meme_avec_un_fichier_colle(self):
        h = [{'role': 'user', 'content': "```html\n" + ("x" * 5000)
              + "\n```\n\ncherche sur internet la doc de cette balise"}]
        self.assertTrue(outils_web._recherche_pertinente(h))

    def test_conversation_vide(self):
        self.assertFalse(outils_web._recherche_pertinente([]))


class VersionsPerimeesTest(unittest.TestCase):
    """Only the last version of each file goes back to the model.

    Measured on real conversations: 42 332 of the 72 182 characters of a
    thread were old versions of the same file, replayed at every message.
    """

    def _msg(self, role, contenu):
        return {'role': role, 'content': contenu}

    def _fichier(self, nom, marqueur, n=3000):
        return f"Voici `{nom}` :\n\n```html\n<!-- {marqueur} -->\n" + ("x" * n) + "\n```"

    def test_seule_la_derniere_version_survit(self):
        h = [self._msg('user', 'fais un jeu'),
             self._msg('assistant', self._fichier('index.html', 'V1')),
             self._msg('user', 'corrige'),
             self._msg('assistant', self._fichier('index.html', 'V2'))]
        out = chat._sans_versions_perimees(h)
        self.assertNotIn('V1', out[1]['content'])
        self.assertIn('version précédente', out[1]['content'])
        self.assertIn('V2', out[3]['content'])

    def test_deux_fichiers_distincts_gardent_chacun_leur_version(self):
        h = [self._msg('assistant', self._fichier('index.html', 'HTML1')),
             self._msg('assistant', self._fichier('style.css', 'CSS1'))]
        out = chat._sans_versions_perimees(h)
        self.assertIn('HTML1', out[0]['content'])
        self.assertIn('CSS1', out[1]['content'])

    def test_le_code_colle_par_l_utilisateur_n_est_jamais_touche(self):
        colle = "```html\n<!-- COLLE -->\n" + ("y" * 3000) + "\n```"
        h = [self._msg('user', colle),
             self._msg('assistant', self._fichier('index.html', 'V1'))]
        out = chat._sans_versions_perimees(h)
        self.assertIn('COLLE', out[0]['content'])

    def test_un_court_extrait_ne_perime_rien(self):
        h = [self._msg('assistant', self._fichier('index.html', 'V1')),
             self._msg('assistant', "Regarde :\n\n```js\nconst a = 1;\n```")]
        out = chat._sans_versions_perimees(h)
        self.assertIn('V1', out[0]['content'])

    def test_la_prose_autour_du_bloc_est_conservee(self):
        h = [self._msg('assistant', self._fichier('index.html', 'V1') + "\n\nJ'ai ajouté le son."),
             self._msg('assistant', self._fichier('index.html', 'V2'))]
        out = chat._sans_versions_perimees(h)
        self.assertIn("J'ai ajouté le son.", out[0]['content'])

    def test_le_gain_est_reel(self):
        h = [self._msg('assistant', self._fichier('index.html', f'V{i}', 20000)) for i in range(4)]
        avant = sum(len(m['content']) for m in h)
        apres = sum(len(m['content']) for m in chat._sans_versions_perimees(h))
        self.assertLess(apres, avant * 0.4)

    def test_conversation_sans_code_inchangee(self):
        h = [self._msg('user', 'bonjour'), self._msg('assistant', 'salut')]
        self.assertEqual(chat._sans_versions_perimees(h), h)


class TrouvaillesTest(unittest.TestCase):
    """What the search brings back is reinjected as TEXT, never as a `tool` role.

    Sending `tool_calls` and `tool`-role messages without declaring the
    tools gave a conversation the template cannot render: 35 tokens
    produced, no content received, « The model returned no response ».
    """

    def test_rien_trouve_rien_ajoute(self):
        self.assertEqual(outils_web._texte_des_trouvailles([]), '')

    def test_le_contenu_est_repris_et_cadre_comme_externe(self):
        t = outils_web._texte_des_trouvailles([('recherche_web', '{"resultats": [{"url": "https://x.fr"}]}')])
        self.assertIn('https://x.fr', t)
        self.assertIn('externes', t)

    def test_le_texte_reste_borne(self):
        t = outils_web._texte_des_trouvailles([('lire_pages', 'x' * 200_000)])
        self.assertLessEqual(len(t), 40_000)

    def test_plusieurs_appels_sont_tous_repris(self):
        t = outils_web._texte_des_trouvailles([('recherche_web', 'AAA'), ('lire_pages', 'BBB')])
        self.assertIn('AAA', t)
        self.assertIn('BBB', t)


class GardeDesRoutesTest(unittest.TestCase):
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
    PUBLIQUES = {
        'api_config':         "ne renvoie que {oidc_enabled}, lu avant connexion",
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
        'healthz':            "liveness publique (healthcheck / sonde) : ne renvoie "
                              "que {ok, time}, rien d'interné",
        'prom_metrics':       "métriques Prometheus (texte) pour Grafana : publique "
                              "par choix (pull), mais REFUSÉE (403) dès que la requête "
                              "traverse l'edge Cloudflare (en-tête Cf-Connecting-Ip) — "
                              "cf. test_metrics_refuse_via_cloudflare",
        'conversations.share_view': "vue publique, lecture seule, d'une conversation "
                                    "partagée (jeton opaque) : contenu échappé",
    }

    def test_aucune_route_sans_garde_hors_liste(self):
        sans_garde = set()
        for regle in portal.app.url_map.iter_rules():
            vue = portal.app.view_functions.get(regle.endpoint)
            if vue is None or getattr(vue, '_garde', None):
                continue
            sans_garde.add(regle.endpoint)
        nouvelles = sans_garde - set(self.PUBLIQUES)
        self.assertEqual(nouvelles, set(),
                         "route(s) sans login_required/admin_required : "
                         f"{sorted(nouvelles)} — publier une route est un choix, "
                         "pas un défaut : documente-la dans PUBLIQUES ou ajoute une garde.")

    def test_la_liste_des_publiques_ne_pourrit_pas(self):
        """An entry matching no route anymore must disappear."""
        connues = {r.endpoint for r in portal.app.url_map.iter_rules()}
        self.assertEqual(set(self.PUBLIQUES) - connues, set(),
                         "entrée(s) obsolète(s) dans PUBLIQUES")

    def test_le_marqueur_est_bien_pose(self):
        """Without a marker, the main test would pass while seeing nothing."""
        gardees = [r.endpoint for r in portal.app.url_map.iter_rules()
                   if getattr(portal.app.view_functions.get(r.endpoint), '_garde', None)]
        self.assertGreater(len(gardees), 90, "le marqueur _garde a disparu des décorateurs")


class NomsResolublesTest(unittest.TestCase):
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
        'image_routes', 'music_routes', 'voice_routes', 'asr_routes', 'ocr_routes',
    ]

    def _noms_non_resolus(self, chemin):
        arbre = ast.parse(io.open(chemin, encoding='utf-8').read())
        connus = set(dir(builtins)) | {'__file__', '__name__', '__doc__'}
        for n in ast.walk(arbre):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                connus.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    connus.add((a.asname or a.name).split('.')[0])
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                connus.add(n.id)
            elif isinstance(n, ast.arg):
                connus.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                connus.add(n.name)
            elif isinstance(n, ast.Global):
                connus.update(n.names)
        return sorted({n.id for n in ast.walk(arbre)
                       if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                       and n.id not in connus})

    def test_chaque_module_resout_tous_ses_noms(self):
        racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for nom in self.MODULES:
            chemin = os.path.join(racine, f'{nom}.py')
            if not os.path.exists(chemin):
                continue
            with self.subTest(module=nom):
                self.assertEqual(
                    self._noms_non_resolus(chemin), [],
                    f"{nom}.py charge un nom défini nulle part — il lèvera un "
                    "NameError à l'appel. Réimporte-le depuis le module qui le "
                    "définit désormais.")


class HealthRouteTest(unittest.TestCase):
    """/healthz (public liveness) and /api/health (aggregated state, logged-in)."""

    def setUp(self):
        portal.app.config["TESTING"] = True

    def test_healthz_publique_renvoie_ok(self):
        c = portal.app.test_client()
        r = c.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_api_health_requiert_session(self):
        c = portal.app.test_client()
        r = c.get("/api/health")
        self.assertEqual(r.status_code, 401)

    def test_api_health_rend_l_etat_des_services(self):
        import unittest.mock as mock
        with mock.patch.object(portal, "_service_reachable", return_value=False), \
                mock.patch.object(portal, "get_running_models", return_value=[]), \
                mock.patch.object(portal, "comfyui_is_up", return_value=False), \
                mock.patch.object(portal, "get_ocr_model", return_value=None), \
                mock.patch.object(portal, "get_voice_model", return_value=None), \
                mock.patch.object(portal, "image_ready", return_value=False), \
                mock.patch.object(portal, "music_ready", return_value=False):
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

    def test_litellm_joignable_meme_quand_health_exige_une_cle(self):
        """LiteLLM >= 1.102 answers 401 on `/health` (key required): the portal
        must not conclude the proxy is down. Regression of 2026-09-24 — the
        upgrade 1.92 → 1.102.1 made « LiteLLM injoignable » display on the
        dashboard while the proxy served requests."""
        import unittest.mock as mock

        class Reponse:
            def __init__(self, code):
                self.status_code = code

        def faux_get(url, timeout=None):
            if url.endswith("/health"):          # 1.102: authenticated
                return Reponse(401)
            if url.endswith("/health/liveliness"):  # public, the real probe
                return Reponse(200)
            return Reponse(200)                   # runner /status, etc.

        with mock.patch.object(portal.requests, "get", side_effect=faux_get), \
                mock.patch.object(portal, "get_running_models", return_value=["m"]), \
                mock.patch.object(portal, "comfyui_is_up", return_value=False), \
                mock.patch.object(portal, "get_ocr_model", return_value=None), \
                mock.patch.object(portal, "get_voice_model", return_value=None), \
                mock.patch.object(portal, "image_ready", return_value=False), \
                mock.patch.object(portal, "music_ready", return_value=False):
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

    def test_requiert_session(self):
        c = portal.app.test_client()
        self.assertEqual(c.get("/api/pending-count").status_code, 401)

    def test_compte_les_demandes_en_attente(self):
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


class AnnonceModeleSupprimeTest(unittest.TestCase):
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

    def test_suppression_retire_aussi_l_annonce(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "boss"
            s["auth_at"] = int(time.time())
            s["is_admin"] = True
            s["csrf"] = "test-csrf"
        with patch.object(admin_routes, '_unregister_litellm_model', return_value=True), \
             patch.object(admin_routes, 'runner_delete_files', return_value=(True, 0, '')):
            r = c.post(f"/admin/model/delete/{self.mid}",
                       headers={'X-CSRFToken': 'test-csrf'})
        # JSON contract: no more redirect. The `flash` was rendered by no
        # template and the redirect HTML response counted as a success on
        # the UI side (its `catch` treated non-JSON as « ok »).
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        with portal.app.app_context():
            db = portal.get_db()
            annonces = db.execute("SELECT COUNT(*) FROM announcements "
                                  "WHERE kind='model_add' AND a='ztest-modele'").fetchone()[0]
            configs = db.execute("SELECT COUNT(*) FROM model_configs WHERE id=?",
                                 (self.mid,)).fetchone()[0]
            trace = db.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0]
        self.assertEqual(annonces, 0)
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

    def test_approve_temporaire_creer_subvention(self):
        """Approve with duration: boosted cap + budget_grants row."""
        with patch.object(admin_routes, '_litellm_user_info', return_value={'max_budget': 200000000}), \
             patch.object(admin_routes, 'litellm_update_user_budget', return_value=True) as up:
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

    def test_expiration_revient_a_la_base(self):
        """At expiry: the account is brought back to the base cap."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) "
                "VALUES ('demo', 200000000, 250000000, '2020-01-01T00:00:00', '2020-01-01', '2020-01-01')")
            db.commit()
        with portal.app.app_context(), \
             patch.object(admin_routes, '_litellm_user_info', return_value={'max_budget': 250000000}), \
             patch.object(admin_routes, 'litellm_update_user_budget', return_value=True) as up:
            ramenes = admin_routes.revert_expired_grants()
        self.assertEqual(ramenes, 1)
        args = up.call_args
        self.assertEqual(args.args[1], 200000000)          # …to the base, nothing else
        with portal.app.app_context():
            self.assertIsNone(portal.get_db().execute("SELECT * FROM budget_grants").fetchone())

    def test_expiration_necrase_pas_une_decision_admin(self):
        """If the admin redefined the cap meanwhile, we do not overwrite."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) "
                "VALUES ('demo', 200000000, 250000000, '2020-01-01T00:00:00', '2020-01-01', '2020-01-01')")
            db.commit()
        with portal.app.app_context(), \
             patch.object(admin_routes, '_litellm_user_info', return_value={'max_budget': 50000000}), \
             patch.object(admin_routes, 'litellm_update_user_budget', return_value=True) as up:
            ramenes = admin_routes.revert_expired_grants()
        self.assertEqual(ramenes, 0)
        up.assert_not_called()                             # the admin decision is authoritative

    def test_redefinir_le_plafond_a_la_baisse(self):
        """/budget/set sets an EXACT amount: 200M → 50M, and purges the boosts."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO budget_grants (username, base_budget, current_budget, expires_at, created_at, updated_at) "
                "VALUES ('demo', 200000000, 250000000, '2099-01-01T00:00:00', '2025-01-01', '2025-01-01')")
            db.commit()
        with patch.object(admin_routes, 'litellm_update_user_budget', return_value=True) as up:
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/admin/users/demo/budget/set", data={"budget": "50000000", "csrf_token": "test-csrf"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()['ok'])
        args = up.call_args
        self.assertEqual(args.args[1], 50000000)           # exact amount, not +
        with portal.app.app_context():
            self.assertIsNone(portal.get_db().execute("SELECT * FROM budget_grants").fetchone())

    def test_redefinir_refuse_les_montants_irrealistes(self):
        with patch.object(admin_routes, 'litellm_update_user_budget', return_value=True) as up:
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

    def test_budget_remaining_utilise_les_tokens_reels(self):
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

    def test_metriques_publiques_sur_le_lan(self):
        c = portal.app.test_client()
        r = c.get("/metrics")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("cronos_cpu_pct", body)
        self.assertIn("cronos_model_online", body)
        self.assertIn("cronos_gpu_util_pct", body)

    def test_metrics_refuse_via_cloudflare(self):
        """Cloudflare sets Cf-Connecting-Ip on EVERY request crossing the
        edge: its presence proves the call comes from the internet
        (dgx.cronos.website), not the LAN/netbird, so we serve no host metrics."""
        c = portal.app.test_client()
        r = c.get("/metrics", headers={"Cf-Connecting-Ip": "203.0.113.7"})
        self.assertEqual(r.status_code, 403)
        body = r.get_data(as_text=True)
        self.assertNotIn("cronos_cpu_pct", body)
        self.assertNotIn("cronos_gpu_util_pct", body)


class MediaCancelTest(unittest.TestCase):
    """Cancellation of an image/music/video generation (« Arrêter » button)."""

    CSRF = "test-csrf"

    def setUp(self):
        portal.app.config["TESTING"] = True
        with portal.app.app_context():
            db = portal.get_db()
            for t in ("image_jobs", "music_jobs", "video_jobs"):
                db.execute(f"DELETE FROM {t}")
            db.commit()

    def _login(self, c, username="demo"):
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF

    def _headers(self):
        return {"X-CSRFToken": self.CSRF}

    def test_post_sans_jeton_refuse(self):
        # before_request checks the CSRF first thing (defence in depth).
        c = portal.app.test_client(); self._login(c)
        self.assertEqual(c.post("/api/image/cancel/p1").status_code, 400)

    def test_img_cancel_inconnu_404(self):
        c = portal.app.test_client(); self._login(c)
        self.assertEqual(c.post("/api/image/cancel/inconnu", headers=self._headers()).status_code, 404)

    def test_img_cancel_noop_si_deja_fini(self):
        with portal.app.app_context():
            portal.get_db().execute(
                "INSERT INTO image_jobs (username,prompt_id,prompt,status,count,done_count,created_at) "
                "VALUES ('demo','p1','x','done',1,1,'2025-01-01')")
            portal.get_db().commit()
        c = portal.app.test_client(); self._login(c)
        r = c.post("/api/image/cancel/p1", headers=self._headers())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_img_cancel_annule_un_job_en_cours(self):
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

    def test_music_cancel_annule(self):
        with portal.app.app_context():
            portal.get_db().execute(
                "INSERT INTO music_jobs (username,job_id,prompt,status,count,done_count,created_at,duration_ms) "
                "VALUES ('demo','m1','x','running',3,0,'2025-01-01',NULL)")
            portal.get_db().commit()
        c = portal.app.test_client(); self._login(c)
        self.assertTrue(c.post("/api/music/cancel/m1", headers=self._headers()).get_json()["ok"])
        with portal.app.app_context():
            st = portal.get_db().execute(
                "SELECT status FROM music_jobs WHERE job_id='m1'").fetchone()["status"]
        self.assertEqual(st, "cancelled")

    def test_video_cancel_annule(self):
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

    def test_post_sans_jeton_refuse(self):
        # No session → before_request answers 400 (missing token) before any
        # other consideration: we never create an anonymous share.
        self.assertEqual(
            portal.app.test_client().post("/conversations/share", data={"client_id": "x"}).status_code,
            400)

    def test_partage_puis_vue_lecture_seule(self):
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

    def test_partage_accepte_le_json(self):
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

    def test_vue_anonyme_renvoie_vers_login(self):
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

    def test_vue_inconnue_404(self):
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

    def test_audit_sans_session_est_refuse(self):
        c = portal.app.test_client()
        self.assertIn(c.get("/admin/audit").status_code, (302, 401, 403))

    def test_log_puis_liste(self):
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

    def test_liste_et_compteur(self):
        from db import add_notification
        add_notification("alice", "image", "Génération image terminée (2/2).")
        add_notification("alice", "request", "Budget accordé : +100 tokens.")
        c = self._login()
        data = c.get("/api/notifications").get_json()
        self.assertEqual(data["unread"], 2)
        self.assertEqual(len(data["items"]), 2)
        self.assertTrue(all(not i["seen"] for i in data["items"]))

    def test_marquage_lu(self):
        from db import add_notification
        add_notification("alice", "image", "Génération image terminée (1/1).")
        c = self._login()
        self.assertEqual(c.get("/api/notifications").get_json()["unread"], 1)
        r = c.post("/api/notifications/seen", headers={"X-CSRFToken": "test-csrf"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/notifications").get_json()["unread"], 0)

    def test_sans_session_est_refuse(self):
        c = portal.app.test_client()
        self.assertIn(c.get("/api/notifications").status_code, (302, 401, 403))
        # POST without CSRF → 400 (before_request CSRF before login_required).
        self.assertEqual(c.post("/api/notifications/seen").status_code, 400)

    def test_docs_requiert_login(self):
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

    def test_routes_enregistrees(self):
        self.assertIn('/api/playground/title', self._paths())
        self.assertIn('/api/playground/summarize', self._paths())

    def test_post_sans_csrf_refuse(self):
        c = portal.app.test_client()
        # before_request CSRF runs before login_required → 400 on POST.
        self.assertEqual(c.post('/api/playground/title').status_code, 400)
        self.assertEqual(c.post('/api/playground/summarize').status_code, 400)

    def test_sans_session_refuse(self):
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

    def test_titre_genere(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "_non_stream", return_value=("Titre court", None)):
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/api/playground/title", headers={"X-CSRFToken": self.CSRF},
                       json={"model": "fake-model", "messages": [{"role": "user", "content": "Bonjour"}]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["title"], "Titre court")

    def test_titre_sans_modele_409(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=[]):
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/api/playground/title", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "user", "content": "Bonjour"}]})
            self.assertEqual(r.status_code, 409)

    def test_titre_vide_retourne_vide(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "_non_stream", return_value=("", None)):
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/api/playground/title", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "user", "content": "Salut"}]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["title"], "")

    def test_summary_genere(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "_non_stream", return_value=("Résumé du contexte", None)):
            c = portal.app.test_client()
            self._login(c)
            r = c.post("/api/playground/summarize", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "assistant", "content": "Réponse"}]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["summary"], "Résumé du contexte")

    def test_maintenance_arrete_le_titre_et_le_resume(self):
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

    def test_plafond_de_debit_s_applique_au_titre(self):
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

    def test_support_utilise_la_cle_de_l_utilisateur(self):
        import unittest.mock as mock
        vus = []

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            vus.append(headers or {})
            return self._FakeStream()

        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             mock.patch.object(chat, "quota_depasse_reset", return_value=None), \
             mock.patch.object(chat, "_fin_support"), \
             mock.patch.object(chat.requests, "post", side_effect=_post):
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "Salut"}]})
            self.assertEqual(r.status_code, 200)
            r.get_data(as_text=True)      # forces the execution of the SSE generator
        self.assertTrue(vus, "aucun appel au modèle n'a été fait")
        self.assertEqual(vus[0].get("Authorization"), "Bearer sk-user-123")

    def test_support_sans_cle_notifie_et_n_appelle_pas_le_modele(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[]), \
             mock.patch.object(chat, "_fin_support"), \
             mock.patch.object(chat.requests, "post", side_effect=AssertionError("appel modèle interdit")):
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "Salut"}]})
            self.assertEqual(r.status_code, 200)
            self.assertIn("no_api_key", r.get_data(as_text=True))

    def test_support_quota_depasse_notifie(self):
        import unittest.mock as mock
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             mock.patch.object(chat, "quota_depasse_reset", return_value=3600), \
             mock.patch.object(chat, "_fin_support"), \
             mock.patch.object(chat.requests, "post", side_effect=AssertionError("appel modèle interdit")):
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "Salut"}]})
            self.assertEqual(r.status_code, 200)
            body = r.get_data(as_text=True)
            self.assertIn("quota_exceeded", body)
            self.assertIn("3600", body)


class SupportNoticesStructureesTest(unittest.TestCase):
    """The Support chat SSE ERROR texts are STRUCTURED notices (`cronos_notice`):
    the server sends an id (+ `wait`/`status` arguments), the frontend writes
    the sentence. The ids are a CONTRACT with `texteNotice` (an unknown id
    displays as raw id), so each one is locked byte-identical here.

    Tool labels and tool results are not error texts and stay as they are.
    """

    CSRF = "test-csrf"

    class _Flux:
        """Fake upstream stream: a list of frames, or an error to raise."""

        def __init__(self, frames, ok=True, status_code=200, erreur=None):
            self.frames = list(frames)
            self.ok = ok
            self.status_code = status_code
            self.erreur = erreur

        def close(self):
            pass

        def iter_lines(self, decode_unicode=False):
            if self.erreur is not None:
                raise self.erreur
            yield from self.frames

    @staticmethod
    def _frame_texte(t):
        return "data: " + json.dumps({"choices": [{"delta": {"content": t}}]})

    @staticmethod
    def _frame_outil(name, args, call_id="call_1"):
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
    def _notices(corps):
        """The notice payloads of the stream (`[DONE]` and tool events aside)."""
        out = []
        for ligne in corps.splitlines():
            if ligne.startswith("data: ") and ligne != "data: [DONE]":
                payload = json.loads(ligne[6:])
                if "cronos_notice" in payload:
                    out.append(payload)
        return out

    def _flux(self, messages=(("user", "Salut"),), running=("fake-model",), wait=0,
              post=None):
        """Runs /support/chat and returns (status code, SSE body)."""
        msgs = [{"role": role, "content": content} for role, content in messages]
        with patch.object(chat, "get_running_models", return_value=list(running)), \
             patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             patch.object(chat, "quota_depasse_reset", return_value=None), \
             patch.object(chat, "_chat_rate_limited", return_value=wait), \
             patch.object(chat, "_user_extra_tools", return_value=([], {})), \
             patch.object(chat, "_exec_support_tool", return_value=("ok", True)), \
             patch.object(chat, "_fin_support"), \
             patch.object(chat.requests, "post",
                          side_effect=post or AssertionError("appel interdit")):
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": msgs})
            return r.status_code, r.get_data(as_text=True)

    def test_message_vide_notice(self):
        code, corps = self._flux(messages=())
        self.assertEqual(code, 400)
        self.assertEqual(self._notices(corps),
                         [{"cronos_notice": {"id": "empty_message"}}])

    def test_plafond_attente_notice(self):
        code, corps = self._flux(wait=7)
        self.assertEqual(self._notices(corps),
                         [{"cronos_notice": {"id": "chat_rate_limited", "wait": 7}}])
        self.assertNotIn("Trop de", corps)

    def test_aucun_modele_notice(self):
        code, corps = self._flux(running=())
        self.assertEqual(self._notices(corps),
                         [{"cronos_notice": {"id": "no_model_running"}}])

    def test_amont_injoignable_notice(self):
        import requests as _rq
        code, corps = self._flux(post=_rq.exceptions.ConnectionError("coupé"))
        self.assertEqual(self._notices(corps),
                         [{"cronos_notice": {"id": "model_unreachable"}}])
        self.assertIn("data: [DONE]", corps)

    def test_amont_en_erreur_notice_statut(self):
        code, corps = self._flux(
            post=lambda *a, **k: self._Flux([], ok=False, status_code=500))
        self.assertEqual(self._notices(corps),
                         [{"cronos_notice": {"id": "model_replied_error",
                                             "status": 500}}])

    def test_reponse_vide_notice(self):
        code, corps = self._flux(post=lambda *a, **k: self._Flux(["data: [DONE]"]))
        self.assertEqual(self._notices(corps),
                         [{"cronos_notice": {"id": "empty_reply"}}])

    def test_lecture_timeout_notice(self):
        import requests as _rq
        code, corps = self._flux(
            post=lambda *a, **k: self._Flux([], erreur=_rq.exceptions.ReadTimeout()))
        self.assertEqual(self._notices(corps),
                         [{"cronos_notice": {"id": "model_timeout"}}])

    def _boucle_d_outils(self, final):
        """4 tool rounds, then the forced final turn served by `final`."""
        appels = {"n": 0}
        outil = self._Flux([self._frame_outil("request_budget", {"alias": "a"}),
                            "data: [DONE]"])

        def _post(*a, **k):
            appels["n"] += 1
            return outil if appels["n"] <= 4 else final
        return self._flux(post=_post)

    @staticmethod
    def _frame_raisonnement(t):
        """A thinking frame, as LiteLLM produces it (`reasoning_content`)."""
        return "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": t}}]})

    def test_le_raisonnement_est_relaie(self):
        """The model THINKS (adaptive, like the playground): what it produces is
        generated, so billed — and it was dropped by this relay. It must reach
        the client in `reasoning_content`, the same contract the playground
        uses, so the same block can render it."""
        # ASCII only: `json.dumps` escapes accents (je p\u00e8se…), and this
        # test is about the FRAME SHAPE, not about the encoding.
        frames = [self._frame_raisonnement("je pese les options"),
                  self._frame_texte("la reponse"),
                  "data: [DONE]"]
        code, corps = self._flux(post=lambda *a, **k: self._Flux(frames))
        self.assertEqual(code, 200)
        self.assertIn('"reasoning_content": "je pese les options"', corps)
        self.assertIn('"content": "la reponse"', corps)

    def test_la_balise_de_pensee_dans_le_contenu_reste_masquee(self):
        """A think tag COUPLED INTO the content is a parasite, not a structured
        reasoning: it stays hidden while the answer streams."""
        def _c(t):
            return "data: " + json.dumps({"choices": [{"delta": {"content": t}}]})

        frames = [_c("<think>"), _c("mon raisonnement interne"),
                  _c("</think>"), _c("la reponse finale"), "data: [DONE]"]
        code, corps = self._flux(post=lambda *a, **k: self._Flux(frames))
        self.assertEqual(code, 200)
        self.assertNotIn("mon raisonnement interne", corps)
        self.assertIn("la reponse finale", corps)

    def test_modele_occupe_apres_boucle_d_outils(self):
        code, corps = self._boucle_d_outils(self._Flux([], ok=False, status_code=503))
        self.assertEqual(self._notices(corps),
                         [{"cronos_notice": {"id": "model_busy"}}])

    def test_reformulation_apres_boucle_d_outils(self):
        code, corps = self._boucle_d_outils(self._Flux(["data: [DONE]"]))
        self.assertEqual(self._notices(corps),
                         [{"cronos_notice": {"id": "reformulate"}}])


class SupportServicesTest(unittest.TestCase):
    """The Support can SEE the platform's services and RELAUNCH one (« je suis
    chaud que il puisse regarder si tout fonctionne et que si un service est
    stop il puisse relancer »).

    The state comes from facts (`_sidecar_status`), never from the model's
    word, and a relaunch is a SENSITIVE action: same confirmation flow as
    launching a model, and admin-only.
    """

    def test_le_service_est_dans_le_contexte(self):
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

    def test_loutil_n_est_propose_qu_aux_admins(self):
        noms_admin = {t["function"]["name"] for t in assistance._support_tools(True)}
        noms = {t["function"]["name"] for t in assistance._support_tools(False)}
        self.assertIn("manage_service", noms_admin)
        self.assertNotIn("manage_service", noms)

    def test_loutil_est_garde_pour_la_confirmation(self):
        """manage_service is guarded: the model only SUBMITS it."""
        self.assertIn("manage_service", assistance.GUARDED_TOOLS)
        self.assertIn("manage_service", assistance.OUTILS_REFUSES_SI_EXTERNE)

    def _exec(self, args, is_admin):
        with patch.object(assistance, "get_db"):
            return assistance._exec_support_tool(
                "manage_service", args, "demo", "Demo", is_admin)

    def test_execution_reservee_aux_admins(self):
        res, ok = self._exec({"service": "ocr", "action": "start"}, False)
        self.assertFalse(ok)
        self.assertIn("administrateurs", res)

    def test_demarrage_renvoie_le_nouvel_etat(self):
        with patch.object(assistance, "_sidecar_action", return_value=(True, "")) as act, \
             patch.object(assistance, "_sidecar_status", return_value="starting"), \
             patch.object(assistance, "_mem_guard", return_value=None):
            res, ok = self._exec({"service": "ocr", "action": "start"}, True)
        self.assertTrue(ok)
        self.assertIn("ocr : starting", res)
        act.assert_called_once_with("ocr", "start")

    def test_relancaison_arrete_puis_redemarre(self):
        with patch.object(assistance, "_sidecar_action", return_value=(True, "")) as act, \
             patch.object(assistance, "_sidecar_status", return_value="running"), \
             patch.object(assistance, "_mem_guard", return_value=None):
            res, ok = self._exec({"service": "asr", "action": "restart"}, True)
        self.assertTrue(ok)
        self.assertEqual([c.args for c in act.call_args_list],
                         [("asr", "stop"), ("asr", "start")])

    def test_la_garde_memoire_bloque_le_demarrage(self):
        """On unified memory a sidecar that overflows takes the chat model with
        it: the guard refuses BEFORE the start."""
        with patch.object(assistance, "_mem_guard", return_value="Mémoire insuffisante"), \
             patch.object(assistance, "_sidecar_action") as act:
            res, ok = self._exec({"service": "image", "action": "start"}, True)
        self.assertFalse(ok)
        self.assertIn("Mémoire insuffisante", res)
        act.assert_not_called()

    def test_les_logs_se_lisent_a_la_demande(self):
        """`read_logs` pulls the tail of a service — the logs are NOT in the
        prompt (« pas que dans le system prompt »)."""
        with patch.object(assistance, "get_db"), \
             patch.object(assistance, "sidecar_logs",
                          return_value=["erreur: poids introuvable", "fin"]) as lire:
            res, ok = assistance._exec_support_tool(
                "read_logs", {"service": "asr", "lines": 50}, "demo", "Demo", True)
        self.assertTrue(ok)
        self.assertIn("erreur: poids introuvable", res)
        lire.assert_called_once_with("asr", 50)

    def test_les_logs_de_litellm_se_lisent_sans_le_runner(self):
        """The proxy writes to the SHARED volume: its logs are read from the
        file, no runner involved (a runner restart would kill the model)."""
        with patch.object(assistance, "get_db"), \
             patch("sidecars.open", create=True) as ouvrir:
            ouvrir.side_effect = FileNotFoundError
            res, ok = assistance._exec_support_tool(
                "read_logs", {"service": "litellm"}, "demo", "Demo", True)
        self.assertTrue(ok)          # absent = « aucun log disponible », pas une erreur
        self.assertIn("litellm", res)
        self.assertIn("read_logs", {t["function"]["name"] for t in assistance._support_tools(True)})
        self.assertIn("litellm", [t["function"]["parameters"]["properties"]["service"]["enum"]
                                  for t in assistance._support_tools(True)
                                  if t["function"]["name"] == "read_logs"][0])

    def test_les_logs_sont_admin_seulement(self):
        with patch.object(assistance, "get_db"):
            res, ok = assistance._exec_support_tool(
                "read_logs", {"service": "model"}, "demo", "Demo", False)
        self.assertFalse(ok)
        self.assertIn("administrateurs", res)
        self.assertIn("read_logs", {t["function"]["name"]
                                    for t in assistance._support_tools(True)})
        self.assertNotIn("read_logs", {t["function"]["name"]
                                       for t in assistance._support_tools(False)})

    def test_les_logs_ne_sont_plus_injectes_dans_le_contexte(self):
        """The context only points at the tool: a turn must not carry a blind
        log tail it did not ask for."""
        assistance._services_cache['at'] = 0.0
        with patch.object(assistance, "_sidecar_status", return_value="running"), \
             patch.object(assistance, "runner_status", return_value={"status": "running"}), \
             patch.object(assistance, "runner_logs",
                          return_value=["LIGNE_DE_LOG_QUI_NE_DOIT_PAS_APPARAITRE"]), \
             patch.object(assistance, "get_db") as gdb:
            gdb.return_value.execute.return_value.fetchall.return_value = []
            ctx = assistance._support_context(
                "demo", True, user_msg="le modèle renvoie une erreur 500")
        self.assertNotIn("LIGNE_DE_LOG_QUI_NE_DOIT_PAS_APPARAITRE", ctx)
        self.assertIn("read_logs", ctx)

    def test_service_ou_action_inconnue(self):
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
    def _frame_outil(name, args, call_id="call_1"):
        delta = {"tool_calls": [{"index": 0, "id": call_id,
                                 "function": {"name": name,
                                              "arguments": json.dumps(args)}}]}
        return "data: " + json.dumps({"choices": [{"delta": delta}]})

    @staticmethod
    def _frame_texte(texte):
        return "data: " + json.dumps({"choices": [{"delta": {"content": texte}}]})

    def _client(self, username="demo"):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        return c

    def _nettoyer(self):
        with portal.app.app_context():
            db = portal.get_db()
            for t in ("pending_actions", "support_feedback", "support_thread",
                      "audit_log", "login_attempts"):
                db.execute(f"DELETE FROM {t}")
            db.commit()

    def setUp(self):
        self._nettoyer()

    tearDown = setUp

    def _creer_demande(self, username="demo", tool="revoke_api_key",
                       args=None, label="Révocation de clé", target="test"):
        with portal.app.app_context():
            return assistance.creer_action_en_attente(username, tool, args or {"alias": "test"},
                                                      label, target)

    def test_action_sensible_non_executee_sans_confirmation(self):
        import unittest.mock as mock
        frames = [[self._frame_outil("revoke_api_key", {"alias": "test"}), "data: [DONE]"],
                  [self._frame_texte("Je peux révoquer cette clé — confirme ci-dessous."),
                   "data: [DONE]"]]
        appels = {"n": 0}

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            i = min(appels["n"], len(frames) - 1)
            appels["n"] += 1
            return self._Flux(frames[i])

        # `_fin_support` is neutralized: it is a thread started after the
        # response (saved thread + memory extraction) that calls requests.post
        # once the round is over and would steal a simulated frame from the next test.
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             mock.patch.object(chat, "quota_depasse_reset", return_value=None), \
             mock.patch.object(chat, "requests") as mock_requests, \
             mock.patch.object(chat, "_fin_support"), \
             mock.patch.object(chat, "_exec_support_tool") as exec_tool:
            mock_requests.post.side_effect = _post
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "révoque ma clé test"}]})
            self.assertEqual(r.status_code, 200)
            body = r.get_data(as_text=True)
        # Nothing was executed, and the UI receives what it needs to show the button.
        exec_tool.assert_not_called()
        self.assertIn("cronos_confirm", body)
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

    def test_confirmation_execute_une_seule_fois(self):
        import unittest.mock as mock
        tok = self._creer_demande()
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

    def test_annulation_n_execute_rien(self):
        import unittest.mock as mock
        tok = self._creer_demande()
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

    def test_jeton_d_un_autre_compte_refuse(self):
        tok = self._creer_demande(username="bob")
        with patch.object(chat, "_exec_support_tool", return_value=("jamais", False)) as exec_tool:
            r = self._client("demo").post("/support/confirm",
                                          headers={"X-CSRFToken": self.CSRF}, json={"token": tok})
            self.assertEqual(r.status_code, 404)
            exec_tool.assert_not_called()

    def test_jeton_inconnu_refuse(self):
        r = self._client().post("/support/confirm", headers={"X-CSRFToken": self.CSRF},
                                json={"token": "n-importe-quoi"})
        self.assertEqual(r.status_code, 404)
        r2 = self._client().post("/support/confirm", headers={"X-CSRFToken": self.CSRF}, json={})
        self.assertEqual(r2.status_code, 400)

    def test_une_action_non_sensible_n_est_pas_retardee(self):
        """create_api_key runs directly: no button for the non-destructive."""
        import unittest.mock as mock
        frames = [[self._frame_outil("create_api_key", {"alias": "ma-cle"}), "data: [DONE]"],
                  [self._frame_texte("C'est fait."), "data: [DONE]"]]
        appels = {"n": 0}

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            i = min(appels["n"], len(frames) - 1)
            appels["n"] += 1
            return self._Flux(frames[i])

        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user-123"}]), \
             mock.patch.object(chat, "quota_depasse_reset", return_value=None), \
             mock.patch.object(chat, "requests") as mock_requests, \
             mock.patch.object(chat, "_exec_support_tool",
                               return_value=("Clé créée.", True)) as exec_tool:
            mock_requests.post.side_effect = _post
            r = self._client().post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                                    json={"messages": [{"role": "user", "content": "crée une clé"}]})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("cronos_confirm", r.get_data(as_text=True))
        self.assertEqual(exec_tool.call_count, 1)

    def test_contexte_contient_les_actions_recentes(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO audit_log (username, action, detail, created_at) "
                       "VALUES (?,?,?,?)",
                       ("demo", "model.launch_échec", "lancement de laguna (OOM)", "2026-09-13T10:00"))
            db.commit()
            ctx = assistance._support_context("demo", False)
        self.assertIn("laguna (OOM)", ctx)
        self.assertIn("Ses dernières actions", ctx)

    def test_feedback_enregistre(self):
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

    def test_fil_support_conserve_et_efface(self):
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
    def _outil(name, args, call_id="call_1"):
        delta = {"tool_calls": [{"index": 0, "id": call_id,
                                 "function": {"name": name, "arguments": json.dumps(args)}}]}
        return "data: " + json.dumps({"choices": [{"delta": delta}]})

    @staticmethod
    def _texte(t):
        return "data: " + json.dumps({"choices": [{"delta": {"content": t}}]})

    def test_action_sensible_refusee_apres_contenu_externe(self):
        import unittest.mock as mock
        frames = [
            [self._outil("mcp_serveur_outil", {}), "data: [DONE]"],       # external read
            [self._outil("revoke_api_key", {"alias": "prod"}, "call_2"), "data: [DONE]"],
            [self._texte("Je ne peux pas faire ça dans ce tour."), "data: [DONE]"],
        ]
        n = {"i": 0}

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            i = min(n["i"], len(frames) - 1)
            n["i"] += 1
            return self._Flux(frames[i])

        routing = {"mcp_serveur_outil": {"kind": "mcp", "server_id": 1,
                                         "server_name": "srv", "tool_name": "outil"}}
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user"}]), \
             mock.patch.object(chat, "quota_depasse_reset", return_value=None), \
             mock.patch.object(chat, "_user_extra_tools", return_value=([], routing)), \
             mock.patch.object(chat, "_exec_mcp_tool", return_value=("texte d'un tiers", True)), \
             mock.patch.object(chat, "requests") as mock_requests, \
             mock.patch.object(chat, "_fin_support"), \
             mock.patch.object(chat, "_exec_support_tool") as exec_tool:
            mock_requests.post.side_effect = _post
            r = c.post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "user", "content": "résume cette page"}]})
            body = r.get_data(as_text=True)
        # No execution, no confirmation proposal: the refusal is clean.
        exec_tool.assert_not_called()
        self.assertNotIn("cronos_confirm", body)
        # The SSE body is JSON: accents are escaped there (\u00e9), so we
        # check the ASCII part of the message.
        self.assertIn("Action bloqu", body)
        with portal.app.app_context():
            n_pending = portal.get_db().execute(
                "SELECT COUNT(*) c FROM pending_actions WHERE username='demo'").fetchone()["c"]
        self.assertEqual(n_pending, 0)

    def test_creation_de_cle_refusee_apres_contenu_externe(self):
        """`create_api_key` is not destructive, but it DELIVERS a secret.

        It thus runs directly in the normal case (product choice, see
        `test_une_action_non_sensible_n_est_pas_retardee`) — except after
        reading external content: else a hostile page had a key created in
        the logged-in user's name, and its value entered the model's
        context (thus the kept thread) without anyone asking for it.
        """
        import unittest.mock as mock
        frames = [
            [self._outil("mcp_serveur_outil", {}), "data: [DONE]"],        # external read
            [self._outil("create_api_key", {"alias": "ma-cle"}, "call_2"), "data: [DONE]"],
            [self._texte("Je ne crée pas de clé dans ce tour."), "data: [DONE]"],
        ]
        n = {"i": 0}

        def _post(url, headers=None, json=None, timeout=None, stream=False):
            i = min(n["i"], len(frames) - 1)
            n["i"] += 1
            return self._Flux(frames[i])

        routing = {"mcp_serveur_outil": {"kind": "mcp", "server_id": 1,
                                         "server_name": "srv", "tool_name": "outil"}}
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
            s["csrf"] = self.CSRF
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "get_user_keys", return_value=[{"key": "sk-user"}]), \
             mock.patch.object(chat, "quota_depasse_reset", return_value=None), \
             mock.patch.object(chat, "_user_extra_tools", return_value=([], routing)), \
             mock.patch.object(chat, "_exec_mcp_tool", return_value=("texte d'un tiers", True)), \
             mock.patch.object(chat, "requests") as mock_requests, \
             mock.patch.object(chat, "_fin_support"), \
             mock.patch.object(chat, "_exec_support_tool") as exec_tool:
            mock_requests.post.side_effect = _post
            r = c.post("/support/chat", headers={"X-CSRFToken": self.CSRF},
                       json={"messages": [{"role": "user", "content": "résume cette page"}]})
            body = r.get_data(as_text=True)
        # No execution, no confirmation proposal: the refusal is clean.
        exec_tool.assert_not_called()
        self.assertNotIn("cronos_confirm", body)
        self.assertIn("Action bloqu", body)
        with portal.app.app_context():
            db = portal.get_db()
            n_cles = db.execute(
                "SELECT COUNT(*) c FROM api_keys WHERE username='demo'").fetchone()["c"]
            n_pending = db.execute(
                "SELECT COUNT(*) c FROM pending_actions WHERE username='demo'").fetchone()["c"]
        self.assertEqual(n_cles, 0, "aucune clé ne doit avoir été créée")
        self.assertEqual(n_pending, 0)


class SupportFinDeTourTest(unittest.TestCase):
    """End of Support round: the thread is kept, a CUT answer is not.

    The thread serves to resume the conversation after a page reload:
    freezing it on an interrupted answer would bring back a truncated
    sentence at every visit, and the memory extraction false facts.
    """

    def _nettoyer(self):
        with portal.app.app_context():
            db = portal.get_db()
            for t in ("support_thread", "audit_log"):
                db.execute(f"DELETE FROM {t}")
            db.commit()

    setUp = _nettoyer
    tearDown = _nettoyer

    HIST = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "réponse"}]

    def _fil(self):
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT messages FROM support_thread WHERE username='demo'").fetchone()
        return json.loads(row["messages"]) if row else None

    def test_reponse_complete_sauve_le_fil(self):
        chat._fin_support_corps("demo", self.HIST, "réponse finale", "fake", "sk-x",
                                False, portal.app)
        fil = self._fil()
        self.assertEqual([m["content"] for m in fil], ["question", "réponse", "réponse finale"])

    def test_reponse_interrompue_ne_sauve_rien(self):
        chat._fin_support("demo", self.HIST, ["réponse à moitié écri"], "fake", "sk-x",
                          False, False, portal.app)
        self.assertIsNone(self._fil(), "une réponse coupée ne doit pas devenir le fil")
