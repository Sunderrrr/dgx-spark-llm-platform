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
# Ces symboles ont quitte le monolithe pour websearch_tools.py (28/08) : on les
# vise dans leur module proprietaire plutot que de faire de app.py une facade.
import websearch_tools as outils_web


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
                  "renseigne-toi en ligne là-dessus"):
            self.assertTrue(outils_web._recherche_pertinente(self._u(t)), t)

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
        out = portal._sans_versions_perimees(h)
        self.assertNotIn('V1', out[1]['content'])
        self.assertIn('version précédente', out[1]['content'])
        self.assertIn('V2', out[3]['content'])

    def test_deux_fichiers_distincts_gardent_chacun_leur_version(self):
        h = [self._msg('assistant', self._fichier('index.html', 'HTML1')),
             self._msg('assistant', self._fichier('style.css', 'CSS1'))]
        out = portal._sans_versions_perimees(h)
        self.assertIn('HTML1', out[0]['content'])
        self.assertIn('CSS1', out[1]['content'])

    def test_le_code_colle_par_l_utilisateur_n_est_jamais_touche(self):
        colle = "```html\n<!-- COLLE -->\n" + ("y" * 3000) + "\n```"
        h = [self._msg('user', colle),
             self._msg('assistant', self._fichier('index.html', 'V1'))]
        out = portal._sans_versions_perimees(h)
        self.assertIn('COLLE', out[0]['content'])

    def test_un_court_extrait_ne_perime_rien(self):
        h = [self._msg('assistant', self._fichier('index.html', 'V1')),
             self._msg('assistant', "Regarde :\n\n```js\nconst a = 1;\n```")]
        out = portal._sans_versions_perimees(h)
        self.assertIn('V1', out[0]['content'])

    def test_la_prose_autour_du_bloc_est_conservee(self):
        h = [self._msg('assistant', self._fichier('index.html', 'V1') + "\n\nJ'ai ajouté le son."),
             self._msg('assistant', self._fichier('index.html', 'V2'))]
        out = portal._sans_versions_perimees(h)
        self.assertIn("J'ai ajouté le son.", out[0]['content'])

    def test_le_gain_est_reel(self):
        h = [self._msg('assistant', self._fichier('index.html', f'V{i}', 20000)) for i in range(4)]
        avant = sum(len(m['content']) for m in h)
        apres = sum(len(m['content']) for m in portal._sans_versions_perimees(h))
        self.assertLess(apres, avant * 0.4)

    def test_conversation_sans_code_inchangee(self):
        h = [self._msg('user', 'bonjour'), self._msg('assistant', 'salut')]
        self.assertEqual(portal._sans_versions_perimees(h), h)


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

    Audit du 24/08 : 108 routes, 7 publiques, aucun oubli. Ce test fige ce
    résultat. Il ne lit PAS le source — il parcourt le `url_map` de Flask et
    interroge le marqueur `_garde` posé par login_required/admin_required, donc
    il voit aussi une route enregistrée autrement que par un `@app.route`
    littéral.

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
        'internal_authcheck': "appelé par Traefik (forwardAuth), jamais par un navigateur ; "
                              "ne renvoie aucune donnée",
        'static':             "fichiers statiques servis par Flask",
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
