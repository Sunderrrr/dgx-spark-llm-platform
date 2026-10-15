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


class TriggerTest(unittest.TestCase):
    """The image tool only arms on an explicit request from the user."""

    def _requested(self, text):
        return image_tools._image_requested(
            [{'role': 'user', 'content': text}])

    def test_answers_to_clarifying_questions_keep_the_request(self):
        # « fais-moi une image pour halloween » → questions → answers alone:
        # the tool was not armed and the model wrote its calls as text
        # (2026-10-13).
        q = '```ask\n{"questions": [{"question": "Style ?", "options": ["nuit"]}]}\n```'
        hist = [{'role': 'user', 'content': "hello fait moi une image pour halloween, j'en veux 3 en portrait"},
                {'role': 'assistant', 'content': q},
                {'role': 'user', 'content': "Style ?\n→ nuit"}]
        self.assertTrue(image_tools._image_requested(hist))
        # Without the questionnaire, answers alone arm nothing.
        hist[1] = {'role': 'assistant', 'content': "Voilà !"}
        self.assertFalse(image_tools._image_requested(hist))

    def test_follow_up_on_generated_images_keeps_the_tool(self):
        # « ajuste les photos » right after generated images: no image noun
        # nor verb, but the tool must stay offered — without it the model
        # invented /image/file/<id>/3…8 (2026-10-13).
        hist = [{'role': 'user', 'content': "fais-moi 3 images d'halloween"},
                {'role': 'assistant', 'content': "![image générée](/image/file/abc123/0)"},
                {'role': 'user', 'content': "vasi ajuste les photo fait des promte de meilleur qualité"}]
        self.assertTrue(image_tools._image_requested(hist))
        hist[1] = {'role': 'assistant', 'content': "Bonjour !"}
        self.assertFalse(image_tools._image_requested(hist))

    def test_explicit_directives_(self):
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
            "hello fait moi une image pour halloween",   # « fait » (sic)
            "tu peux me faire 3 visuels pour la soirée",
            "fais-moi une affiche pour la fête",
            "vasi génére les image",                       # génére + les (2026-10-13)
            "refais l'image en plus sombre",
        ):
            self.assertTrue(self._requested(text), text)

    def test_never_false_positives(self):
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
            self.assertFalse(self._requested(text), text)

    def test_pasted_code_triggers_nothing(self):
        # Same trap as for search: pasted code that TALKS about images must not
        # count as a directive (see _texte_de_la_demande).
        pasted = ("```python\n# générer une image de secours\n"
                 "def snapshot(): ...\n```\nexplique ce code")
        self.assertFalse(self._requested(pasted))


class GenerationTest(unittest.TestCase):
    """Execution of generer_image: worker, returned addresses, closed job."""

    def setUp(self):
        import app as portal
        portal.app.config['TESTING'] = True

    def _fake_sidecar(self):
        def fake_post(url, **kw):
            r = mock.Mock()
            r.ok = True
            r.headers = {'Content-Type': 'image/png'}
            r.content = b'\x89PNG pretendu'
            return r
        return fake_post

    def test_generation_succeeds(self):
        step_log = []
        with mock.patch('image_routes.requests.post',
                        side_effect=self._fake_sidecar()), \
             mock.patch.object(image_tools, '_POLL_INTERVAL_S', 0.05):
            import app as portal
            with portal.app.test_request_context():
                gen = image_tools._exec_image_tool(
                    {'prompt': 'a cat astronaut, cinematic lighting'},
                    'zz-test-img', step_log)
                heartbeats, result = [], None
                while True:
                    try:
                        chunk = next(gen)
                    except StopIteration as stop:
                        result = stop.value
                        break
                    heartbeats.append(chunk)
        self.assertIn('/image/file/', result)
        self.assertTrue(all(b.startswith(': ') for b in heartbeats), heartbeats)
        self.assertEqual(step_log[-1]['step'], 'generation_done')
        self.assertEqual(len(step_log[-1]['images']), 1)
        self.assertTrue(step_log[-1]['images'][0].startswith('/image/file/'))
        # The job is properly closed on the database side: no ghost « running ».
        c = sqlite3.connect(DB_PATH)
        line = c.execute("SELECT status, done_count FROM image_jobs WHERE prompt_id=?",
                          (step_log[-1]['prompt_id'],)).fetchone()
        c.close()
        self.assertEqual(line, ('done', 1))

    def test_empty_prompt_refused(self):
        step_log = []
        gen = image_tools._exec_image_tool({'prompt': '  '}, 'zz-test-img', step_log)
        try:
            next(gen)
        except StopIteration as stop:
            result = stop.value
        self.assertIn('impossible', result)
        self.assertEqual(step_log, [])


