"""Web search: the anti-SSRF barrier and the bounding of what goes to the model.

The network already forbids the crawler from reaching the host; these
tests cover the last barrier, the one the network does not set up: no
private URL must be sent to the crawler, however it is written.
"""
import socket
import unittest
from unittest import mock

import websearch
import websearch_tools


def _read_with(text):
    """Passes `texte` off as the markdown rendered by the crawler."""
    def fake_post(url, **kw):
        r = mock.Mock(); r.raise_for_status = lambda: None
        r.json = lambda: {'results': [{'url': u, 'success': True,
                                       'markdown': {'raw_markdown': text},
                                       'metadata': {'title': 't'}}
                                      for u in kw['json']['urls']]}
        return r
    with mock.patch('websearch.requests.post', side_effect=fake_post), \
         mock.patch('websearch.socket.getaddrinfo',
                    return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
        pages, _ = websearch.read(['https://exemple.fr/a'])
    return pages[0]


class TriggerTest(unittest.TestCase):
    """Search only goes out on an explicit directive from the user.

    Five « clever » versions of this threshold failed in prod (suffered
    searches): this test freezes the strict rule — the word
    WEB/INTERNET/GOOGLE must be present in the request, else we do not search.
    """

    def _pertinent(self, text):
        return websearch_tools._search_relevant(
            [{'role': 'user', 'content': text}])

    def test_explicit_directives_(self):
        for text in (
            "cherche sur internet pourquoi vLLM crash au boot",
            "regarde sur le web les prix du DGX Spark",
            "va voir sur internet ce que vaut le poste",
            "lance une recherche web là-dessus",
            "fais une recherche sur les ondes sonores",
            "recherche web : dernières news llama.cpp",
            "search the web for the vllm 0.28 release notes",
            "google-le stp",
        ):
            self.assertTrue(self._pertinent(text), text)

    def test_never_false_positives(self):
        for text in (
            # « en ligne » describes a state, not a request to search the web.
            "je cherche à mettre mon site en ligne",
            "regarde pourquoi mon serveur n'est plus en ligne",
            "mon pod est en ligne mais la réponse est vide",
            "il faut chercher la fuite en ligne 42 du fichier",
            # « je cherche » describes the user's goal, not an order to the tool.
            "je cherche pourquoi mon conteneur plante au démarrage",
            "cherche dans le code la fonction qui nettoie ça",
        ):
            self.assertFalse(self._pertinent(text), text)

    def test_pasted_code_triggers_nothing(self):
        # A Google link in pasted code must not count as a directive (observed:
        # three empty searches on « index (2).html »).
        pasted = "```html\n<link href=\"https://fonts.googleapis.com/css2?family=Inter\">\n```"
        self.assertFalse(self._pertinent(pasted + " corrige ce lien de police"))


class PublicUrlTest(unittest.TestCase):
    def test_exotic_schemes_refused(self):
        for u in ('file:///etc/passwd', 'ftp://exemple.fr', 'gopher://x',
                  'javascript:alert(1)', 'data:text/html,<b>x</b>'):
            ok, err = websearch.public_url(u)
            self.assertFalse(ok, u)
            self.assertTrue(err)

    def test_private_ips_written_plainly(self):
        for u in ('http://127.0.0.1/', 'http://10.0.0.5/', 'http://192.168.1.1/',
                  'http://172.19.0.1:8001/', 'http://169.254.169.254/',
                  'http://[::1]/', 'http://100.64.45.103/'):
            ok, _ = websearch.public_url(u)
            self.assertFalse(ok, u)

    def test_host_resolving_private_refused(self):
        with mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('127.0.0.1', 0))]):
            ok, _ = websearch.public_url('https://interne.exemple.fr/')
            self.assertFalse(ok)

    def test_one_private_ip_is_enough_to_refuse(self):
        # A host can announce several addresses: a single private one must suffice.
        with mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0)),
                                      (2, 1, 6, '', ('10.1.2.3', 0))]):
            ok, _ = websearch.public_url('https://double.exemple.fr/')
            self.assertFalse(ok)

    def test_public_host_accepted(self):
        with mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            ok, err = websearch.public_url('https://exemple.fr/page')
            self.assertTrue(ok, err)

    def test_unknown_host_refused(self):
        with mock.patch('websearch.socket.getaddrinfo',
                        side_effect=websearch.socket.gaierror):
            ok, _ = websearch.public_url('https://nexistepas.invalid/')
            self.assertFalse(ok)


