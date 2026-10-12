"""Playground video tool: explicit request detection + execution.

Same philosophy as test_image_tools.py: the trigger rule must stay STRICT
(a request, not a mention), and the execution must return a
/video/file/<prompt_id> address usable by the model in its markdown — while
closing the job row even when the chat client has left.
"""
import sqlite3
import unittest
from contextlib import ExitStack
from unittest import mock

from db import DB_PATH

import video_tools


class TriggerTest(unittest.TestCase):
    """The video tool only arms on an explicit request from the user."""

    def _requested(self, text):
        return video_tools._video_requested(
            [{'role': 'user', 'content': text}])

    def test_explicit_directives_(self):
        for text in (
            "génère une vidéo d'un chat qui marche sur la lune",
            "genere une video de paysage",                  # without accent
            "peux-tu générer un film de plongée sous-marine ?",
            "je voudrais une animation de papillons qui volent",
            "je veux une séquence de la fusée au décollage",
            "fais un clip de danse urbaine",
            "réalise une vidéo de la tour Eiffel la nuit",
            "generate a video of a cat astronaut",
            "create a short movie about deep sea diving",
        ):
            self.assertTrue(self._requested(text), text)

    def test_never_false_positives(self):
        for text in (
            # A MENTIONED video is not a REQUESTED video.
            "regarde la vidéo que j'ai envoyée",
            "quelle est la durée de la vidéo ?",
            "la vidéo générée hier saccade",                # past participle
            "peux-tu décrire la vidéo du message précédent ?",
            # Words that belong to other jobs entirely.
            "crée une animation CSS de chargement",
            "fais une transition de fondu sur la page",
            "découpe un film plastique de protection",
            "le clip du presse-papiers ne marche plus",
        ):
            self.assertFalse(self._requested(text), text)

    def test_pasted_code_triggers_nothing(self):
        # Same trap as for search and images: pasted code that TALKS about
        # videos must not count as a directive (see _texte_de_la_demande).
        pasted = ("```js\n// générer une vidéo de secours\n"
                 "const v = makeVideo();\n```\nexplique ce code")
        self.assertFalse(self._requested(pasted))


class AvailabilityTest(unittest.TestCase):
    """Declared while the service is READY OR STARTABLE — never below."""

    def test_ready_or_startable(self):
        with mock.patch.object(video_tools, 'comfyui_is_up', return_value=True), \
             mock.patch.object(video_tools, 'sidecar_startable', return_value=False):
            self.assertTrue(video_tools.video_available())
        with mock.patch.object(video_tools, 'comfyui_is_up', return_value=False), \
             mock.patch.object(video_tools, 'sidecar_startable', return_value=True):
            self.assertTrue(video_tools.video_available())

    def test_absent_and_not_startable(self):
        with mock.patch.object(video_tools, 'comfyui_is_up', return_value=False), \
             mock.patch.object(video_tools, 'sidecar_startable', return_value=False):
            self.assertFalse(video_tools.video_available())