class ToolsPhaseTest(unittest.TestCase):
    """The tool phase arms generer_image and reinjects the addresses to the model."""

    def test_phase_with_image(self):
        import websearch_tools

        calls = {'tour': 0}

        def fake_litellm(url, **kw):
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
        def fake_post(url, **kw):
            if '/v1/chat/completions' in url:
                return fake_litellm(url, **kw)
            return self._fake_sidecar()(url, **kw)

        step_log, findings = [], []
        with mock.patch('requests.post', side_effect=fake_post), \
             mock.patch.object(image_tools, '_POLL_INTERVAL_S', 0.05):
            import app as portal
            with portal.app.test_request_context():
                msgs = [{'role': 'user', 'content': 'génère une image de chat'}]
                steps = list(websearch_tools._tools_phase(
                    'modele-test', msgs, 'cle', step_log, findings,
                    web_ok=False, img_ok=True, username='zz-test-img'))
        announcements = [e for e in steps if '"generation"' in e]
        self.assertTrue(announcements, steps)
        self.assertTrue(any('/image/file/' in content for _n, content in findings),
                        findings)
        self.assertEqual(step_log[-1]['tool'], 'diffusers')

    def _fake_sidecar(self):
        def fake_post(url, **kw):
            r = mock.Mock()
            r.ok = True
            r.headers = {'Content-Type': 'image/png'}
            r.content = b'\x89PNG pretendu'
            return r
        return fake_post


if __name__ == '__main__':
    unittest.main()


class InventedImagesTest(unittest.TestCase):
    """An answer citing generated-image addresses that serve nothing."""

    def test_only_missing_files_are_reported(self):
        exists = {('abc123', 0), ('abc123', 1)}
        with mock.patch.object(image_tools, 'image_exists',
                               side_effect=lambda user, pid, idx=0: (pid, idx) in exists):
            text = ("![a](/image/file/abc123/0) ![b](/image/file/abc123/1) "
                    "![c](/image/file/abc123/3) ![d](/image/file/abc123/3) ![e](/image/file/ffff00/0)")
            self.assertEqual(image_tools.invented_images(text, 'demo'),
                             ['/image/file/abc123/3', '/image/file/ffff00/0'])


class CancelFlagTest(unittest.TestCase):
    """The worker's cancellation probe must see a cancelled job."""

    def test_cancelled_job_is_seen(self):
        import app  # noqa: F401  (creates the schema)
        import image_routes
        c = sqlite3.connect(DB_PATH)
        c.execute("INSERT INTO image_jobs (username, prompt_id, prompt, status, count, done_count, format, created_at) "
                  "VALUES ('zz-cancel', 'c0ffee', 'p', 'cancelled', 2, 0, 'png', '2026-10-15T00:00:00')")
        c.execute("INSERT INTO image_jobs (username, prompt_id, prompt, status, count, done_count, format, created_at) "
                  "VALUES ('zz-cancel', 'beef00', 'p', 'running', 2, 0, 'png', '2026-10-15T00:00:00')")
        c.commit()
        try:
            self.assertTrue(image_routes._image_cancelled('c0ffee', 'zz-cancel'))
            self.assertFalse(image_routes._image_cancelled('beef00', 'zz-cancel'))
        finally:
            c.execute("DELETE FROM image_jobs WHERE username='zz-cancel'")
            c.commit()
            c.close()
