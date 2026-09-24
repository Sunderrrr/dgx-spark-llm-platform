"""Garde-fous d'authentification et de rendu des réponses du portail.

Ces tests couvrent ce qui a réellement cassé en production par le passé :
l'open-redirect du paramètre `next`, le verrouillage anti-brute-force (qui
vivait en mémoire de process et repartait à zéro à chaque redéploiement), la
protection CSRF, et la collision possible entre un outil de serveur MCP et un
outil privilégié intégré.
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
# _apply_session vit dans auth.py : c'est lui qui décide du sort du jeton CSRF
# au moment d'ouvrir une session (voir CsrfApresConnexionTest).
import auth
# Le chat a quitte le monolithe pour chat_routes.py (28/08) : on le vise dans
# son module proprietaire.
import chat_routes as chat
# Le support a quitte le monolithe pour support.py (28/08) : on le vise dans
# son module proprietaire.
import support as assistance
# Ces symboles ont quitte le monolithe pour websearch_tools.py (28/08) : on les
# vise dans leur module proprietaire plutot que de faire de app.py une facade.
import websearch_tools as outils_web

import admin_routes


class SafeNextTest(unittest.TestCase):
    """`?next=` ne doit jamais renvoyer ailleurs que sur le portail."""

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
    """Le compteur est en base : il doit être partagé entre workers et
    survivre à un redémarrage."""

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
        # Antidate la première tentative au-delà de la fenêtre : le compteur
        # doit repartir de zéro plutôt que de cumuler indéfiniment.
        portal.get_db().execute("UPDATE login_attempts SET first_at=? WHERE key=?",
                                (time.time() - portal.LOGIN_WINDOW - 10, cle))
        portal.get_db().commit()
        portal._login_fail(cle)
        row = portal.get_db().execute(
            "SELECT fails FROM login_attempts WHERE key=?", (cle,)).fetchone()
        self.assertEqual(row['fails'], 1)


class LoginLockoutParUserTest(unittest.TestCase):
    """Le seuil de verrouillage doit aussi se cumuler par username, pas seulement
    par IP : un botnet qui change d'IP à chaque essai ne doit pas contourner la
    protection (chaque IP seule reste sous le seuil, le compte lui se verrouille)."""

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
        # Username avec '@' (échoue USERNAME_RE) pour ne pas dépendre d'un LDAP
        # joignable : le chemin d'échec reste instantané et purement local.
        return self.client.post('/login',
                                data={'username': username, 'password': password},
                                headers={'X-CSRFToken': 'test-csrf',
                                         'Cf-Connecting-Ip': ip})

    def test_identifiant_invalide_n_ecrit_rien_et_ne_verrouille_pas(self):
        """Un identifiant hors format est refusé AVANT toute écriture en base.

        `username` vient d'un formulaire non authentifié et sert de clé à
        `login_attempts` : sans filtre, un nom neuf de 4 Mo écrivait ~8 Mo de
        lignes par requête (clé différente à chaque fois, donc jamais verrouillé)
        et la table n'est purgée qu'au démarrage.
        """
        r = self._login('198.51.100.250', 'x' * 300)
        self.assertEqual(r.status_code, 401)
        with portal.app.test_request_context():
            row = portal.get_db().execute(
                "SELECT 1 FROM login_attempts WHERE key LIKE 'user:x%'").fetchone()
            self.assertIsNone(row, "rien ne doit être écrit pour un identifiant refusé")

    def test_botnet_ip_rotatives_verrouille_le_user(self):
        # Nom d'utilisateur conforme au format accepté partout (USERNAME_RE) :
        # c'est le compteur par compte qu'on éprouve, pas le filtre d'entrée.
        u = 'bob.cible'
        for i in range(portal.LOGIN_MAX_FAILS - 1):  # seuil - 1, IPs toutes différentes
            self.assertEqual(self._login(f'198.51.100.{i+1}', u).status_code, 401)
        with portal.app.test_request_context():
            row = portal.get_db().execute(
                "SELECT fails FROM login_attempts WHERE key=?", ('user:bob.cible',)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['fails'], portal.LOGIN_MAX_FAILS - 1)
            self.assertEqual(portal._login_locked('user:bob.cible'), 0)
        # Le seuil est atteint sur le compte → verrouillé, même depuis une IP neuve,
        # et la tentative suivante est bloquée AVANT d'être comptée.
        self.assertEqual(self._login('203.0.113.201', u).status_code, 401)
        with portal.app.test_request_context():
            self.assertGreater(portal._login_locked('user:bob.cible'), 0)
            self._login('203.0.113.202', u)
            row = portal.get_db().execute(
                "SELECT fails FROM login_attempts WHERE key=?", ('user:bob.cible',)).fetchone()
            self.assertEqual(row['fails'], portal.LOGIN_MAX_FAILS)


class SessionRegistryTest(unittest.TestCase):
    """Le registre serveur rend la révocation immédiate possible : un compte
    verrouillé / une session révoquée expire tout de suite, même si le cookie
    signé (volé ou rejoué) est encore valide."""

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
        # Session sans sid (postérieure au registre ? non : antérieure, ou de
        # test) : on garde l'expiration par l'âge, pas de révocation forcée —
        # c'est ce qui évite de déconnecter tout le monde à la migration.
        portal.session['username'] = 'bob'
        portal.session['auth_at'] = int(time.time())
        self.assertFalse(portal._session_expired())
        # Mais une session sans sid et trop âgée expire bien par l'âge.
        portal.session['auth_at'] = int(time.time()) - portal.SESSION_MAX_AGE - 10
        self.assertTrue(portal._session_expired())


class CsrfTest(unittest.TestCase):
    """Toute requête non sûre doit porter un jeton CSRF valide."""

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
    """Les endpoints protégés ne doivent rien servir sans session."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self.client = portal.app.test_client()

    def test_endpoints_proteges(self):
        for route in ('/api/settings', '/api/keys', '/api/whoami', '/api/admin'):
            r = self.client.get(route)
            self.assertIn(r.status_code, (302, 401, 403), f"{route} -> {r.status_code}")


