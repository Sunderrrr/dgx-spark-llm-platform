"""Cycle de vie d'un compte : accès coupé, données purgées, sessions visibles.

Écrit le 2026-09-13, en même temps que le correctif qu'il verrouille. Avant,
supprimer un compte ne faisait qu'un DELETE dans `local_users` : ses clés API
restaient valides (LiteLLM les valide lui-même, le portail n'est pas dans le
chemin de l'API), sa session navigateur survivait jusqu'à 12 h, et ses données
personnelles restaient rattachées à un nom qu'un collègue pouvait reprendre —
la mémoire étant indexée par compte, un compte recréé héritait des souvenirs
du précédent.

Deux règles de conception méritent d'être verrouillées ici, parce qu'elles sont
contre-intuitives :

1. Un compte dont le portail n'a AUCUNE trace ne doit pas être considéré comme
   supprimé (`test_compte_non_local_sans_ligne_reste_valide`) : on ne déduit
   jamais une révocation d'une donnée absente. La première version de ce
   correctif le faisait et déconnectait tout le monde.
2. Le rôle est relu à chaque requête pour un compte LOCAL (le portail en est
   maître), mais pas réinventé pour un compte d'annuaire, dont le rôle vient du
   dernier login consigné.
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
    """Socle commun : deux comptes locaux (un simple, un admin) et un ménage
    strict, pour que ces tests ne laissent rien derrière eux."""

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
        """Client avec une session ouverte et, si demandé, sa ligne serveur."""
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
        # werkzeug refuse data ET json ensemble : on n'en envoie qu'un.
        kwargs = {'headers': {'X-CSRFToken': 'tok'}}
        if json is not None:
            kwargs['json'] = json
        else:
            kwargs['data'] = data or {}
        return client.post(url, **kwargs)


class RevalidationTest(BaseComptes):
    """L'état du compte est relu à chaque requête, pas figé dans le cookie."""

    def test_desactivation_coupe_la_session_en_cours(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(CIBLE, sid=sid)
        self.assertEqual(c.get('/api/whoami').status_code, 200)
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("UPDATE local_users SET enabled=0 WHERE username=?", (CIBLE,))
            db.commit()
        # Sans la relecture, cette session restait valide jusqu'à 12 h.
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
        """Un compte sans ligne locale NI source consignée garde sa session.

        C'est la règle du « on ne déduit rien d'une donnée absente » : la
        première version du correctif traitait ces comptes comme supprimés et
        déconnectait tout le monde d'un coup."""
        c = self._client('ztest-inconnu', is_admin=True)
        self.assertEqual(c.get('/api/whoami').status_code, 200)

    def test_role_annuaire_consigne_fait_foi(self):
        """Pour un compte LDAP/SSO, le dernier rôle consigné est relu."""
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
        # Les droits ont été retirés côté annuaire : la session suit.
        self.assertEqual(c.get('/api/admin/users').status_code, 403)


class BlocageTest(BaseComptes):
    """Bloquer refuse l'accès quelle que soit la source d'authentification."""

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
        # 403 et non « identifiants incorrects » : le mot de passe est bon,
        # c'est le compte qui est refusé.
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
        """Le cas qui n'avait AUCUN levier avant : un compte LDAP/SSO sans
        ligne locale, qu'on ne pouvait que déconnecter — il revenait."""
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
    """Supprimer un compte, c'est retirer un accès, pas effacer une ligne."""

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
        # Le résumé est fourni pour que l'interface dise ce qui sera perdu.
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
        """LiteLLM injoignable : la suppression aboutit, mais l'admin est
        prévenu que des clés peuvent survivre. L'échec silencieux était
        exactement le problème."""
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
    """Un compte d'annuaire n'a pas de ligne locale : sa suppression est
    impossible par construction, mais ses données ne doivent pas rester."""

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
        r = self._post(admin, f'/admin/users/{ANNUAIRE}/purge', json={'confirm': 'DELETE'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['purged']['memory_facts'], 1)
        with portal.app.app_context():
            db = portal.get_db()
            reste = db.execute("SELECT COUNT(*) FROM memory_nodes WHERE username=?",
                               (ANNUAIRE,)).fetchone()[0]
            trace = db.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0]
        self.assertEqual(reste, 0)
        # L'audit distingue une purge d'une suppression : ce n'est pas le même
        # geste, et un compte d'annuaire peut revenir.
        self.assertEqual(trace, 'user.purge')
        with portal.app.app_context():
            # Purger n'est pas bloquer : le compte d'annuaire peut se
            # reconnecter (et repartira alors d'un compte vide).
            self.assertFalse(portal.est_bloque(ANNUAIRE))

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
    """La vue détail doit informer sans jamais laisser fuir une clé."""

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


class AutoServiceTest(BaseComptes):
    """Ce que l'utilisateur peut faire pour lui-même."""

    def test_liste_mes_sessions_sans_sid_complet(self):
        sid = secrets.token_urlsafe(32)
        c = self._client(CIBLE, sid=sid)
        r = c.get('/api/account/sessions')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['local_account'])
        self.assertEqual(len(body['sessions']), 1)
        s = body['sessions'][0]
        # Empreinte courte seulement : le sid est le secret du cookie.
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
        # La session courante survit : se faire déconnecter par sa propre
        # action décourage de changer de mot de passe.
        self.assertEqual(courante['revoked'], 0)
        self.assertEqual(c.get('/api/whoami').status_code, 200)

    def test_refuse_un_mot_de_passe_identique(self):
        c = self._client(CIBLE)
        r = self._post(c, '/api/account/password', json={'current': MDP, 'new': MDP})
        self.assertEqual(r.status_code, 400)