class RechercheTest(unittest.TestCase):
    def _response(self, results):
        r = mock.Mock()
        r.ok = True
        r.raise_for_status = lambda: None
        r.json = lambda: {'results': results}
        return r

    def test_private_links_removed_from_results(self):
        with mock.patch('websearch.requests.get', return_value=self._response([
            {'url': 'http://127.0.0.1/secret', 'title': 'interne'},
            {'url': 'https://exemple.fr/a', 'title': 'public'},
        ])), mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            res, err = websearch.search('test')
        self.assertIsNone(err)
        self.assertEqual([r['url'] for r in res], ['https://exemple.fr/a'])

    def test_duplicates_removed_and_count_bounded(self):
        items = [{'url': f'https://exemple.fr/{i}', 'title': str(i)} for i in range(30)]
        items += [{'url': 'https://exemple.fr/0', 'title': 'doublon'}]
        with mock.patch('websearch.requests.get', return_value=self._response(items)), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            res, _ = websearch.search('test', count_value=99)
        self.assertEqual(len(res), websearch.MAX_RESULTS)
        self.assertEqual(len({r['url'] for r in res}), len(res))

    def test_empty_question_refused(self):
        res, err = websearch.search('   ')
        self.assertEqual(res, [])
        self.assertTrue(err)

    def test_unreachable_engine_does_not_raise(self):
        with mock.patch('websearch.requests.get', side_effect=OSError('boum')):
            res, err = websearch.search('test')
        self.assertEqual(res, [])
        self.assertIn('injoignable', err)


class LectureTest(unittest.TestCase):
    def test_private_url_never_sent_to_crawler(self):
        calls = []

        def fake_post(url, **kw):
            calls.append(kw.get('json', {}).get('urls'))
            r = mock.Mock(); r.raise_for_status = lambda: None
            r.json = lambda: {'results': []}
            return r

        with mock.patch('websearch.requests.post', side_effect=fake_post), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            pages, _ = websearch.read(['http://169.254.169.254/latest/meta-data/',
                                       'https://exemple.fr/ok'])
        self.assertEqual(calls, [['https://exemple.fr/ok']])
        self.assertTrue(any(p.get('error') for p in pages))

    def test_content_bounded_per_page_and_total(self):
        big = 'x' * 100_000
        def fake_post(url, **kw):
            r = mock.Mock(); r.raise_for_status = lambda: None
            r.json = lambda: {'results': [
                {'url': u, 'success': True, 'markdown': {'raw_markdown': big},
                 'metadata': {'title': 't'}} for u in kw['json']['urls']]}
            return r
        with mock.patch('websearch.requests.post', side_effect=fake_post), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            pages, _ = websearch.read([f'https://exemple.fr/{i}' for i in range(10)])
        self.assertLessEqual(len(pages), websearch.MAX_PAGES)
        for p in pages:
            self.assertLessEqual(len(p.get('content', '')), websearch.MAX_CHARS_PAGE)
        total = sum(len(p.get('content', '')) for p in pages)
        self.assertLessEqual(total, websearch.MAX_CHARS_TOTAL)

    def test_unreachable_crawler_does_not_raise(self):
        with mock.patch('websearch.requests.post', side_effect=OSError('boum')), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            pages, err = websearch.read(['https://exemple.fr/a'])
        self.assertEqual(pages, [])
        self.assertIn('injoignable', err)


class CleanupTest(unittest.TestCase):
    """The banner is removed line by line — we do NOT discard the page.

    Measured for real: on letelegramme.fr the first real headline only comes
    at the 4th line, and on tf1info.fr every headline is prefixed with
    « Nouvelle notification » — including the one we were looking for.
    Judging the page by its start amounted to discarding pages that held the answer.
    """

    def test_banner_removed_article_kept(self):
        raw = ("Votre carte de paiement arrive à expiration. Mettez la à jour.\n"
                "Continuer sans accepter →\n"
                "## Dans le Morbihan, la liquidation d'une entreprise du bâtiment\n"
                "laisse clients, artisans et salariés dans l'impasse.")
        net = websearch.clean(raw)
        self.assertNotIn('carte de paiement', net)
        self.assertNotIn('Continuer sans accepter', net)
        self.assertIn('Morbihan', net)

    def test_notification_prefix_removed_title_kept(self):
        raw = ("* Nouvelle notificationEN DIRECT - Guerre en Ukraine : Macron annonce\n"
                "* Vidéo Nouvelle notification\"The Voice Kids 2026\" : des gages")
        net = websearch.clean(raw)
        self.assertNotIn('Nouvelle notification', net)
        self.assertIn('Guerre en Ukraine', net)

    def test_short_code_lines_never_removed(self):
        # A technical doc is full of short lines: filtering by length would
        # destroy the code. We filter only on banner patterns.
        raw = "async def main():\n    await asyncio.gather(a(), b())\n}\n)\nreturn x"
        net = websearch.clean(raw)
        for l in ('async def main():', 'asyncio.gather', 'return x', '}'):
            self.assertIn(l, net)

    def test_page_reduced_to_nothing_reported(self):
        p = _read_with("Continuer sans accepter\nutilisons des cookies\naccepter les cookies")
        self.assertIn('error', p)

    def test_empty_javascript_page_reported(self):
        p = _read_with("A required part of this site couldn't load.")
        self.assertIn('error', p)

    def test_article_normal_conserve(self):
        article = ("Le président a annoncé mardi une nouvelle livraison de matériel "
                   "destinée à renforcer la défense antiaérienne du pays.\n") * 12
        p = _read_with(article)
        self.assertNotIn('error', p)
        self.assertGreater(len(p['content']), 200)

    def test_technical_documentation_kept(self):
        doc = ("asyncio.TaskGroup regroupe plusieurs tâches concurrentes et attend "
               "leur achèvement collectif de manière structurée.\n") * 12
        p = _read_with(doc)
        self.assertNotIn('error', p)
        self.assertIn('TaskGroup', p['content'])