class McpToolNameTest(unittest.TestCase):
    """Un serveur MCP hostile ne doit pas pouvoir masquer un outil intégré."""

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
    """Le frontend ne lit que les lignes `data:` ; le cadrage doit rester exact."""

    def test_trame_texte(self):
        trame = chat._sse_text('salut')
        self.assertTrue(trame.startswith('data: '))
        self.assertTrue(trame.endswith('\n\n'))
        self.assertIn('salut', trame)

    def test_echappe_les_sauts_de_ligne(self):
        # Un \n brut couperait la trame SSE en deux et casserait le flux.
        self.assertNotIn('\n', chat._sse_text('a\nb')[:-2])

    def test_done_optionnel(self):
        self.assertIn('[DONE]', ''.join(chat._sse_chunks('x', done=True)))
        self.assertNotIn('[DONE]', ''.join(chat._sse_chunks('x', done=False)))


class AvatarTest(unittest.TestCase):
    def test_liste_blanche_stricte(self):
        # L'id atterrit dans un src d'<img> : pas d'entrée libre.
        self.assertIn('claude', portal.AVATAR_IDS)
        self.assertNotIn('../../etc/passwd', portal.AVATAR_IDS)
        self.assertNotIn('avatar-01', portal.AVATAR_IDS)  # ancien jeu retiré


if __name__ == '__main__':
    unittest.main()


class SessionLifetimeTest(unittest.TestCase):
    """Une session ne doit pas être éternelle : le cookie signé porte
    `is_admin`, un vol de cookie donnait sinon un accès permanent."""

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
        # Sessions émises avant l'ajout d'auth_at : on préfère forcer une
        # reconnexion plutôt que de les traiter comme éternelles.
        with portal.app.test_request_context():
            from flask import session
            session['username'] = 'bob'
            self.assertTrue(portal._session_expired())

    def test_anonyme_non_concerne(self):
        with portal.app.test_request_context():
            self.assertFalse(portal._session_expired())


class GuardedToolsTest(unittest.TestCase):
    """Le résultat d'un outil MCP/compétence est du texte tiers réinjecté dans
    le contexte du modèle : les actions irréversibles doivent être hors de
    portée d'une injection de prompt."""

    def test_les_actions_destructives_sont_gardees(self):
        for nom in ('revoke_api_key', 'launch_model', 'stop_model'):
            self.assertIn(nom, assistance.GUARDED_TOOLS)

    def test_les_actions_inoffensives_ne_le_sont_pas(self):
        for nom in ('request_budget', 'request_model', 'list_models'):
            self.assertNotIn(nom, assistance.GUARDED_TOOLS)


