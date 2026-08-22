"""Recherche web : la barrière anti-SSRF et le bornage de ce qui part au modèle.

Le réseau interdit déjà au crawler d'atteindre l'hôte ; ces tests couvrent la
dernière barrière, celle que le réseau ne pose pas : aucune URL privée ne doit
être transmise au crawler, quelle que soit la façon dont elle est écrite.
"""
import unittest
from unittest import mock

import websearch


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


class MurEtPauvreteTest(unittest.TestCase):
    """Une page qui rend son bandeau au lieu de son article est pire que rien :
    le modèle prendrait le bandeau pour l'information. Mesuré en vrai sur la
    presse française — murs de consentement, d'abonnement, détection de robot.
    """

    def _crawl(self, texte):
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

    def test_mur_de_cookies_signale_et_contenu_ecarte(self):
        p = self._crawl("Continuer sans accepter → Pour soutenir le travail de notre "
                        "rédaction, nous et nos 231 partenaires utilisons des cookies "
                        + "pour stocker des informations sur votre terminal. " * 20)
        self.assertIn('erreur', p)
        self.assertNotIn('contenu', p)

    def test_mur_d_abonnement_signale(self):
        p = self._crawl("Votre abonnement arrive à expiration, mettez à jour votre carte. "
                        * 40)
        self.assertIn('erreur', p)

    def test_page_javascript_vide_signalee(self):
        p = self._crawl("A required part of this site couldn't load.")
        self.assertIn('erreur', p)

    def test_article_normal_conserve(self):
        article = ("Le président a annoncé mardi une nouvelle livraison de matériel "
                   "destinée à renforcer la défense antiaérienne du pays. ") * 12
        p = self._crawl(article)
        self.assertNotIn('erreur', p)
        self.assertIn('contenu', p)
        self.assertGreater(len(p['contenu']), 200)

    def test_documentation_technique_conservee(self):
        doc = ("asyncio.TaskGroup permet de regrouper plusieurs tâches concurrentes "
               "et d'attendre leur achèvement collectif de manière structurée. ") * 12
        p = self._crawl(doc)
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