class OrigineSessionTest(BaseComptes):
    """Une session ouverte avant l'ajout des colonnes IP/user-agent n'en a
    aucune : son porteur doit pouvoir la compléter en ouvrant sa liste, sinon il
    ne peut pas reconnaître ses propres sessions (un tiret ne dit rien)."""

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
        # C'est précisément la trace qu'on veut garder : un cookie volé ne doit
        # pas pouvoir réécrire sa propre origine en se présentant.
        self.assertEqual((row['ip'], row['user_agent']), ('203.0.113.9', 'Ancien/1.0'))

    def test_la_vue_admin_ne_touche_pas_la_session_d_un_autre(self):
        autrui = secrets.token_urlsafe(32)
        self._client(CIBLE, sid=autrui, ip=None, user_agent=None)
        admin = self._client(ADMIN, is_admin=True)
        self.assertEqual(admin.get(f'/admin/users/{CIBLE}/detail').status_code, 200)
        with portal.app.app_context():
            row = portal.get_db().execute(
                "SELECT ip FROM user_sessions WHERE sid=?", (autrui,)).fetchone()
        # Y écrire l'IP de l'admin ferait dire à la ligne « cette session vient
        # de l'admin » — un mensonge dans la seule vue qui sert à repérer une
        # session inconnue.
        self.assertIsNone(row['ip'])


class PolitiqueMotDePasseTest(unittest.TestCase):
    """Règles de mot de passe, testées directement : elles servent à la fois à
    la création par un admin et au changement en autonomie."""

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
    """Un quota qui n'a pas pu être écrit ne doit plus passer inaperçu."""

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
        # Sans cette propagation, le membre gardait l'ancienne enveloppe : un
        # quota fantôme, plus généreux que le défaut.
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
        # Son plafond personnel ne dépendait pas du groupe : rien à propager.
        self.assertEqual(vus, [])


class NotificationMotDePasseTest(unittest.TestCase):
    """La notification de changement de mot de passe est un bonus, jamais une
    condition : elle ne doit rien casser quand l'email n'est pas configuré."""

    def test_sans_smtp_rien_ne_part_et_rien_ne_leve(self):
        with patch.object(user_lifecycle, 'SMTP_HOST', ''):
            self.assertFalse(user_lifecycle.prevenir_mot_de_passe_change(CIBLE))

    def test_annuaire_injoignable_ne_leve_pas(self):
        with patch.object(user_lifecycle, 'SMTP_HOST', 'smtp.zoho.com'), \
             patch('auth.ldap_lookup_email', side_effect=RuntimeError('annuaire HS')):
            self.assertFalse(user_lifecycle.prevenir_mot_de_passe_change(CIBLE))

    def test_sans_adresse_connue_rien_ne_part(self):
        with patch.object(user_lifecycle, 'SMTP_HOST', 'smtp.zoho.com'), \
             patch('auth.ldap_lookup_email', return_value=None), \
             patch('notify.send_user_email') as envoi:
            self.assertFalse(user_lifecycle.prevenir_mot_de_passe_change(CIBLE))
        self.assertFalse(envoi.called)

    def test_envoi_au_bon_destinataire_et_message_distinct(self):
        with patch.object(user_lifecycle, 'SMTP_HOST', 'smtp.zoho.com'), \
             patch('auth.ldap_lookup_email', return_value='quelqu-un@example.com'), \
             patch('notify.send_user_email', return_value=True) as envoi:
            self.assertTrue(user_lifecycle.prevenir_mot_de_passe_change(CIBLE))
            self.assertTrue(user_lifecycle.prevenir_mot_de_passe_change(
                CIBLE, par_admin=ADMIN))
        self.assertEqual(envoi.call_args_list[0][0][0], 'quelqu-un@example.com')
        # Le message dit QUI a changé le mot de passe : c'est toute la valeur
        # de la notification pour la personne qui la reçoit.
        self.assertIn("Ton mot de passe", envoi.call_args_list[0][0][2])
        self.assertIn("Un administrateur", envoi.call_args_list[1][0][2])


class BalayageHygieneTest(BaseComptes):
    """Le balayage du démarrage ne doit supprimer que ce qui est MORT.

    Le risque de ce genre de nettoyage est de faire disparaître une session
    encore valide, ce qui déconnecterait tout le monde — d'où un test qui
    vérifie les deux côtés : les lignes périmées partent, les vivantes restent.
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
    """L'inventaire des tables purgées doit rester honnête : toute table qui
    porte un `username` et n'est pas dans la liste doit être un choix."""

    NON_PURGEES_VOLONTAIREMENT = {'audit_log', 'local_users'}

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
