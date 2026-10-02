"""Account lifecycle: access cut, data purged, sessions visible.

Written on 2026-09-13, along with the fix it locks in. Before, deleting
an account only did a DELETE in `local_users`: its API keys stayed valid
(LiteLLM validates them itself, the portal is not in the API path), its
browser session survived up to 12 h, and its personal data stayed
attached to a name a colleague could take over — memory being indexed
per account, a recreated account inherited the previous one's
memories.

Two design rules deserve to be locked in here, because they are
counter-intuitive:

1. An account the portal has NO trace of must not be considered deleted
   (`test_compte_non_local_sans_ligne_reste_valide`): we never infer a
   revocation from missing data. The first version of this fix did, and
   logged everyone out at once.
2. The role is re-read at every request for a LOCAL account (the portal
   owns it), but not reinvented for a directory account, whose role comes
   from the last recorded login.
"""
import secrets
import time
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

import app as portal
import litellm_client
import user_lifecycle

CIBLE = 'ztest-cible'
ADMIN = 'ztest-admin'
ANNUAIRE = 'ztest-annuaire'
MDP = 'MotDePasse123'


class BaseComptes(unittest.TestCase):
    """Common base: two local accounts (a plain one, an admin) and a strict
    cleanup, so these tests leave nothing behind."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self._clean()
        self._mkuser(CIBLE, MDP, is_admin=0)
        self._mkuser(ADMIN, MDP, is_admin=1)

    def tearDown(self):
        self._clean()

    def _tables(self):
        return ('user_sessions', 'user_sources', 'blocked_users', 'api_keys',
                'memory_nodes', 'memory_edges', 'conversations', 'conversation_shares',
                'user_prefs', 'webauthn_credentials', 'user_security', 'pending_webauthn',
                'audit_log')

    def _clean(self):
        with portal.app.app_context():
            db = portal.get_db()
            for t in self._tables():
                db.execute(f"DELETE FROM {t} WHERE username IN (?,?,?)",
                           (CIBLE, ADMIN, ANNUAIRE))
            db.execute("DELETE FROM login_attempts WHERE key LIKE ?", ('%ztest%',))
            db.execute("DELETE FROM local_users WHERE username IN (?,?,?)",
                       (CIBLE, ADMIN, ANNUAIRE))
            db.commit()

    def _mkuser(self, username, password, is_admin=0):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute(
                "INSERT INTO local_users "
                "(username, password_hash, fullname, is_admin, group_name, max_budget, "
                "enabled, created_at) VALUES (?,?,?,?,NULL,NULL,1,?)",
                (username, generate_password_hash(password), username, is_admin,
                 time.time()))
            db.commit()

    def _uid(self, username):
        with portal.app.app_context():
            return portal.get_db().execute(
                "SELECT id FROM local_users WHERE username=?", (username,)).fetchone()['id']

    def _client(self, username, is_admin=False, sid=None, ip='82.67.54.181',
                user_agent='Mozilla/5.0 (X11; Linux) Test/1.0'):
        """Client with an opened session and, if asked, its server row."""
        if sid is not None:
            with portal.app.app_context():
                db = portal.get_db()
                db.execute(
                    "INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                    "created_at, ip, user_agent) VALUES (?,?,?,?,0,?,?,?)",
                    (sid, username, time.time(), time.time() + 3600, time.time(), ip, user_agent))
                db.commit()
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['csrf'] = 'tok'
            s['username'] = username
            s['auth_at'] = int(time.time())
            s['is_admin'] = is_admin
            if sid:
                s['sid'] = sid
        return c

    def _post(self, client, url, data=None, json=None):
        # werkzeug refuses data AND json together: we send only one.
        kwargs = {'headers': {'X-CSRFToken': 'tok'}}
        if json is not None:
            kwargs['json'] = json
        else:
            kwargs['data'] = data or {}
        return client.post(url, **kwargs)


class RevalidationTest(BaseComptes):
    """The account state is re-read at every request, not frozen in the cookie."""

    def test_desactivation_coupe_la_session_en_cours(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(CIBLE, sid=sid)
        self.assertEqual(c.get('/api/whoami').status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("UPDATE local_users SET enabled=0 WHERE username=?", (CIBLE,))
            db.commit()
        # Without the re-read, this session stayed valid up to 12 h.
        self.assertEqual(c.get('/api/whoami').status_code, 401)

    def test_retrogradation_retire_le_droit_admin(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(ADMIN, is_admin=True, sid=sid)
        self.assertEqual(c.get('/api/admin/users').status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("UPDATE local_users SET is_admin=0 WHERE username=?", (ADMIN,))
            db.commit()
        self.assertEqual(c.get('/api/admin/users').status_code, 403)
        self.assertFalse(c.get('/api/whoami').get_json()['is_admin'])

    def test_blocage_coupe_la_session(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(CIBLE, sid=sid)
        self.assertEqual(c.get('/api/whoami').status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO blocked_users (username, reason, blocked_by, blocked_at) "
                       "VALUES (?,?,?,?)", (CIBLE, 'test', ADMIN, '2026-09-13T10:00:00'))
            db.commit()
        self.assertEqual(c.get('/api/whoami').status_code, 401)

    def test_compte_non_local_sans_ligne_reste_valide(self):
        """An account with no local row NOR recorded source keeps its session.

        This is the « we infer nothing from missing data » rule: the first
        version of the fix treated such accounts as deleted and logged
        everyone out at once."""
        c = self._client('ztest-inconnu', is_admin=True)
        self.assertEqual(c.get('/api/whoami').status_code, 200)

    def test_role_annuaire_consigne_fait_foi(self):
        """For an LDAP/SSO account, the last recorded role is re-read."""
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_sources (username, sources, fullname, last_source, "
                       "last_seen, last_is_admin) VALUES (?,?,?,?,?,?)",
                       (ANNUAIRE, 'sso', ANNUAIRE, 'sso', '2026-09-13T10:00:00', 1))
            db.commit()
        c = self._client(ANNUAIRE, is_admin=True)
        self.assertEqual(c.get('/api/admin/users').status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("UPDATE user_sources SET last_is_admin=0 WHERE username=?", (ANNUAIRE,))
            db.commit()
        # The rights were removed on the directory side: the session follows.
        self.assertEqual(c.get('/api/admin/users').status_code, 403)


class BlocageTest(BaseComptes):
    """Blocking refuses access whatever the authentication source."""

    def test_bloquer_coupe_les_sessions_ouvertes(self):
        sid = secrets.token_urlsafe(32)
        self._client(CIBLE, sid=sid)
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{CIBLE}/block', json={'reason': 'départ'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['revoked_sessions'], 1)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT revoked FROM user_sessions WHERE sid=?", (sid,)).fetchone()
        self.assertEqual(row['revoked'], 1)

    def test_compte_bloque_ne_peut_plus_se_connecter(self):
        admin = self._client(ADMIN, is_admin=True)
        self._post(admin, f'/admin/users/{CIBLE}/block', json={'reason': 'départ'})
        anon = portal.app.test_client()
        with anon.session_transaction() as s:
            s['csrf'] = 'tok'
        r = anon.post('/login', data={'username': CIBLE, 'password': MDP},
                      headers={'X-CSRFToken': 'tok', 'Cf-Connecting-Ip': '82.67.54.181'})
        # 403 and not « identifiants incorrects »: the password is right,
        # it is the account that is refused.
        self.assertEqual(r.status_code, 403)

    def test_debloquer_rend_l_acces(self):
        admin = self._client(ADMIN, is_admin=True)
        self._post(admin, f'/admin/users/{CIBLE}/block', json={'reason': 'test'})
        self.assertEqual(self._post(admin, f'/admin/users/{CIBLE}/unblock').status_code, 200)
        anon = portal.app.test_client()
        with anon.session_transaction() as s:
            s['csrf'] = 'tok'
        r = anon.post('/login', data={'username': CIBLE, 'password': MDP},
                      headers={'X-CSRFToken': 'tok', 'Cf-Connecting-Ip': '82.67.54.181'})
        self.assertEqual(r.status_code, 302)

    def test_compte_annuaire_bloquable(self):
        """The case that had NO lever before: an LDAP/SSO account without a
        local row, that one could only log out — it came back."""
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{ANNUAIRE}/block', json={'reason': 'parti'})
        self.assertEqual(r.status_code, 200)
        c = self._client(ANNUAIRE, is_admin=False)
        self.assertEqual(c.get('/api/whoami').status_code, 401)

    def test_auto_blocage_refuse(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{ADMIN}/block', json={'reason': 'erreur'})
        self.assertEqual(r.status_code, 400)

    def test_dernier_admin_local_protege(self):
        autre = self._client(ANNUAIRE, is_admin=True)
        r = self._post(autre, f'/admin/users/{ADMIN}/block', json={'reason': 'test'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('Dernier administrateur', r.get_json()['error'])


class SuppressionTest(BaseComptes):
    """Deleting an account means removing an access, not erasing a row."""

    def _donnees(self, username):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO api_keys (username, key_alias, key_value, created_at) "
                       "VALUES (?,?,?,?)", (username, 'test', 'sk-test-123', '2026-09-13'))
            db.execute("INSERT INTO memory_nodes (username, name, name_norm, kind, created_at) "
                       "VALUES (?,?,?,?,?)", (username, 'vLLM', 'vllm', 'sujet', '2026-09-13'))
            db.execute("INSERT INTO memory_edges (username, src_id, relation, fact, created_at) "
                       "VALUES (?,?,?,?,?)", (username, 1, 'concerne', 'un fait', '2026-09-13'))
            db.execute("INSERT INTO conversations (username, client_id, title, model, messages, "
                       "updated_at) VALUES (?,?,?,?,?,?)",
                       (username, 'c1', 't', 'm', '[]', '2026-09-13'))
            db.execute("INSERT INTO user_prefs (username, theme_id) VALUES (?,?)",
                       (username, 'dark'))
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (secrets.token_urlsafe(32), username, time.time(),
                        time.time() + 3600, time.time()))
            db.commit()

    def test_sans_confirmation_refuse(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/delete/{self._uid(CIBLE)}')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()['needs_confirm'])
        # The summary is provided so the UI can say what will be lost.
        self.assertIn('conversations', r.get_json()['donnees'])
        self.assertIsNotNone(self._uid(CIBLE))

    def test_auto_suppression_refusee(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/delete/{self._uid(ADMIN)}',
                       json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('propre compte', r.get_json()['error'])

    def test_dernier_admin_local_protege(self):
        autre = self._client(ANNUAIRE, is_admin=True)
        r = self._post(autre, f'/admin/users/delete/{self._uid(ADMIN)}',
                       json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('Dernier administrateur', r.get_json()['error'])

    def test_suppression_purge_acces_et_donnees(self):
        self._donnees(CIBLE)
        admin = self._client(ADMIN, is_admin=True)
        with patch.object(litellm_client, 'revoke_litellm_key', return_value=True), \
             patch.object(litellm_client, 'delete_litellm_user', return_value=True):
            r = self._post(admin, f'/admin/users/delete/{self._uid(CIBLE)}',
                           json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 200)
        purged = r.get_json()['purged']
        self.assertEqual(purged['keys_revoked'], 1)
        self.assertEqual(purged['keys_failed'], 0)
        self.assertTrue(purged['litellm_user'])
        with portal.app.app_context():
            db = portal.get_db()
            for table in ('local_users', 'api_keys', 'memory_nodes', 'memory_edges',
                          'conversations', 'user_prefs'):
                n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE username=?",
                               (CIBLE,)).fetchone()[0]
                self.assertEqual(n, 0, f'{table} devrait être purgée')
            sessions = db.execute(
                "SELECT COUNT(*) FROM user_sessions WHERE username=? AND revoked=0",
                (CIBLE,)).fetchone()[0]
            self.assertEqual(sessions, 0)
            trace = db.execute(
                "SELECT detail FROM audit_log WHERE action='user.delete' ORDER BY id DESC"
            ).fetchone()
        self.assertIn(CIBLE, trace['detail'])

    def test_echec_de_revocation_signale(self):
        """LiteLLM unreachable: the deletion succeeds, but the admin is warned
        that keys may survive. The silent failure was exactly the
        problem."""
        self._donnees(CIBLE)
        admin = self._client(ADMIN, is_admin=True)
        with patch.object(litellm_client, 'revoke_litellm_key', return_value=False), \
             patch.object(litellm_client, 'delete_litellm_user', return_value=False):
            r = self._post(admin, f'/admin/users/delete/{self._uid(CIBLE)}',
                           json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['purged']['keys_failed'], 1)
        self.assertIn('LiteLLM', body.get('warning', ''))
        self.assertIn('NON supprimée', self._derniere_trace())

    def _derniere_trace(self):
        with portal.app.app_context():
            return portal.get_db().execute(
                "SELECT detail FROM audit_log WHERE action='user.delete' ORDER BY id DESC"
            ).fetchone()['detail']


class PurgeCompteAnnuaireTest(BaseComptes):
    """A directory account has no local row: its deletion is impossible by
    construction, but its data must not remain."""

    def _donnees(self, username):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO memory_nodes (username, name, name_norm, kind, created_at) "
                       "VALUES (?,?,?,?,?)", (username, 'sujet', 'sujet', 'sujet', '2026-09-13'))
            db.execute("INSERT INTO conversations (username, client_id, title, model, messages, "
                       "updated_at) VALUES (?,?,?,?,?,?)",
                       (username, 'c1', 't', 'm', '[]', '2026-09-13'))
            db.commit()

    def test_purge_efface_les_donnees_sans_toucher_a_l_acces(self):
        self._donnees(ANNUAIRE)
        admin = self._client(ADMIN, is_admin=True)
        # We BLOCK first: it is the ACCESS state that must survive the purge.
        # Without this prior block, the test missed the defect — `blocked_users`
        # was among the purged tables, so the normal gesture when a directory
        # employee leaves (block then purge) made the account loggable again,
        # silently and without saying so in the audit.
        self.assertEqual(
            self._post(admin, f'/admin/users/{ANNUAIRE}/block',
                       json={'reason': 'parti'}).status_code, 200)
        r = self._post(admin, f'/admin/users/{ANNUAIRE}/purge', json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['purged']['memory_facts'], 1)
        with portal.app.app_context():
            db = portal.get_db()
            reste = db.execute("SELECT COUNT(*) FROM memory_nodes WHERE username=?",
                               (ANNUAIRE,)).fetchone()[0]
            trace = db.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0]
        self.assertEqual(reste, 0)
        # The audit distinguishes a purge from a deletion: it is not the same
        # gesture, and a directory account can come back.
        self.assertEqual(trace, 'user.purge')
        with portal.app.app_context():
            # Purging is not unblocking: the purge only erases DATA, blocking
            # remains the offboarding lever.
            self.assertTrue(portal.est_bloque(ANNUAIRE))

    def test_sans_confirmation_refuse(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{ANNUAIRE}/purge')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()['needs_confirm'])

    def test_compte_local_refuse_sur_cette_route(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{CIBLE}/purge', json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('suppression', r.get_json()['error'])

    def test_purge_de_son_propre_compte_refusee(self):
        admin = self._client(ADMIN, is_admin=True)
        r = self._post(admin, f'/admin/users/{ADMIN}/purge', json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 400)


class DetailTest(BaseComptes):
    """The detail view must inform without ever leaking a key."""

    def test_detail_ne_renvoie_jamais_la_valeur_d_une_cle(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO api_keys (username, key_alias, key_value, created_at) "
                       "VALUES (?,?,?,?)", (CIBLE, 'captures', 'sk-secret-a-ne-pas-sortir',
                                            '2026-09-13'))
            db.commit()
        admin = self._client(ADMIN, is_admin=True)
        r = admin.get(f'/admin/users/{CIBLE}/detail')
        self.assertEqual(r.status_code, 200)
        corps = r.get_data(as_text=True)
        self.assertNotIn('sk-secret-a-ne-pas-sortir', corps)
        body = r.get_json()
        self.assertEqual(body['keys'][0]['alias'], 'captures')
        self.assertTrue(body['local'])

    def test_detail_d_un_compte_annuaire(self):
        admin = self._client(ADMIN, is_admin=True)
        r = admin.get(f'/admin/users/{ANNUAIRE}/detail')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['local'])

    def test_liste_des_comptes_expose_etat_de_blocage(self):
        admin = self._client(ADMIN, is_admin=True)
        self._post(admin, f'/admin/users/{CIBLE}/block', json={'reason': 'départ'})
        users = {u['username']: u for u in admin.get('/api/admin/users').get_json()['users']}
        self.assertTrue(users[CIBLE]['blocked'])
        self.assertEqual(users[CIBLE]['block_reason'], 'départ')
        self.assertFalse(users[ADMIN]['blocked'])

    def test_liste_des_comptes_porte_l_avatar(self):
        """The admin list carries the avatar, like the rest of the UI.

        The admin screen shows the pp there: chosen brand logo, or
        generated avatar when `avatar_id` is null. Without this field the
        admin saw initials where the person sees their pp — unguessable.
        """
        admin = self._client(ADMIN, is_admin=True)
        users = {u['username']: u for u in admin.get('/api/admin/users').get_json()['users']}
        self.assertIn('avatar_id', users[CIBLE])
        self.assertIsNone(users[CIBLE]['avatar_id'], "aucun logo choisi : avatar généré")

        with portal.app.app_context():
            db = portal.get_db()
            logo = portal.AVATAR_IDS[0]
            db.execute("INSERT INTO user_prefs (username, avatar_id) VALUES (?,?) "
                       "ON CONFLICT(username) DO UPDATE SET avatar_id=excluded.avatar_id",
                       (CIBLE, logo))
            db.commit()
        users = {u['username']: u for u in admin.get('/api/admin/users').get_json()['users']}
        self.assertEqual(users[CIBLE]['avatar_id'], logo)


class AutoServiceTest(BaseComptes):
    """What the user can do for themselves."""

    def test_liste_mes_sessions_sans_sid_complet(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(CIBLE, sid=sid)
        r = c.get('/api/account/sessions')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['local_account'])
        self.assertEqual(len(body['sessions']), 1)
        s = body['sessions'][0]
        # Short fingerprint only: the sid is the cookie's secret.
        self.assertEqual(len(s['id']), 12)
        self.assertNotIn(sid, r.get_data(as_text=True))
        self.assertTrue(s['current'])
        self.assertEqual(s['ip'], '82.67.54.181')

    def test_revoquer_une_autre_session(self):
        sid = secrets.token_urlsafe(32)
        autre = secrets.token_urlsafe(32)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (autre, CIBLE, time.time() - 60, time.time() + 3600, time.time()))
            db.commit()
        c = self._client(CIBLE, sid=sid)
        r = self._post(c, '/api/account/sessions/revoke', json={'id': autre[:12]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['current'])
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT revoked FROM user_sessions WHERE sid=?", (autre,)).fetchone()
        self.assertEqual(row['revoked'], 1)

    def test_revoquer_la_session_courante(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(CIBLE, sid=sid)
        r = self._post(c, '/api/account/sessions/revoke', json={'id': sid[:12]})
        self.assertTrue(r.get_json()['current'])
        self.assertEqual(c.get('/api/whoami').status_code, 401)

    def test_impossible_de_revoquer_la_session_d_autrui(self):
        sid_autre = secrets.token_urlsafe(32)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (sid_autre, ADMIN, time.time(), time.time() + 3600, time.time()))
            db.commit()
        c = self._client(CIBLE, sid=secrets.token_urlsafe(32))
        r = self._post(c, '/api/account/sessions/revoke', json={'id': sid_autre[:12]})
        self.assertEqual(r.status_code, 404)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT revoked FROM user_sessions WHERE sid=?", (sid_autre,)).fetchone()
        self.assertEqual(row['revoked'], 0)

    def test_revoquer_toutes_les_autres(self):
        sid = secrets.token_urlsafe(32)
        with portal.app.app_context():
            db = portal.get_db()
            for _ in range(2):
                db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, "
                           "revoked, created_at) VALUES (?,?,?,?,0,?)",
                           (secrets.token_urlsafe(32), CIBLE, time.time(),
                            time.time() + 3600, time.time()))
            db.commit()
        c = self._client(CIBLE, sid=sid)
        r = self._post(c, '/api/account/sessions/revoke', json={'all': True})
        self.assertEqual(r.get_json()['revoked'], 2)
        self.assertEqual(c.get('/api/whoami').status_code, 200)

    def test_compte_annuaire_ne_change_pas_son_mot_de_passe(self):
        c = self._client(ANNUAIRE)
        r = self._post(c, '/api/account/password',
                       json={'current': 'x', 'new': 'NouveauMotDePasse1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('annuaire', r.get_json()['error'])

    def test_mot_de_passe_actuel_incorrect(self):
        c = self._client(CIBLE)
        r = self._post(c, '/api/account/password',
                       json={'current': 'faux', 'new': 'NouveauMotDePasse1'})
        self.assertEqual(r.status_code, 400)

    def test_changement_de_mot_de_passe_ferme_les_autres_sessions(self):
        sid = secrets.token_urlsafe(32)
        autre = secrets.token_urlsafe(32)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (autre, CIBLE, time.time(), time.time() + 3600, time.time()))
            db.commit()
        c = self._client(CIBLE, sid=sid)
        r = self._post(c, '/api/account/password',
                       json={'current': MDP, 'new': 'NouveauMotDePasse1'})
        self.assertEqual(r.status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            row = db.execute("SELECT password_hash FROM local_users WHERE username=?",
                             (CIBLE,)).fetchone()
            self.assertTrue(check_password_hash(row['password_hash'], 'NouveauMotDePasse1'))
            autre_row = db.execute("SELECT revoked FROM user_sessions WHERE sid=?",
                                   (autre,)).fetchone()
            courante = db.execute("SELECT revoked FROM user_sessions WHERE sid=?",
                                  (sid,)).fetchone()
        self.assertEqual(autre_row['revoked'], 1)
        # The current session survives: being logged out by one's own
        # action discourages changing one's password.
        self.assertEqual(courante['revoked'], 0)
        self.assertEqual(c.get('/api/whoami').status_code, 200)

    def test_refuse_un_mot_de_passe_identique(self):
        c = self._client(CIBLE)
        r = self._post(c, '/api/account/password', json={'current': MDP, 'new': MDP})
        self.assertEqual(r.status_code, 400)


class OrigineSessionTest(BaseComptes):
    """A session opened before the IP/user-agent columns were added has none:
    its bearer must be able to fill them in by opening their list, else they
    cannot recognize their own sessions (a dash says nothing)."""

    def test_une_session_sans_origine_se_complete_a_la_lecture(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(CIBLE, sid=sid, ip=None, user_agent=None)
        self.assertEqual(c.get('/api/account/sessions').status_code, 200)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT ip, user_agent FROM user_sessions WHERE sid=?", (sid,)).fetchone()
        self.assertTrue(row['ip'])
        self.assertTrue(row['user_agent'])

    def test_une_origine_deja_notee_n_est_jamais_ecrasee(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(CIBLE, sid=sid, ip='203.0.113.9', user_agent='Ancien/1.0')
        self.assertEqual(c.get('/api/account/sessions').status_code, 200)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT ip, user_agent FROM user_sessions WHERE sid=?", (sid,)).fetchone()
        # This is exactly the trace we want to keep: a stolen cookie must not
        # be able to rewrite its own origin by showing up.
        self.assertEqual((row['ip'], row['user_agent']), ('203.0.113.9', 'Ancien/1.0'))

    def test_la_vue_admin_ne_touche_pas_la_session_d_un_autre(self):
        autrui = secrets.token_urlsafe(32)
        self._client(CIBLE, sid=autrui, ip=None, user_agent=None)
        admin = self._client(ADMIN, is_admin=True)
        self.assertEqual(admin.get(f'/admin/users/{CIBLE}/detail').status_code, 200)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT ip FROM user_sessions WHERE sid=?", (autrui,)).fetchone()
        # Writing the admin's IP there would make the row say « cette session
        # vient de l'admin » — a lie in the only view used to spot an unknown
        # session.
        self.assertIsNone(row['ip'])


class PolitiqueMotDePasseTest(unittest.TestCase):
    """Password rules, tested directly: they serve both the creation by an
    admin and the self-service change."""

    def _err(self, pw, user=None):
        from local_users import password_policy_error
        return password_policy_error(pw, user)

    def test_trop_court(self):
        self.assertIsNotNone(self._err('court'))

    def test_mot_de_passe_courant_refuse(self):
        self.assertIsNotNone(self._err('password123'))

    def test_identifiant_dans_le_mot_de_passe_refuse(self):
        self.assertIsNotNone(self._err('ztest-cible2026', 'ztest-cible'))

    def test_repetitif_refuse(self):
        self.assertIsNotNone(self._err('aaaaaaaaaaaa'))

    def test_mot_de_passe_correct_accepte(self):
        self.assertIsNone(self._err('ChevalPileOrage42', 'ztest-cible'))


class QuotaTest(BaseComptes):
    """A quota that could not be written must no longer go unnoticed."""

    def test_echec_de_quota_signale_a_la_creation(self):
        admin = self._client(ADMIN, is_admin=True)
        with patch('admin_routes._sync_local_user_budget', return_value=False):
            r = self._post(admin, '/admin/users/create',
                           data={'username': 'ztest-nouveau', 'password': 'MotDePasse123'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('warning', r.get_json())
        with portal.app.app_context():
            portal.get_db().execute("DELETE FROM local_users WHERE username='ztest-nouveau'")
            portal.get_db().commit()

    def test_suppression_de_groupe_resynchronise_les_membres(self):
        vus = []
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) "
                       "VALUES (?,?,?,?)", ('ztest-groupe', 5000, 0, '2026-09-13'))
            db.execute("UPDATE local_users SET group_name='ztest-groupe' WHERE username=?",
                       (CIBLE,))
            db.commit()
        admin = self._client(ADMIN, is_admin=True)

        def faux_sync(username, row):
            vus.append(username)
            return True

        with patch('admin_routes._sync_local_user_budget', side_effect=faux_sync):
            r = self._post(admin, '/admin/groups/delete/ztest-groupe')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['membres'], 1)
        # Without this propagation, the member kept the old envelope: a ghost
        # quota, more generous than the default.
        self.assertEqual(vus, [CIBLE])
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT group_name FROM local_users WHERE username=?", (CIBLE,)).fetchone()
        self.assertIsNone(row['group_name'])

    def test_membre_avec_plafond_propre_non_resynchronise(self):
        vus = []
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("INSERT INTO user_groups (name, max_budget, is_admin, created_at) "
                       "VALUES (?,?,?,?)", ('ztest-groupe', 5000, 0, '2026-09-13'))
            db.execute("UPDATE local_users SET group_name='ztest-groupe', max_budget=777 "
                       "WHERE username=?", (CIBLE,))
            db.commit()
        admin = self._client(ADMIN, is_admin=True)
        with patch('admin_routes._sync_local_user_budget',
                   side_effect=lambda u, r: vus.append(u) or True):
            self._post(admin, '/admin/groups/delete/ztest-groupe')
        # Their personal cap did not depend on the group: nothing to propagate.
        self.assertEqual(vus, [])


class NotificationMotDePasseTest(unittest.TestCase):
    """The password change notification is a bonus, never a condition: it
    must break nothing when the email is not configured."""

    def test_sans_smtp_rien_ne_part_et_rien_ne_leve(self):
        with patch.object(user_lifecycle, 'SMTP_HOST', ''):
            self.assertFalse(user_lifecycle.prevenir_mot_de_passe_change(CIBLE))

    def test_annuaire_injoignable_ne_leve_pas(self):
        with patch('auth.ldap_lookup_email', side_effect=RuntimeError('annuaire HS')):
            self.assertFalse(
                user_lifecycle._envoyer_notification_mot_de_passe(CIBLE))

    def test_sans_adresse_connue_rien_ne_part(self):
        with patch('auth.ldap_lookup_email', return_value=None), \
             patch('notify.send_user_email') as envoi:
            self.assertFalse(
                user_lifecycle._envoyer_notification_mot_de_passe(CIBLE))
        self.assertFalse(envoi.called)

    def test_envoi_au_bon_destinataire_et_message_distinct(self):
        with patch('auth.ldap_lookup_email', return_value='quelqu-un@example.com'), \
             patch('notify.send_user_email', return_value=True) as envoi:
            self.assertTrue(
                user_lifecycle._envoyer_notification_mot_de_passe(CIBLE))
            self.assertTrue(user_lifecycle._envoyer_notification_mot_de_passe(
                CIBLE, par_admin=ADMIN))
        self.assertEqual(envoi.call_args_list[0][0][0], 'quelqu-un@example.com')
        # The message says WHO changed the password: this is the whole value
        # of the notification for the person receiving it.
        self.assertIn("Ton mot de passe", envoi.call_args_list[0][0][2])
        self.assertIn("Un administrateur", envoi.call_args_list[1][0][2])

    def test_la_reponse_n_attend_pas_l_annuaire(self):
        """Defect measured on 2026-09-16: 3,2 s of response because of the LDAP bind.

        The password change was already written to database, but the click
        stayed suspended until the (mute) directory saw fit to expire.
        The notification is now entrusted to a thread: we check here that
        the call returns immediately, even when the address lookup is slow,
        then we wait for the thread to verify the email still goes out — a
        latency fix that would lose the notification would be worse than
        the evil.
        """
        import time as _time
        vus = []

        def _lent(_user):
            _time.sleep(1.5)
            return 'quelqu-un@example.com'

        with patch.object(user_lifecycle, 'SMTP_HOST', 'smtp.zoho.com'), \
             patch('auth.ldap_lookup_email', side_effect=_lent), \
             patch('notify.send_user_email',
                   side_effect=lambda *a, **k: vus.append(a) or True):
            t0 = _time.time()
            self.assertTrue(user_lifecycle.prevenir_mot_de_passe_change(CIBLE))
            ecoule = _time.time() - t0
            self.assertLess(ecoule, 0.5,
                            f"la réponse a attendu l'annuaire ({ecoule:.2f} s)")
            for _ in range(60):                       # up to ~3 s
                if vus:
                    break
                _time.sleep(0.05)
        self.assertTrue(vus, "la notification doit finir par partir malgré le fil")


class BalayageHygieneTest(BaseComptes):
    """The startup sweep must only remove what is DEAD.

    The risk of this kind of cleanup is making a still valid session
    disappear, which would log everyone out — hence a test checking both
    sides: stale rows go, live ones stay.
    """

    def test_les_lignes_perimees_partent_et_les_vivantes_restent(self):
        with portal.app.app_context():
            db = portal.get_db()
            sid_vivante = secrets.token_urlsafe(32)
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,0,?)",
                       (sid_vivante, CIBLE, time.time(), time.time() + 3600, time.time()))
            db.execute("INSERT INTO user_sessions (sid, username, auth_at, expires_at, revoked, "
                       "created_at) VALUES (?,?,?,?,1,?)",
                       (secrets.token_urlsafe(32), CIBLE, time.time() - 40 * 86400,
                        time.time() - 39 * 86400, time.time() - 40 * 86400))
            db.execute("INSERT INTO login_attempts (key, fails, first_at, locked_until) "
                       "VALUES (?,?,?,?)", ('82.0.0.1|ztest-vieux', 6,
                                            time.time() - 30 * 86400, 0))
            db.execute("INSERT INTO pending_webauthn (nonce, username, kind, challenge, "
                       "created_at, expires_at) VALUES (?,?,?,?,?,?)",
                       ('ztest-nonce', CIBLE, 'login', b'x', time.time() - 600, time.time() - 300))
            db.commit()
            portal.init_db()
            db = portal.get_db()
            restantes = {r['sid'] for r in db.execute(
                "SELECT sid FROM user_sessions WHERE username=?", (CIBLE,)).fetchall()}
            vieux = db.execute("SELECT COUNT(*) FROM login_attempts WHERE key LIKE ?",
                               ('%ztest-vieux',)).fetchone()[0]
            nonce = db.execute("SELECT COUNT(*) FROM pending_webauthn WHERE nonce=?",
                               ('ztest-nonce',)).fetchone()[0]
        self.assertEqual(restantes, {sid_vivante})
        self.assertEqual(vieux, 0)
        self.assertEqual(nonce, 0)


class PurgeInventaireTest(BaseComptes):
    """The inventory of purged tables must stay honest: any table carrying a
    `username` and absent from the list must be a deliberate choice."""

    # `audit_log` (and its `audit_log_archive`, the rows it evicts — see
    # db.log_audit, 2026-10-02) is the trace of admin actions, `local_users` is
    # erased explicitly by identifier — and `blocked_users` is an ACCESS
    # decision, not personal data: purging it would UNBLOCK the account (see the
    # comment on `TABLES_PURGEES`). It is the only offboarding lever for a
    # directory account, so it survives the purge.
    NON_PURGEES_VOLONTAIREMENT = {'audit_log', 'audit_log_archive',
                                  'local_users', 'blocked_users'}

    def test_toutes_les_tables_utilisateur_sont_couvertes(self):
        with portal.app.app_context():
            db = portal.get_db()
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            avec_username = set()
            for t in tables:
                cols = {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
                if 'username' in cols:
                    avec_username.add(t)
        manquantes = avec_username - set(user_lifecycle.TABLES_PURGEES) \
            - self.NON_PURGEES_VOLONTAIREMENT
        self.assertEqual(
            manquantes, set(),
            f"tables portant un username et non purgées : {sorted(manquantes)} — "
            "les ajouter à TABLES_PURGEES ou les déclarer ici avec la raison")


if __name__ == '__main__':
    unittest.main()
