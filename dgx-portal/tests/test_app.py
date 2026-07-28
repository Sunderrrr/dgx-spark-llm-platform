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