class GenerationTest(unittest.TestCase):
    """Execution of generer_video: ComfyUI job, returned address, closed job."""

    def setUp(self):
        import app as portal
        portal.app.config['TESTING'] = True

    def _finished_status(self, prompt_id):
        return {'status': 'done', 'video_path': f'{prompt_id}.mp4',
                'video_subfolder': '', 'video_type': 'output'}

    def _launch(self, args, step_log, **patches):
        """Runs the tool generator to its end and returns its text.

        `_cache_video_local` lives in video_routes (the job persistence calls
        it there), everything else is the tool's own sidecar interface.
        """
        base = {'comfyui_is_up': mock.Mock(return_value=True),
                '_POLL_INTERVAL_S': 0.05,
                '_cache_video_local': mock.Mock(return_value=None)}
        base.update(patches)
        with ExitStack() as stack:
            for name, value in base.items():
                target = ('video_routes._cache_video_local' if name == '_cache_video_local'
                         else 'video_tools.' + name)
                stack.enter_context(mock.patch(target, value))
            import app as portal
            with portal.app.test_request_context():
                gen = video_tools._exec_video_tool(args, 'zz-test-vid', step_log)
                result, heartbeats = None, []
                while True:
                    try:
                        heartbeats.append(next(gen))
                    except StopIteration as arret:
                        result = arret.value
                        break
        return result, heartbeats

    def test_generation_succeeds(self):
        step_log = []
        result, heartbeats = self._launch(
            {'prompt': 'a cat walking on the moon', 'duration': 5}, step_log,
            comfyui_generate=mock.Mock(return_value='pid-test-1'),
            comfyui_status=self._finished_status)
        self.assertIn('/video/file/pid-test-1', result)
        self.assertTrue(all(b.startswith(': ') for b in heartbeats), heartbeats)
        self.assertEqual(step_log[-1]['step'], 'generation_video_done')
        self.assertEqual(step_log[-1]['videos'], ['/video/file/pid-test-1'])
        self.assertIsNone(step_log[-1]['error'])
        # The job is properly closed on the database side: no ghost « running »,
        # and the generation duration was written by the worker.
        c = sqlite3.connect(DB_PATH)
        line = c.execute("SELECT status, req_duration_s, duration_ms FROM video_jobs "
                          "WHERE prompt_id=?", ('pid-test-1',)).fetchone()
        c.close()
        self.assertEqual(line[0], 'done')
        self.assertEqual(line[1], 5)
        self.assertIsNotNone(line[2])

    def test_empty_description_refused(self):
        step_log = []
        result, _ = self._launch({'prompt': '  '}, step_log,
                                   comfyui_generate=mock.Mock(),
                                   comfyui_status=self._finished_status)
        self.assertIn('impossible', result)
        self.assertEqual(step_log, [])

    def test_unavailable_service_gives_reason(self):
        # The memory guard (or a failed start) says WHY: a bare « indisponible »
        # would leave the user clicking in the dark.
        step_log = []
        result, _ = self._launch(
            {'prompt': 'une vague'}, step_log,
            sidecar_autostart=mock.Mock(
                return_value=(False, "mémoire insuffisante")),
            comfyui_generate=mock.Mock(),
            comfyui_status=self._finished_status)
        self.assertIn('indisponible', result)
        self.assertIn('mémoire insuffisante', result)
        self.assertEqual(step_log[-1]['error'], "mémoire insuffisante")

    def test_submission_refused(self):
        step_log = []
        result, _ = self._launch(
            {'prompt': 'une vague'}, step_log,
            comfyui_generate=mock.Mock(return_value=None),
            comfyui_status=self._finished_status)
        self.assertIn('a échoué', result)
        self.assertEqual(step_log[-1]['error'],
                         "ComfyUI inaccessible ou requête refusée.")

    def test_still_running_said_without_address(self):
        # A generation longer than the tool's window is NOT a failure: we say
        # it keeps running (and never hand a link that would 404).
        step_log = []
        result, _ = self._launch(
            {'prompt': 'une vague'}, step_log,
            comfyui_generate=mock.Mock(return_value='pid-lent'),
            comfyui_status=mock.Mock(return_value={'status': 'running'}),
            _video_worker=mock.Mock(),
            _VIDEO_TIMEOUT_S=0.2)
        self.assertIn('en cours de génération', result)
        self.assertNotIn('/video/file/', result)
        self.assertEqual(step_log[-1]['error'], "Génération toujours en cours.")


class ToolsPhaseTest(unittest.TestCase):
    """The tool phase arms generer_video and reinjects the address to the model."""

    def test_phase_with_video(self):
        import websearch_tools

        calls = {'tour': 0}

        def fake_litellm(url, **kw):
            calls['tour'] += 1
            r = mock.Mock()
            r.ok = True
            if calls['tour'] == 1:
                r.json = lambda: {'choices': [{'message': {
                    'content': '', 'tool_calls': [{'id': 'appel1', 'function': {
                        'name': 'generate_video',
                        'arguments': '{"prompt": "a cat on the moon"}'}}]}}]}
            else:
                r.json = lambda: {'choices': [{'message': {
                    'content': 'Voici votre vidéo.', 'tool_calls': []}}]}
            return r

        step_log, findings = [], []
        with mock.patch('requests.post', side_effect=fake_litellm), \
             mock.patch.object(video_tools, 'comfyui_is_up', return_value=True), \
             mock.patch.object(video_tools, 'comfyui_generate',
                               return_value='pid-phase'), \
             mock.patch.object(video_tools, 'comfyui_status',
                               side_effect=lambda pid: {
                                   'status': 'done', 'video_path': pid + '.mp4',
                                   'video_subfolder': '', 'video_type': 'output'}), \
             mock.patch.object(video_tools, '_POLL_INTERVAL_S', 0.05), \
             mock.patch('video_routes._cache_video_local', return_value=None):
            import app as portal
            with portal.app.test_request_context():
                msgs = [{'role': 'user', 'content': 'génère une vidéo de chat'}]
                steps = list(websearch_tools._tools_phase(
                    'modele-test', msgs, 'cle', step_log, findings,
                    web_ok=False, video_ok=True, username='zz-test-vid'))
        announcements = [e for e in steps if '"generation_video"' in e]
        self.assertTrue(announcements, steps)
        self.assertTrue(any('/video/file/' in content for _n, content in findings),
                        findings)
        self.assertEqual(step_log[-1]['tool'], 'comfyui')


if __name__ == '__main__':
    unittest.main()
