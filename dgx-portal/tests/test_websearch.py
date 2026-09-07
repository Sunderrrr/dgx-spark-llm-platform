"""Recherche web : la barrière anti-SSRF et le bornage de ce qui part au modèle.

Le réseau interdit déjà au crawler d'atteindre l'hôte ; ces tests couvrent la
dernière barrière, celle que le réseau ne pose pas : aucune URL privée ne doit
être transmise au crawler, quelle que soit la façon dont elle est écrite.
"""
import unittest
from unittest import mock

import websearch
import websearch_tools


def _lire_avec(texte):
    """Fait passer `texte` pour le markdown rendu par le crawler."""
    def faux_post(url, **kw):
        r = mock.Mock(); r.raise_for_status = lambda: None
        r.json = lambda: {'results': [{'url': u, 'success': True,
                                       'markdown': {'raw_markdown': texte},
                                       'metadata': {'title': 't'}}
                                      for u in kw['json']['urls']]}
        return r
    with mock.patch('websearch.requests.post', side_effect=faux_post), \
         mock.patch('websearch.socket.getaddrinfo',
                    return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
        pages, _ = websearch.lire(['https://exemple.fr/a'])
    return pages[0]


class DeclenchementTest(unittest.TestCase):
    """La recherche ne part que sur une directive explicite de l'utilisateur.

    Cinq versions « intelligentes » de ce seuil ont échoué en prod (recherches
    subies) : ce test fige la règle stricte — le mot WEB/INTERNET/GOOGLE doit
    être présent dans la demande, sinon on ne cherche pas.
    """

    def _pertinent(self, texte):
        return websearch_tools._recherche_pertinente(
            [{'role': 'user', 'content': texte}])

    def test_directives_explicites(self):
        for texte in (
            "cherche sur internet pourquoi vLLM crash au boot",
            "regarde sur le web les prix du DGX Spark",
            "va voir sur internet ce que vaut le poste",
            "lance une recherche web là-dessus",
            "fais une recherche sur les ondes sonores",
            "recherche web : dernières news llama.cpp",
            "search the web for the vllm 0.28 release notes",
            "google-le stp",
        ):
            self.assertTrue(self._pertinent(texte), texte)

    def test_faux_positifs_jamais(self):
        for texte in (
            # « en ligne » décrit un état, pas une demande de chercher sur le web.
            "je cherche à mettre mon site en ligne",
            "regarde pourquoi mon serveur n'est plus en ligne",
            "mon pod est en ligne mais la réponse est vide",
            "il faut chercher la fuite en ligne 42 du fichier",
            # « je cherche » décrit le but de l'utilisateur, pas un ordre à l'outil.
            "je cherche pourquoi mon conteneur plante au démarrage",
            "cherche dans le code la fonction qui nettoie ça",
        ):
            self.assertFalse(self._pertinent(texte), texte)

    def test_le_code_colle_ne_declenche_rien(self):
        # Un lien Google dans du code collé ne doit pas compter comme une
        # directive (constaté : trois recherches vides sur « index (2).html »).
        colle = "```html\n<link href=\"https://fonts.googleapis.com/css2?family=Inter\">\n```"
        self.assertFalse(self._pertinent(colle + " corrige ce lien de police"))


class UrlPubliqueTest(unittest.TestCase):
    def test_schemas_exotiques_refuses(self):
        for u in ('file:///etc/passwd', 'ftp://exemple.fr', 'gopher://x',
                  'javascript:alert(1)', 'data:text/html,<b>x</b>'):
            ok, err = websearch.url_publique(u)
            self.assertFalse(ok, u)
            self.assertTrue(err)

    def test_ip_privees_ecrites_en_clair(self):
        for u in ('http://127.0.0.1/', 'http://10.0.0.5/', 'http://192.168.1.1/',
                  'http://172.19.0.1:8001/', 'http://169.254.169.254/',
                  'http://[::1]/', 'http://100.73.45.103/'):
            ok, _ = websearch.url_publique(u)
            self.assertFalse(ok, u)

    def test_hote_qui_resout_en_prive_refuse(self):
        with mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('127.0.0.1', 0))]):
            ok, _ = websearch.url_publique('https://interne.exemple.fr/')
            self.assertFalse(ok)

    def test_une_seule_ip_privee_suffit_a_refuser(self):
        # Un hôte peut annoncer plusieurs adresses : une seule privée doit suffire.
        with mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0)),
                                      (2, 1, 6, '', ('10.1.2.3', 0))]):
            ok, _ = websearch.url_publique('https://double.exemple.fr/')
            self.assertFalse(ok)

    def test_hote_public_accepte(self):
        with mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            ok, err = websearch.url_publique('https://exemple.fr/page')
            self.assertTrue(ok, err)

    def test_hote_introuvable_refuse(self):
        with mock.patch('websearch.socket.getaddrinfo',
                        side_effect=websearch.socket.gaierror):
            ok, _ = websearch.url_publique('https://nexistepas.invalid/')
            self.assertFalse(ok)