class ExtractionSettingsTest(unittest.TestCase):
    def test_measured_settings_sent_to_crawler(self):
        seen = {}

        def fake_post(url, **kw):
            seen.update(kw['json']['crawler_config']['params'])
            r = mock.Mock(); r.raise_for_status = lambda: None
            r.json = lambda: {'results': []}
            return r

        with mock.patch('websearch.requests.post', side_effect=fake_post), \
             mock.patch('websearch.socket.getaddrinfo',
                        return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            websearch.read(['https://exemple.fr/a'])
        self.assertTrue(seen['markdown_generator']['params']['options']['ignore_links'])
        self.assertIn('nav', seen['excluded_tags'])
        self.assertIn('footer', seen['excluded_tags'])
        # Relevance pruning is deliberately ABSENT: it removed the code blocks
        # of documentation pages.
        self.assertNotIn('content_filter', seen['markdown_generator']['params'])

class RedirectionInterneTest(unittest.TestCase):
    """A public seed can redirect inwards: we refuse the PAGE.

    Why this test exists: crawl4ai only validates the START URLs
    (`_normalize_and_validate_seeds` → `validate_url_destination`, commented
    « before fetching » in its `api.py`). Its browser thus follows a redirect
    without anyone re-checking the destination, and an internal service's
    content would come back into the model's context. We check the FINAL URL
    (`redirected_url`), which its `handle_crawl_request` does serialize
    since it returns `result.model_dump()`.
    """

    def _pages(self, results):
        def fake_post(url, **kw):
            r = mock.Mock(); r.raise_for_status = lambda: None
            r.json = lambda: {'results': results}
            return r
        def resolves(h, *a, **k):
            if h == 'exemple.fr':
                return [(2, 1, 6, '', ('93.184.216.34', 0))]
            if h == 'litellm':
                return [(2, 1, 6, '', ('172.19.0.5', 0))]
            raise socket.gaierror('introuvable')

        with mock.patch('websearch.requests.post', side_effect=fake_post), \
             mock.patch('websearch.socket.getaddrinfo', side_effect=resolves):
            pages, _ = websearch.read(['https://exemple.fr/a'])
        return pages[0]

    def test_redirect_to_internal_address_refused(self):
        page = self._pages([{'url': 'https://exemple.fr/a', 'success': True,
                             'redirected_url': 'http://127.0.0.1:8001/status',
                             'markdown': {'raw_markdown': 'contenu interne ' * 30},
                             'metadata': {'title': 't'}}])
        self.assertNotIn('content', page)
        self.assertIn('interne', page.get('error', ''))

    def test_redirect_to_docker_network_service_refused(self):
        page = self._pages([{'url': 'https://exemple.fr/a', 'success': True,
                             'redirected_url': 'http://litellm:4001/health',
                             'markdown': {'raw_markdown': 'contenu interne ' * 30},
                             'metadata': {'title': 't'}}])
        self.assertNotIn('content', page)
        self.assertIn('interne', page.get('error', ''))

    def test_public_redirect_kept(self):
        page = self._pages([{'url': 'https://exemple.fr/a', 'success': True,
                             'redirected_url': 'https://exemple.fr/a/',
                             'markdown': {'raw_markdown': 'article normal ' * 30},
                             'metadata': {'title': 't'}}])
        self.assertIn('content', page)

    def test_missing_redirected_url_changes_nothing(self):
        """Previous behaviour: a crawler that says nothing is not suspicious."""
        page = self._pages([{'url': 'https://exemple.fr/a', 'success': True,
                             'markdown': {'raw_markdown': 'article normal ' * 30},
                             'metadata': {'title': 't'}}])
        self.assertIn('content', page)

    def test_not_knowing_is_not_refusing(self):
        """Name that does not resolve on our side: we do NOT discard the page."""
        self.assertFalse(websearch.internal_target('https://nom-inconnu-zz.test/x'))
        self.assertFalse(websearch.internal_target('pas une url'))
        self.assertFalse(websearch.internal_target(''))
        self.assertTrue(websearch.internal_target('http://169.254.169.254/latest/'))

    def test_unresolvable_redirect_kept(self):
        page = self._pages([{'url': 'https://exemple.fr/a', 'success': True,
                             'redirected_url': 'https://autre-nom-zz.test/a',
                             'markdown': {'raw_markdown': 'article normal ' * 30},
                             'metadata': {'title': 't'}}])
        self.assertIn('content', page)
