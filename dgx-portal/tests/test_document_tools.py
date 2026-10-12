"""Playground document tool (OCR): explicit request detection + execution.

Same philosophy as test_image_tools.py: the trigger rule must stay STRICT
(a request to READ, not a mention), and the execution must return the TEXT
extracted from the attachment the playground already sends as base64.
"""
import json
import unittest
from contextlib import ExitStack
from unittest import mock

import document_tools


class DeclenchementTest(unittest.TestCase):
    """The document tool only arms on an explicit request from the user."""

    def _demandee(self, text):
        return document_tools._document_requested(
            [{'role': 'user', 'content': text}])

    def test_directives_explicites(self):
        for text in (
            "lis ce document joint",
            "peux-tu lire la pièce jointe ?",
            "transcris cette facture",
            "extrais le texte de la capture d'écran",
            "que contient ce document ?",
            "reconnais le texte de cette photo",
            "déchiffre ce manuscrit",
            "OCR cette image",                              # the tool's own name
            "read the attached document",
            "transcribe this receipt",
        ):
            self.assertTrue(self._demandee(text), text)

    def test_faux_positifs_jamais(self):
        for text in (
            # A MENTIONED document is not a document to READ.
            "le document que tu as rédigé hier",
            "la facture de janvier est dans le dépôt",
            "décris cette image",                           # describing is the model's job
            "regarde le document que j'ai envoyé",
            # Reading on the web is search's job, a source file is text.
            "lis la page web de la documentation",
            "lis le code source du module",
            "va voir le site du projet",
        ):
            self.assertFalse(self._demandee(text), text)

    def test_le_code_colle_ne_declenche_rien(self):
        # Same trap as for search and images: pasted code that TALKS about
        # documents must not count as a directive (see _texte_de_la_demande).
        colle = ("```python\n# transcris ce document papier\n"
                 "def ocr(path): ...\n```\nexplique ce code")
        self.assertFalse(self._demandee(colle))


class PiecesJointesTest(unittest.TestCase):
    """Which attachment is read: the one the model names, else the newest."""

    def test_la_plus_recente_par_defaut(self):
        self.assertEqual(document_tools._attachment(['a', 'b', 'c'], ''), ('c', ''))

    def test_le_modele_peut_nommer_la_piece(self):
        # Images travel WITHOUT their file name (data URLs), so a name can
        # only aim at a PLACE — « 2 », « image-2.jpg » and « pièce jointe 2 ».
        for name in ('2', 'image-2.jpg', 'pièce jointe 2'):
            self.assertEqual(document_tools._attachment(['a', 'b', 'c'], name)[0],
                             'b', name)

    def test_nom_hors_portee_retombe_sur_la_plus_recente(self):
        self.assertEqual(document_tools._attachment(['a', 'b'], 'facture_2026.png')[0], 'b')

    def test_aucune_piece_jointe(self):
        self.assertEqual(document_tools._attachment([], 'x'), (None, ''))