class RechercheTest(unittest.TestCase):
    def _reponse(self, resultats):
        r = mock.Mock()
        r.ok = True
        r.raise_for_status = lambda: None
        r.json = lambda: {'results': resultats}
        return r

    def test_les_liens_prives_sont_ecartes_des_resultats(self):
        with mock.patch('websearch.requests.get', return_value=self._reponse([
            {'url': 'http://127.0.0.1/secret', 'title': 'interne'},
            {'url': 'https://exemple.fr/a', 'title': 'public'},
        ])), mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            res, err = websearch.rechercher('test')
        self.assertIsNone(err)
        self.assertEqual([r['url'] for r in res], ['https://exemple.fr/a'])

    def test_doublons_supprimes_et_nombre_borne(self):
        items = [{'url': f'https://exemple.fr/{i}', 'title': str(i)} for i in range(30)]
        items += [{'url': 'https://exemple.fr/0', 'title': 'doublon'}]
        with mock.patch('websearch.requests.get', return_value=self._reponse(items)), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            res, _ = websearch.rechercher('test', nombre=99)
        self.assertEqual(len(res), websearch.MAX_RESULTATS)
        self.assertEqual(len({r['url'] for r in res}), len(res))

    def test_question_vide_refusee(self):
        res, err = websearch.rechercher('   ')
        self.assertEqual(res, [])
        self.assertTrue(err)

    def test_moteur_injoignable_ne_leve_pas(self):
        with mock.patch('websearch.requests.get', side_effect=OSError('boum')):
            res, err = websearch.rechercher('test')
        self.assertEqual(res, [])
        self.assertIn('injoignable', err)


class LectureTest(unittest.TestCase):
    def test_une_url_privee_n_est_jamais_transmise_au_crawler(self):
        appels = []

        def faux_post(url, **kw):
            appels.append(kw.get('json', {}).get('urls'))
            r = mock.Mock(); r.raise_for_status = lambda: None
            r.json = lambda: {'results': []}
            return r

        with mock.patch('websearch.requests.post', side_effect=faux_post), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            pages, _ = websearch.lire(['http://169.254.169.254/latest/meta-data/',
                                       'https://exemple.fr/ok'])
        self.assertEqual(appels, [['https://exemple.fr/ok']])
        self.assertTrue(any(p.get('erreur') for p in pages))

    def test_contenu_borne_par_page_et_au_total(self):
        gros = 'x' * 100_000
        def faux_post(url, **kw):
            r = mock.Mock(); r.raise_for_status = lambda: None
            r.json = lambda: {'results': [
                {'url': u, 'success': True, 'markdown': {'raw_markdown': gros},
                 'metadata': {'title': 't'}} for u in kw['json']['urls']]}
            return r
        with mock.patch('websearch.requests.post', side_effect=faux_post), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            pages, _ = websearch.lire([f'https://exemple.fr/{i}' for i in range(10)])
        self.assertLessEqual(len(pages), websearch.MAX_PAGES)
        for p in pages:
            self.assertLessEqual(len(p.get('contenu', '')), websearch.MAX_CARS_PAGE)
        total = sum(len(p.get('contenu', '')) for p in pages)
        self.assertLessEqual(total, websearch.MAX_CARS_TOTAL)

    def test_crawler_injoignable_ne_leve_pas(self):
        with mock.patch('websearch.requests.post', side_effect=OSError('boum')), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            pages, err = websearch.lire(['https://exemple.fr/a'])
        self.assertEqual(pages, [])
        self.assertIn('injoignable', err)