class OidcUsernameTest(unittest.TestCase):
    """preferred_username/nickname/email sont modifiables par l'utilisateur
    dans beaucoup d'IdP, et cette valeur devient la clé de propriété de toutes
    les données : elle doit passer le même filtre que le chemin LDAP."""

    def test_accepte_un_identifiant_normal(self):
        for nom in ('mboitel', 'jean.dupont', 'a-b_c', 'x' * 64):
            self.assertTrue(portal.USERNAME_RE.match(nom), nom)

    def test_rejette_les_identifiants_forges(self):
        for nom in ('', 'a' * 65, '../admin', 'bob@evil.com', 'bob bob',
                    'bob\nadmin', "bob'--", 'bob/../root'):
            self.assertIsNone(portal.USERNAME_RE.match(nom), nom)


class CsrfLazyTest(unittest.TestCase):
    """Régression : le jeton était créé dans before_request, donc CHAQUE
    réponse posait un Set-Cookie. Sur /login, /api/csrf et /api/whoami partent
    en parallèle sans cookie : chacune créait une session neuve avec un jeton
    différent, le dernier Set-Cookie écrasait l'autre, et le POST /login
    partait avec un jeton orphelin → 400, affiché « Identifiants incorrects »."""

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
        # Une requête intercalée ne doit pas faire tourner le jeton.
        client.get('/api/whoami')
        self.assertEqual(client.get('/api/csrf').get_json()['token'], premier)

    def test_post_sans_session_refuse(self):
        client = portal.app.test_client()
        self.assertEqual(client.post('/logout', data={'csrf_token': 'inventé'}).status_code, 400)


