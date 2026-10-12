"""Playground image tool: explicit request detection + execution.

Same philosophy as test_websearch.py: the trigger rule must stay STRICT
(a request, not a mention), and the execution must return
/image/file/<prompt_id> addresses usable by the model in its markdown.
"""
import sqlite3
import unittest
from unittest import mock

from db import DB_PATH

import image_tools


class DeclenchementTest(unittest.TestCase):
    """The image tool only arms on an explicit request from the user."""

    def _demandee(self, text):
        return image_tools._image_requested(
            [{'role': 'user', 'content': text}])

    def test_directives_explicites(self):
        for text in (
            "génère une image d'un chat astronaute",
            "genere une image de chat",            # without accent
            "peux-tu générer une illustration du DGX ?",
            "je voudrais une image de la tour Eiffel",
            "je veux des images de paysages montagnards",
            "dessine-moi un mouton",
            "dessine un plan de la fusée",
            "crée un logo pour ma boulangerie",
            "créer une icône de calendrier",
            "fais-moi un dessin de robot",
            "generate an image of a cat",
            "create a logo for my bakery",
            "draw me a dragon",
        ):
            self.assertTrue(self._demandee(text), text)

    def test_faux_positifs_jamais(self):
        for text in (
            # A MENTIONED image is not a REQUESTED image.
            "décris cette image",
            "regarde l'image que j'ai envoyée",
            "quelle est la résolution de l'image ?",
            "l'image générée hier était floue",     # past participle
            "l'image generee hier etait floue",     # ditto, without accent
            "peux-tu décrire l'image du message précédent ?",
            # System vocabulary: those « images » are not painted.
            "crée une image docker pour ce service",
            "construire l'image OCI puis la pousser",
            "créer une image disque de secours",
            "il faut une image système de récupération",
        ):
            self.assertFalse(self._demandee(text), text)

    def test_le_code_colle_ne_declenche_rien(self):
        # Same trap as for search: pasted code that TALKS about images must not
        # count as a directive (see _texte_de_la_demande).
        colle = ("```python\n# générer une image de secours\n"
                 "def snapshot(): ...\n```\nexplique ce code")
        self.assertFalse(self._demandee(colle))


class GenerationTest(unittest.TestCase):
    """Execution of generer_image: worker, returned addresses, closed job."""

    def setUp(self):
        import app as portal
        portal.app.config['TESTING'] = True

    def _faux_sidecar(self):
        def faux_post(url, **kw):
            r = mock.Mock()
            r.ok = True
            r.headers = {'Content-Type': 'image/png'}
            r.content = b'\x89PNG pretendu'
            return r
        return faux_post

    def test_generation_reussie(self):
        step_log = []
        with mock.patch('image_routes.requests.post',
                        side_effect=self._faux_sidecar()), \
             mock.patch.object(image_tools, '_POLL_INTERVAL_S', 0.05):
            import app as portal
            with portal.app.test_request_context():
                gen = image_tools._exec_image_tool(
                    {'prompt': 'a cat astronaut, cinematic lighting'},
                    'zz-test-img', step_log)
                battements, result = [], None
                while True:
                    try:
                        chunk = next(gen)
                    except StopIteration as arret:
                        result = arret.value
                        break
                    battements.append(chunk)
        self.assertIn('/image/file/', result)
        self.assertTrue(all(b.startswith(': ') for b in battements), battements)
        self.assertEqual(step_log[-1]['step'], 'generation_done')
        self.assertEqual(len(step_log[-1]['images']), 1)
        self.assertTrue(step_log[-1]['images'][0].startswith('/image/file/'))
        # The job is properly closed on the database side: no ghost « running ».
        c = sqlite3.connect(DB_PATH)
        line = c.execute("SELECT status, done_count FROM image_jobs WHERE prompt_id=?",
                          (step_log[-1]['prompt_id'],)).fetchone()
        c.close()
        self.assertEqual(line, ('done', 1))

    def test_prompt_vide_refuse(self):
        step_log = []
        gen = image_tools._exec_image_tool({'prompt': '  '}, 'zz-test-img', step_log)
        try:
            next(gen)
        except StopIteration as arret:
            result = arret.value
        self.assertIn('impossible', result)
        self.assertEqual(step_log, [])


class PhaseOutilsTest(unittest.TestCase):
    """The tool phase arms generer_image and reinjects the addresses to the model."""

    def test_phase_avec_image(self):
        import websearch_tools

        calls = {'tour': 0}

        def faux_litellm(url, **kw):
            calls['tour'] += 1
            r = mock.Mock()
            r.ok = True
            if calls['tour'] == 1:
                r.json = lambda: {'choices': [{'message': {
                    'content': '', 'tool_calls': [{'id': 'appel1', 'function': {
                        'name': 'generate_image',
                        'arguments': '{"prompt": "a cat astronaut"}'}}]}}]}
            else:
                r.json = lambda: {'choices': [{'message': {
                    'content': 'Voici votre image.', 'tool_calls': []}}]}
            return r

        # websearch_tools and image_routes share the SAME `requests` module:
        # a single patch, routed on the URL — two patches would overwrite each
        # other (observed: the LiteLLM call fell on the fake sidecar).
        def faux_post(url, **kw):
            if '/v1/chat/completions' in url:
                return faux_litellm(url, **kw)
            return self._faux_sidecar()(url, **kw)

        step_log, findings = [], []
        with mock.patch('requests.post', side_effect=faux_post), \
             mock.patch.object(image_tools, '_POLL_INTERVAL_S', 0.05):
            import app as portal
            with portal.app.test_request_context():
                msgs = [{'role': 'user', 'content': 'génère une image de chat'}]
                etapes = list(websearch_tools._tools_phase(
                    'modele-test', msgs, 'cle', step_log, findings,
                    web_ok=False, img_ok=True, username='zz-test-img'))
        annonces = [e for e in etapes if '"generation"' in e]
        self.assertTrue(annonces, etapes)
        self.assertTrue(any('/image/file/' in content for _n, content in findings),
                        findings)
        self.assertEqual(step_log[-1]['tool'], 'diffusers')

    def _faux_sidecar(self):
        def faux_post(url, **kw):
            r = mock.Mock()
            r.ok = True
            r.headers = {'Content-Type': 'image/png'}
            r.content = b'\x89PNG pretendu'
            return r
        return faux_post


if __name__ == '__main__':
    unittest.main()