class NettoyageTest(unittest.TestCase):
    """Le bandeau se retire ligne à ligne — on ne jette PAS la page.

    Mesuré en vrai : sur letelegramme.fr le premier vrai titre n'arrive qu'à la
    4ᵉ ligne, et sur tf1info.fr chaque titre est préfixé de « Nouvelle
    notification » — y compris celui qu'on cherchait. Juger la page sur son début
    revenait à jeter des pages qui contenaient la réponse.
    """

    def test_bandeau_retire_mais_article_conserve(self):
        brut = ("Votre carte de paiement arrive à expiration. Mettez la à jour.\n"
                "Continuer sans accepter →\n"
                "## Dans le Morbihan, la liquidation d'une entreprise du bâtiment\n"
                "laisse clients, artisans et salariés dans l'impasse.")
        net = websearch.nettoyer(brut)
        self.assertNotIn('carte de paiement', net)
        self.assertNotIn('Continuer sans accepter', net)
        self.assertIn('Morbihan', net)

    def test_prefixe_de_notification_retire_sans_perdre_le_titre(self):
        brut = ("* Nouvelle notificationEN DIRECT - Guerre en Ukraine : Macron annonce\n"
                "* Vidéo Nouvelle notification\"The Voice Kids 2026\" : des gages")
        net = websearch.nettoyer(brut)
        self.assertNotIn('Nouvelle notification', net)
        self.assertIn('Guerre en Ukraine', net)

    def test_les_lignes_courtes_de_code_ne_sont_jamais_retirees(self):
        # Une doc technique est pleine de lignes courtes : filtrer par longueur
        # détruirait le code. On ne filtre que sur des motifs de bandeau.
        brut = "async def main():\n    await asyncio.gather(a(), b())\n}\n)\nreturn x"
        net = websearch.nettoyer(brut)
        for l in ('async def main():', 'asyncio.gather', 'return x', '}'):
            self.assertIn(l, net)

    def test_page_reduite_a_rien_est_signalee(self):
        p = _lire_avec("Continuer sans accepter\nutilisons des cookies\naccepter les cookies")
        self.assertIn('erreur', p)

    def test_page_javascript_vide_signalee(self):
        p = _lire_avec("A required part of this site couldn't load.")
        self.assertIn('erreur', p)

    def test_article_normal_conserve(self):
        article = ("Le président a annoncé mardi une nouvelle livraison de matériel "
                   "destinée à renforcer la défense antiaérienne du pays.\n") * 12
        p = _lire_avec(article)
        self.assertNotIn('erreur', p)
        self.assertGreater(len(p['contenu']), 200)

    def test_documentation_technique_conservee(self):
        doc = ("asyncio.TaskGroup regroupe plusieurs tâches concurrentes et attend "
               "leur achèvement collectif de manière structurée.\n") * 12
        p = _lire_avec(doc)
        self.assertNotIn('erreur', p)
        self.assertIn('TaskGroup', p['contenu'])


class ReglagesExtractionTest(unittest.TestCase):
    def test_les_reglages_mesures_partent_bien_au_crawler(self):
        vus = {}

        def faux_post(url, **kw):
            vus.update(kw['json']['crawler_config']['params'])
            r = mock.Mock(); r.raise_for_status = lambda: None
            r.json = lambda: {'results': []}
            return r

        with mock.patch('websearch.requests.post', side_effect=faux_post), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            websearch.lire(['https://exemple.fr/a'])
        self.assertTrue(vus['markdown_generator']['params']['options']['ignore_links'])
        self.assertIn('nav', vus['excluded_tags'])
        self.assertIn('footer', vus['excluded_tags'])
        # L'élagage par pertinence est volontairement ABSENT : il supprimait les
        # blocs de code des pages de documentation.
        self.assertNotIn('content_filter', vus['markdown_generator']['params'])
