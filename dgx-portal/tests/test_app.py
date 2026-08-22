"""Garde-fous d'authentification et de rendu des réponses du portail.

Ces tests couvrent ce qui a réellement cassé en production par le passé :
l'open-redirect du paramètre `next`, le verrouillage anti-brute-force (qui
vivait en mémoire de process et repartait à zéro à chaque redéploiement), la
protection CSRF, et la collision possible entre un outil de serveur MCP et un
outil privilégié intégré.
"""

import time
import unittest

import app as portal


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
        self.assertTrue(portal._mcp_tool_name(3, 'search').startswith('mcp_3_'))

    def test_pas_de_collision_avec_les_outils_integres(self):
        integres = {'create_api_key', 'revoke_api_key', 'request_budget',
                    'request_model', 'launch_model', 'stop_model', 'use_skill'}
        for nom in list(integres) + ['../create_api_key', 'create api key']:
            self.assertNotIn(portal._mcp_tool_name(1, nom), integres)

    def test_caracteres_dangereux_neutralises(self):
        genere = portal._mcp_tool_name(1, 'a b/c\\d"e')
        self.assertTrue(all(c.isalnum() or c in '_-' for c in genere), genere)


class CleanReplyTest(unittest.TestCase):
    def test_retire_le_bloc_de_raisonnement(self):
        self.assertEqual(portal._clean_reply('<think>bla</think>Bonjour'), 'Bonjour')

    def test_garde_ce_qui_suit_le_marqueur_final(self):
        self.assertEqual(portal._clean_reply('cheminement...\n### Réponse\nVoilà'), 'Voilà')

    def test_texte_simple_inchange(self):
        self.assertEqual(portal._clean_reply('Bonjour'), 'Bonjour')


class SseFramingTest(unittest.TestCase):
    """Le frontend ne lit que les lignes `data:` ; le cadrage doit rester exact."""

    def test_trame_texte(self):
        trame = portal._sse_text('salut')
        self.assertTrue(trame.startswith('data: '))
        self.assertTrue(trame.endswith('\n\n'))
        self.assertIn('salut', trame)

    def test_echappe_les_sauts_de_ligne(self):
        # Un \n brut couperait la trame SSE en deux et casserait le flux.
        self.assertNotIn('\n', portal._sse_text('a\nb')[:-2])

    def test_done_optionnel(self):
        self.assertIn('[DONE]', ''.join(portal._sse_chunks('x', done=True)))
        self.assertNotIn('[DONE]', ''.join(portal._sse_chunks('x', done=False)))


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
            self.assertIn(nom, portal.GUARDED_TOOLS)

    def test_les_actions_inoffensives_ne_le_sont_pas(self):
        for nom in ('request_budget', 'request_model', 'list_models'):
            self.assertNotIn(nom, portal.GUARDED_TOOLS)


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
        out = portal._history_for_model(h, '', 262144)
        self.assertEqual(len(out[-1]['content']), 57_000)

    def test_sans_contexte_connu_on_ne_touche_a_rien(self):
        h = self._msgs(10, 900_000)
        self.assertEqual(portal._history_for_model(h, '', None), h)

    def test_le_debordement_retire_les_plus_anciens(self):
        # budget = (32768 - 8192) * 3 = 73 728 caractères
        h = self._msgs(40_000, 40_000, 40_000, 500)
        out = portal._history_for_model(h, '', 32768)
        self.assertLess(len(out), len(h))
        # le dernier échange survit toujours, entier
        self.assertEqual(out[-1]['content'], h[-1]['content'])
        self.assertEqual(len(out[-2]['content']), 40_000)

    def test_jamais_moins_de_deux_messages(self):
        h = self._msgs(500_000, 500_000)
        out = portal._history_for_model(h, '', 32768)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(out[0]['content']), 500_000)

    def test_le_systeme_compte_dans_le_budget(self):
        h = self._msgs(30_000, 30_000, 30_000, 100)
        sans = portal._history_for_model(list(h), '', 32768)
        avec = portal._history_for_model(list(h), 'y' * 20_000, 32768)
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
        out = portal._contexte_outils(msgs)
        self.assertTrue(all(len(m['content']) <= portal.OUTILS_MSG_MAX + 8 for m in out))

    def test_le_total_reste_borne(self):
        msgs = [{'role': 'user', 'content': 'y' * 20_000} for _ in range(20)]
        out = portal._contexte_outils(msgs)
        self.assertLessEqual(sum(len(m['content']) for m in out),
                             portal.OUTILS_TOTAL_MAX + portal.OUTILS_MSG_MAX)

    def test_le_dernier_message_est_toujours_la(self):
        msgs = [{'role': 'user', 'content': 'z' * 30_000} for _ in range(10)]
        msgs.append({'role': 'user', 'content': 'CE QUE JE DEMANDE'})
        out = portal._contexte_outils(msgs)
        self.assertIn('CE QUE JE DEMANDE', out[-1]['content'])

    def test_le_systeme_est_conserve_en_tete(self):
        msgs = [{'role': 'system', 'content': 'consignes'},
                {'role': 'user', 'content': 'bonjour'}]
        out = portal._contexte_outils(msgs)
        self.assertEqual(out[0]['role'], 'system')
        self.assertIn('consignes', out[0]['content'])

    def test_debut_et_fin_conserves_dans_un_message_coupe(self):
        # La demande est souvent en tête, la dernière consigne en queue : c'est le
        # ventre du fichier qui n'apprend rien.
        msgs = [{'role': 'user', 'content': 'DEBUT' + 'm' * 60_000 + 'FIN'}]
        out = portal._contexte_outils(msgs)
        self.assertIn('DEBUT', out[-1]['content'])
        self.assertIn('FIN', out[-1]['content'])

    def test_conversation_courte_passe_telle_quelle(self):
        msgs = [{'role': 'user', 'content': 'salut'},
                {'role': 'assistant', 'content': 'bonjour'}]
        self.assertEqual([m['content'] for m in portal._contexte_outils(msgs)],
                         ['salut', 'bonjour'])