class _FauxOcr:
    """Fake OCR sidecar response (the same shape as the /ocr page's stream)."""

    def __init__(self, text, ok=True):
        self.ok = ok
        self.lines = [("data: " + json.dumps(
            {'choices': [{'delta': {'content': text}}]})).encode(),
            b'data: [DONE]']

    def iter_lines(self, decode_unicode=False):
        for line in self.lines:
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ExecutionTest(unittest.TestCase):
    """Execution of lire_document: OCR call, returned text, closed journal."""

    def setUp(self):
        import app as portal
        portal.app.config['TESTING'] = True

    PIECE = 'data:image/png;base64,' + 'aGVsbG8='   # « hello »

    def _launch(self, args, step_log, attachments, response):
        """Runs the tool generator to its end and returns its text."""
        with ExitStack() as stack:
            stack.enter_context(mock.patch(
                'ocr_routes.get_ocr_model', return_value='baidu/Unlimited-OCR'))
            stack.enter_context(mock.patch(
                'ocr_routes.requests.post', return_value=response))
            stack.enter_context(mock.patch.object(
                document_tools, 'sidecar_autostart',
                return_value=(True, '')))
            import app as portal
            with portal.app.test_request_context():
                gen = document_tools._exec_document_tool(
                    args, 'zz-test-doc', step_log, attachments)
                result, battements = None, []
                while True:
                    try:
                        battements.append(next(gen))
                    except StopIteration as arret:
                        result = arret.value
                        break
        return result, battements

    def test_extraction_reussie(self):
        step_log = []
        result, battements = self._launch(
            {'fichier': 'facture.png'}, step_log, [self.PIECE],
            _FauxOcr("Facture n° 42 — total 130 €"))
        self.assertIn("Facture n° 42", result)
        self.assertEqual(step_log[-1]['step'], 'ocr_done')
        self.assertEqual(step_log[-1]['question'], 'facture.png')
        self.assertEqual(step_log[-1]['error'], None)
        self.assertGreater(step_log[-1]['chars'], 0)

    def test_la_plus_recente_est_lue_sans_nom(self):
        step_log = []
        result, _ = self._launch(
            {}, step_log, [self.PIECE, self.PIECE], _FauxOcr("la plus récente"))
        self.assertIn("la plus récente", result)

    def test_aucune_piece_jointe_dit_le_faire(self):
        step_log = []
        result, _ = self._launch({}, step_log, [], _FauxOcr("inutile"))
        self.assertIn('Aucune image jointe', result)
        self.assertEqual(step_log, [])

    def test_extraction_vide_dit_le_motif(self):
        step_log = []
        result, _ = self._launch({}, step_log, [self.PIECE], _FauxOcr(""))
        self.assertIn('Aucun texte n\'a pu être extrait', result)
        self.assertEqual(step_log[-1]['chars'], 0)
        self.assertIsNotNone(step_log[-1]['error'])

    def test_service_indisponible_dit_le_motif(self):
        # The memory guard (or a failed start) says WHY: a bare « indisponible »
        # would leave the user clicking in the dark.
        step_log = []
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                document_tools, 'sidecar_autostart',
                return_value=(False, "mémoire insuffisante")))
            import app as portal
            with portal.app.test_request_context():
                gen = document_tools._exec_document_tool(
                    {}, 'zz-test-doc', step_log, [self.PIECE])
                try:
                    next(gen)
                except StopIteration as arret:
                    result = arret.value
        self.assertIn('indisponible', result)
        self.assertIn('mémoire insuffisante', result)
        self.assertEqual(step_log[-1]['error'], "mémoire insuffisante")


class PhaseOutilsTest(unittest.TestCase):
    """The tool phase arms lire_document and reinjects the text to the model."""

    def test_phase_avec_document(self):
        import websearch_tools

        calls = {'tour': 0}

        def faux_litellm(url, **kw):
            calls['tour'] += 1
            r = mock.Mock()
            r.ok = True
            if calls['tour'] == 1:
                r.json = lambda: {'choices': [{'message': {
                    'content': '', 'tool_calls': [{'id': 'appel1', 'function': {
                        'name': 'lire_document',
                        'arguments': '{"fichier": "facture.png"}'}}]}}]}
            else:
                r.json = lambda: {'choices': [{'message': {
                    'content': 'Voici le texte.', 'tool_calls': []}}]}
            return r

        step_log, findings = [], []
        # websearch_tools and ocr_routes share the SAME `requests` module: a
        # single patch, routed on the URL — two patches would overwrite each
        # other (the same trap as in test_image_tools).
        def faux_post(url, **kw):
            if url.startswith(websearch_tools.LITELLM_URL):
                return faux_litellm(url, **kw)
            return _FauxOcr("Total : 130 €")

        with ExitStack() as stack:
            stack.enter_context(mock.patch('requests.post', side_effect=faux_post))
            stack.enter_context(mock.patch(
                'ocr_routes.get_ocr_model', return_value='baidu/Unlimited-OCR'))
            stack.enter_context(mock.patch.object(
                document_tools, 'sidecar_autostart', return_value=(True, '')))
            import app as portal
            with portal.app.test_request_context():
                msgs = [{'role': 'user', 'content': 'lis ce document'}]
                etapes = list(websearch_tools._tools_phase(
                    'modele-test', msgs, 'cle', step_log, findings,
                    web_ok=False, doc_ok=True, username='zz-test-doc',
                    attachments=[ExecutionTest.PIECE]))
        annonces = [e for e in etapes if '"ocr"' in e]
        self.assertTrue(annonces, etapes)
        self.assertTrue(any('Total : 130 €' in content for _n, content in findings),
                        findings)
        self.assertEqual(step_log[-1]['tool'], 'unlimited-ocr')


if __name__ == '__main__':
    unittest.main()