class CsrfApresConnexionTest(unittest.TestCase):
    """Régression du 2026-09-14 : ouvrir une session REMPLAÇAIT le jeton CSRF.

    Le navigateur garde celui qu'il a mémorisé au chargement de la page ; à partir
    de là, tout POST gardé partait avec l'ancien et recevait « 400 Bad Request —
    CSRF token manquant ou invalide ». Signalé sur la DÉCONNEXION après un login
    SSO, c'est-à-dire l'endroit où l'utilisateur se retrouve sans aucun moyen de
    sortir. Le jeton vit dans un cookie signé et HttpOnly : le reprendre ne
    rouvre pas la fixation CSRF, et le `sid` neuf continue d'assurer la protection
    contre la fixation de session.
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
        """Le chemin complet du symptôme : session SSO, jeton du client, POST."""
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
        """Le correctif ne doit pas transformer la garde en passoire."""
        client = portal.app.test_client()
        with client.session_transaction() as s:
            s['csrf'] = 'jeton-du-client'
            s['username'] = 'demo'
            s['auth_at'] = int(time.time())
        self.assertEqual(
            client.post('/logout', data={'csrf_token': 'inventé'}).status_code, 400)


class ClientIpTest(unittest.TestCase):
    """La chaîne est client → Cloudflare → Traefik → Next.js → Flask :
    request.remote_addr valait toujours l'IP du conteneur frontend, la même
    pour tout le monde. Le verrou global _login_locked(ip) additionnait donc
    les échecs de TOUS les utilisateurs et bloquait le portail entier."""

    def _ip(self, headers):
        with portal.app.test_request_context(headers=headers,
                                             environ_base={'REMOTE_ADDR': '172.19.0.5'}):
            return portal._client_ip()

    def test_prefere_cf_connecting_ip(self):
        self.assertEqual(self._ip({'Cf-Connecting-Ip': '203.0.113.10',
                                   'X-Forwarded-For': '10.0.0.1, 172.19.0.5'}),
                         '203.0.113.10')

    def test_retombe_sur_le_premier_x_forwarded_for(self):
        self.assertEqual(self._ip({'X-Forwarded-For': '203.0.113.10, 172.19.0.5'}),
                         '203.0.113.10')

    def test_retombe_sur_remote_addr(self):
        self.assertEqual(self._ip({}), '172.19.0.5')

    def test_deux_visiteurs_ne_partagent_pas_le_meme_verrou(self):
        a = self._ip({'Cf-Connecting-Ip': '203.0.113.10'})
        b = self._ip({'Cf-Connecting-Ip': '203.0.113.20'})
        self.assertNotEqual(a, b)


class HistoriqueModeleTest(unittest.TestCase):
    """Ce que le playground renvoie au modèle ne doit JAMAIS être amputé.

    Le bug d'origine : chaque message était tronqué à 8 000 caractères, donc après
    une longue réponse le modèle relisait son propre fichier coupé en plein milieu
    et affirmait s'être interrompu — ce qui était vrai de son point de vue.
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
        # budget = (32768 - 8192) * 3 = 73 728 caractères
        h = self._msgs(40_000, 40_000, 40_000, 500)
        out = chat._history_for_model(h, '', 32768)
        self.assertLess(len(out), len(h))
        # le dernier échange survit toujours, entier
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
    """La phase outils relit une version COURTE de la conversation.

    Lui passer l'historique entier ajoutait un préchargement complet avant la
    réponse : mesuré 30 s sur 100 Ko de contexte, plus de 60 s au-delà — et le
    client abandonnait sur une conversation un peu ancienne, alors qu'une
    conversation neuve fonctionnait. Décider « faut-il chercher ? » ne demande
    pas de relire un fichier de 65 Ko.
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
        # La demande est souvent en tête, la dernière consigne en queue : c'est le
        # ventre du fichier qui n'apprend rien.
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
    """La recherche ne part que sur une directive explicite.

    Cinq versions ont échoué en production en devinant l'intention à partir de
    mots isolés : « google » venait d'une balise de police, « source » d'un
    createBufferSource, « en ligne » d'un « jeu d'échecs en ligne » — celle-là a
    bloqué un utilisateur six fois de suite.
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
                  # « en ligne » n'est plus une cible : il décrit un état plus
                  # souvent qu'il ne désigne le web (« site en ligne »).
                  "renseigne-toi sur internet là-dessus"):
            self.assertTrue(outils_web._recherche_pertinente(self._u(t)), t)

    def test_mettre_en_ligne_ne_declenche_pas(self):
        # 2026-09 : « en ligne » décrit un état ; « je cherche à mettre mon
        # site en ligne » a ouvert une recherche web — c'est fini.
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
    """Seule la dernière version de chaque fichier repart au modèle.

    Mesuré sur les conversations réelles : 42 332 des 72 182 caractères d'un fil
    étaient d'anciennes versions du même fichier, rejouées à chaque message.
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
    """Ce que la recherche ramène est réinjecté en TEXTE, jamais en rôle `tool`.

    Envoyer des `tool_calls` et des messages de rôle `tool` sans déclarer les
    outils donnait une conversation que le gabarit ne sait pas rendre : 35 tokens
    produits, aucun contenu reçu, « The model returned no response ».
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
    """Toute route du portail est authentifiée, sauf une liste explicite.

    Recompte le 2026-09-13 sur le `url_map` vivant : 153 routes, 11 sans garde,
    aucun oubli (146 avant les six routes de gestion des comptes ajoutées ce
    jour-là : blocage, déblocage, détail, sessions et mot de passe en
    autonomie — toutes gardées). Ce test fige ce résultat. Il ne lit PAS le source — il parcourt
    le `url_map` de Flask et interroge le marqueur `_garde` posé par
    login_required/admin_required, donc il voit aussi une route enregistrée
    autrement que par un `@app.route` littéral.

    Le vrai risque couvert n'est pas l'état actuel du code mais son futur : le
    conteneur OCR exécute du code de modèle tiers (`--trust-remote-code`) et
    partage `ocr_net` avec le portail. Une route publique ajoutée par
    inadvertance deviendrait joignable depuis ce code-là. Si ce test échoue,
    la question n'est pas « comment le faire passer » mais « cette route
    a-t-elle vraiment vocation à être publique ».
    """

    # Chaque entrée est publique POUR UNE RAISON. On n'en ajoute pas sans
    # savoir dire laquelle.
    PUBLIQUES = {
        'api_config':         "ne renvoie que {oidc_enabled}, lu avant connexion",
        'login':              "point d'entrée de l'authentification",
        'login_sso':          "redirection vers le fournisseur OIDC",
        'oauth_callback':     "retour du fournisseur OIDC, hors session",
        'logout':             "doit marcher même sur une session déjà expirée",
        'api_csrf':           "délivre le jeton CSRF nécessaire pour se connecter",
        # 2e facteur WebAuthn : l'utilisateur n'est PAS encore authentifié (le
        # mot de passe / LDAP vient d'être validé en amont), ce point finalise le
        # login. Il exige le jeton CSRF + un défi one-time + une assertion valide.
        'webauthn.security_verify_login': "finalise un login 2FA (assertion passkey), hors session",
        # Prefixe 'admin.' depuis que l'administration est un blueprint (28/08) :
        # les CHEMINS n'ont pas bougé, seuls les noms d'endpoints.
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
        """Une entrée qui ne correspond plus à aucune route doit disparaître."""
        connues = {r.endpoint for r in portal.app.url_map.iter_rules()}
        self.assertEqual(set(self.PUBLIQUES) - connues, set(),
                         "entrée(s) obsolète(s) dans PUBLIQUES")

    def test_le_marqueur_est_bien_pose(self):
        """Sans marqueur, le test principal passerait en ne voyant rien."""
        gardees = [r.endpoint for r in portal.app.url_map.iter_rules()
                   if getattr(portal.app.view_functions.get(r.endpoint), '_garde', None)]
        self.assertGreater(len(gardees), 90, "le marqueur _garde a disparu des décorateurs")


class NomsResolublesTest(unittest.TestCase):
    """Aucun module du portail ne charge un nom défini nulle part.

    Python résout les globales À L'APPEL. Un nom parti dans un autre module lors
    d'une extraction ne casse donc ni l'import, ni les tests, ni la comparaison
    de table de routes — seulement la requête de l'utilisateur, en production.

    Vécu deux fois le 28/08 pendant le découpage : `_read_uploaded_image` emporté
    avec la section vidéo alors que /api/ocr/extract s'en servait, puis
    `image_ready`/`get_music_model` restés référencés par le tableau de bord des
    sidecars. Les deux importaient proprement et auraient levé un NameError au
    premier clic. Ce test les aurait vus ; c'est pour ça qu'il existe.
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
    """/healthz (liveness publique) et /api/health (état agrégé, connecté)."""

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
        """LiteLLM >= 1.102 répond 401 sur `/health` (clé exigée) : le portail ne
        doit pas en conclure que le proxy est tombé. Régression du 2026-09-24 —
        la montée 1.92 → 1.102.1 a fait afficher « LiteLLM injoignable » sur le
        tableau de bord alors que le proxy servait les requêtes."""
        import unittest.mock as mock

        class Reponse:
            def __init__(self, code):
                self.status_code = code

        def faux_get(url, timeout=None):
            if url.endswith("/health"):          # 1.102 : authentifié
                return Reponse(401)
            if url.endswith("/health/liveliness"):  # public, la vraie sonde
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
    """/api/pending-count : badge sidebar (demandes modèle + budget en attente)."""

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
        # Utilisateur non-admin : uniquement SES demandes, et par TYPE —
        # une demande de budget ne doit jamais allumer le badge « modèle ».
        self.assertEqual(c.get("/api/pending-count").get_json(),
                         {'model': 1, 'budget': 0})
        # Admin : toutes (demo pending + other pending), par type.
        with c.session_transaction() as s:
            s["is_admin"] = True
        self.assertEqual(c.get("/api/pending-count").get_json(),
                         {'model': 1, 'budget': 1})


class AnnonceModeleSupprimeTest(unittest.TestCase):
    """Le fil d'annonces est un « quoi de neuf », pas un journal : retirer un
    modèle du catalogue doit retirer l'annonce qui l'a présenté."""

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
        with patch.object(admin_routes, '_unregister_litellm_model', return_value=True):
            r = c.post(f"/admin/model/delete/{self.mid}",
                       headers={'X-CSRFToken': 'test-csrf'})
        # Contrat JSON : plus de redirection. Le `flash` n'était rendu par aucun
        # template et la réponse HTML de la redirection passait pour un succès
        # côté interface (son `catch` traitait le non-JSON comme « ok »).
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
    """Subventions temporaires (boost N jours) + redéfinition du plafond."""

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
        """Approuver avec durée : plafond boosté + ligne budget_grants."""
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
            self.assertEqual(g['base_budget'], 200000000)  # retour à la base…
            self.assertEqual(g['current_budget'], 250000000)

    def test_expiration_revient_a_la_base(self):
        """À l'échéance : le compte est ramené au plafond de base."""
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
        self.assertEqual(args.args[1], 200000000)          # …à la base, pas autre chose
        with portal.app.app_context():
            self.assertIsNone(portal.get_db().execute("SELECT * FROM budget_grants").fetchone())

    def test_expiration_necrase_pas_une_decision_admin(self):
        """Si l'admin a redéfini le plafond entre-temps, on n'écrase pas."""
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
        up.assert_not_called()                             # la décision admin fait foi

    def test_redefinir_le_plafond_a_la_baisse(self):
        """/budget/set définit un montant EXACT : 200M → 50M, et purge les boosts."""
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
        self.assertEqual(args.args[1], 50000000)           # montant exact, pas +
        with portal.app.app_context():
            self.assertIsNone(portal.get_db().execute("SELECT * FROM budget_grants").fetchone())

    def test_redefinir_refuse_les_montants_irrealistes(self):
        with patch.object(admin_routes, 'litellm_update_user_budget', return_value=True) as up:
            c = portal.app.test_client()
            self._login(c)
            c.post("/admin/users/demo/budget/set", data={"budget": "6666726666666", "csrf_token": "test-csrf"})
            up.assert_not_called()                         # typo illimitante refusée


class BudgetPeriodTest(unittest.TestCase):
    """Découpage de la fenêtre budgétaire + calcul du quota restant."""

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
        # Plancher : jamais négatif quand on a dépassé le budget. On vide le
        # cache entre les deux (sinon le TTL renvoie la valeur précédente).
        portal._BUDGET_CACHE.clear()
        with mock.patch.object(portal, "_real_tokens_by_user", return_value={'budget-test': 99999}):
            used, remaining = portal._budget_remaining('budget-test', 1000, '7d')
            self.assertEqual(remaining, 0)


class PromMetricsTest(unittest.TestCase):
    """/metrics (Prometheus) : texte pour un scrape LAN — et REFUSÉ dès que la
    requête arrive par l'edge Cloudflare (le rewrite du frontend l'exposait)."""

    def test_metriques_publiques_sur_le_lan(self):
        c = portal.app.test_client()
        r = c.get("/metrics")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("cronos_cpu_pct", body)
        self.assertIn("cronos_model_online", body)
        self.assertIn("cronos_gpu_util_pct", body)

    def test_metrics_refuse_via_cloudflare(self):
        """Cloudflare pose Cf-Connecting-Ip sur TOUTE requête qui traverse
        l'edge : sa présence prouve que l'appel vient d'internet (dgx.cronos.website)
        et non du LAN/netbird, donc on ne sert pas les métriques de l'hôte."""
        c = portal.app.test_client()
        r = c.get("/metrics", headers={"Cf-Connecting-Ip": "203.0.113.7"})
        self.assertEqual(r.status_code, 403)
        body = r.get_data(as_text=True)
        self.assertNotIn("cronos_cpu_pct", body)
        self.assertNotIn("cronos_gpu_util_pct", body)


class MediaCancelTest(unittest.TestCase):
    """Annulation d'une génération image/musique/vidéo (bouton « Arrêter »)."""

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
        # before_request vérifie le CSRF avant toute chose (défense en profondeur).
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
    """Partage d'une conversation : lien en lecture seule, RÉSERVÉ AUX CONNECTÉS
    (exigence du 2026-09-09 : un lien qui fuit ne montre rien à un anonyme)."""

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
        # Pas de session → before_request renvoie 400 (jeton manquant) avant toute
        # autre considération : on ne crée jamais un partage anonyme.
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
        """Le playground envoie du JSON (sendJSON), pas du form : accepté."""
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
        """Anonyme + token valide → redirection login, JAMAIS la conversation."""
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
        # Connecté + token inconnu → 404. (Anonyme, c'est une redirection login
        # qui prime : on ne révèle même pas l'existence du lien.)
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = "demo"
            s["auth_at"] = int(time.time())
        self.assertEqual(c.get("/c/nimportequoi").status_code, 404)


class AuditLogTest(unittest.TestCase):
    """Journal d'audit : écriture + lecture admin."""

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

    def _login(self, username="mael", is_admin=False):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["auth_at"] = int(time.time())
            s["is_admin"] = is_admin
            s["csrf"] = "test-csrf"
        return c

    def test_liste_et_compteur(self):
        from db import add_notification
        add_notification("mael", "image", "Génération image terminée (2/2).")
        add_notification("mael", "request", "Budget accordé : +100 tokens.")
        c = self._login()
        data = c.get("/api/notifications").get_json()
        self.assertEqual(data["unread"], 2)
        self.assertEqual(len(data["items"]), 2)
        self.assertTrue(all(not i["seen"] for i in data["items"]))

    def test_marquage_lu(self):
        from db import add_notification
        add_notification("mael", "image", "Génération image terminée (1/1).")
        c = self._login()
        self.assertEqual(c.get("/api/notifications").get_json()["unread"], 1)
        r = c.post("/api/notifications/seen", headers={"X-CSRFToken": "test-csrf"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/notifications").get_json()["unread"], 0)

    def test_sans_session_est_refuse(self):
        c = portal.app.test_client()
        self.assertIn(c.get("/api/notifications").status_code, (302, 401, 403))
        # POST sans CSRF → 400 (before_request CSRF avant login_required).
        self.assertEqual(c.post("/api/notifications/seen").status_code, 400)

    def test_docs_requiert_login(self):
        c = portal.app.test_client()
        self.assertIn(c.get("/docs").status_code, (302, 401, 403))
        c2 = self._login()
        r = c2.get("/docs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("application", r.get_data(as_text=True).lower())


class PlaygroundTitleSummarizeTest(unittest.TestCase):
    """Routes auto-titre / résumé du playground : POST login_required, CSRF."""

    def _paths(self):
        rules = {str(r) for r in portal.app.url_map.iter_rules()}
        return rules

    def test_routes_enregistrees(self):
        self.assertIn('/api/playground/title', self._paths())
        self.assertIn('/api/playground/summarize', self._paths())

    def test_post_sans_csrf_refuse(self):
        c = portal.app.test_client()
        # before_request CSRF court avant login_required → 400 sur POST.
        self.assertEqual(c.post('/api/playground/title').status_code, 400)
        self.assertEqual(c.post('/api/playground/summarize').status_code, 400)

    def test_sans_session_refuse(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "test-csrf"
        r = c.post('/api/playground/title', headers={"X-CSRFToken": "test-csrf"}, json={"messages": [{"role": "user", "content": "Bonjour"}]})
        self.assertIn(r.status_code, (200, 302, 401, 403, 409, 502))


class PlaygroundTitleSummarizeMockTest(unittest.TestCase):
    """Routes auto-titre/résumé avec modèle mocké : réponse JSON attendue."""

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
        """En maintenance, ces deux routes doivent refuser AVANT d'appeler le modèle.

        Elles appelaient LiteLLM sans aucune garde : le chat répondait le message
        de maintenance pendant que l'auto-titre partait quand même (mesuré : 30
        appels → 30×200 avec de vrais appels). Un modèle en cours de libération
        était donc sollicité exactement pendant la fenêtre qu'on veut protéger.
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
                # La garde passe AVANT l'appel : rien n'est parti à LiteLLM.
                non_stream.assert_not_called()
        finally:
            with portal.app.app_context():
                set_setting('maintenance_mode', '0')

    def test_plafond_de_debit_s_applique_au_titre(self):
        """Sans plafond, une boucle côté client tenait les threads gunicorn.

        Le plafond est PROPRE à la route (`rl-titre`), pas partagé avec le chat :
        le partager diviserait par deux le budget de messages de l'utilisateur,
        puisque le titre part après chaque premier message.
        """
        import unittest.mock as mock
        import guards
        c = portal.app.test_client()
        # Compte dédié : le bucket est en SQLite et partagé par le processus de
        # test, les tests voisins du fichier consomment déjà `rl-titre`.
        self._login(c, username="plafond-titre")
        with mock.patch.object(chat, "get_running_models", return_value=["fake-model"]), \
             mock.patch.object(chat, "_non_stream", return_value=("Titre", None)), \
             mock.patch.object(guards, "CHAT_RATE_MAX", 2):
            codes = [c.post("/api/playground/title", headers={"X-CSRFToken": self.CSRF},
                            json={"messages": [{"role": "user", "content": "Bonjour"}]}).status_code
                     for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


class SupportBillingTest(unittest.TestCase):
    """L'assistant Support consomme le GPU AU NOM de l'utilisateur : il doit
    passer par SA clé (celle qui porte le quota LiteLLM), jamais par la clé
    master — sinon le budget est contourné et la consommation n'apparaît dans
    aucun compte (audit sécurité)."""

    CSRF = "test-csrf"

    class _FakeStream:
        """Réponse SSE minimale : un fragment de texte, puis [DONE]."""

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
            r.get_data(as_text=True)      # force l'exécution du générateur SSE
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


class SupportSensibleActionsTest(unittest.TestCase):
    """Les actions sensibles du Support ne s'exécutent QUE sur confirmation.

    Avant : la consigne « demande toujours confirmation » vivait dans le prompt,
    et rien côté serveur n'empêchait le modèle de révoquer une clé ou d'arrêter
    le modèle servi de sa propre initiative. Désormais la boucle de chat DÉPOSE
    une demande, l'interface affiche un bouton, et /support/confirm est le seul
    chemin d'exécution — jeton opaque, lié à l'utilisateur, à usage unique.
    """

    CSRF = "test-csrf"

    class _Flux:
        """Flux SSE simulé (les appels successifs reçoivent des frames différents)."""

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

        # `_fin_support` est neutralisé : c'est un thread lancé après la réponse
        # (fil sauvé + extraction mémoire) qui rappelle requests.post une fois le
        # tour fini et volerait une frame simulée au test suivant.
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
        # Rien n'a été exécuté, et l'interface reçoit de quoi afficher le bouton.
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
            # Deuxième clic (ou rejeu) : la demande n'est plus « pending ».
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
        """create_api_key s'exécute directement : pas de bouton pour le non destructif."""
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
    """Contenu externe (MCP/skill) lu → les actions sensibles sont refusées.

    Ce garde-fou préexistait à la confirmation par jeton, et la boucle de chat a
    été modifiée juste à côté : sans ce test, une régression passerait inaperçue.
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
            [self._outil("mcp_serveur_outil", {}), "data: [DONE]"],       # lecture externe
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
        # Ni exécution, ni proposition de confirmation : le refus est net.
        exec_tool.assert_not_called()
        self.assertNotIn("cronos_confirm", body)
        # Le corps SSE est du JSON : les accents y sont échappés (\u00e9), on
        # vérifie donc la partie ASCII du message.
        self.assertIn("Action bloqu", body)
        with portal.app.app_context():
            n_pending = portal.get_db().execute(
                "SELECT COUNT(*) c FROM pending_actions WHERE username='demo'").fetchone()["c"]
        self.assertEqual(n_pending, 0)

    def test_creation_de_cle_refusee_apres_contenu_externe(self):
        """`create_api_key` n'est pas destructif, mais il DÉLIVRE un secret.

        Il s'exécute donc directement dans le cas normal (choix produit, cf.
        `test_une_action_non_sensible_n_est_pas_retardee`) — sauf après lecture
        d'un contenu externe : sinon une page hostile faisait créer une clé au
        nom de l'utilisateur connecté, et sa valeur entrait dans le contexte du
        modèle (donc dans le fil conservé) sans que personne ne l'ait demandée.
        """
        import unittest.mock as mock
        frames = [
            [self._outil("mcp_serveur_outil", {}), "data: [DONE]"],        # lecture externe
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
        # Ni exécution, ni proposition de confirmation : le refus est net.
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
    """Fin de tour du Support : le fil est conservé, une réponse COUPÉE ne l'est pas.

    Le fil sert à reprendre la conversation après un rechargement de page : le
    figer sur une réponse interrompue ferait réapparaître une phrase tronquée à
    chaque visite, et l'extraction mémoire en tirerait des faits faux.
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
