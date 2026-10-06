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

    def _demandee(self, texte):
        return document_tools._document_demandee(
            [{'role': 'user', 'content': texte}])

    def test_directives_explicites(self):
        for texte in (
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
            self.assertTrue(self._demandee(texte), texte)

    def test_faux_positifs_jamais(self):
        for texte in (
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
            self.assertFalse(self._demandee(texte), texte)

    def test_le_code_colle_ne_declenche_rien(self):
        # Same trap as for search and images: pasted code that TALKS about
        # documents must not count as a directive (see _texte_de_la_demande).
        colle = ("```python\n# transcris ce document papier\n"
                 "def ocr(path): ...\n```\nexplique ce code")
        self.assertFalse(self._demandee(colle))


class PiecesJointesTest(unittest.TestCase):
    """Which attachment is read: the one the model names, else the newest."""

    def test_la_plus_recente_par_defaut(self):
        self.assertEqual(document_tools._piece_jointe(['a', 'b', 'c'], ''), ('c', ''))

    def test_le_modele_peut_nommer_la_piece(self):
        # Images travel WITHOUT their file name (data URLs), so a name can
        # only aim at a PLACE — « 2 », « image-2.jpg » and « pièce jointe 2 ».
        for nom in ('2', 'image-2.jpg', 'pièce jointe 2'):
            self.assertEqual(document_tools._piece_jointe(['a', 'b', 'c'], nom)[0],
                             'b', nom)

    def test_nom_hors_portee_retombe_sur_la_plus_recente(self):
        self.assertEqual(document_tools._piece_jointe(['a', 'b'], 'facture_2026.png')[0], 'b')

    def test_aucune_piece_jointe(self):
        self.assertEqual(document_tools._piece_jointe([], 'x'), (None, ''))


class _FauxOcr:
    """Fake OCR sidecar response (the same shape as the /ocr page's stream)."""

    def __init__(self, texte, ok=True):
        self.ok = ok
        self.lignes = [("data: " + json.dumps(
            {'choices': [{'delta': {'content': texte}}]})).encode(),
            b'data: [DONE]']

    def iter_lines(self, decode_unicode=False):
        for ligne in self.lignes:
            yield ligne

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

    def _lancer(self, args, journal, pieces, reponse):
        """Runs the tool generator to its end and returns its text."""
        with ExitStack() as stack:
            stack.enter_context(mock.patch(
                'ocr_routes.get_ocr_model', return_value='baidu/Unlimited-OCR'))
            stack.enter_context(mock.patch(
                'ocr_routes.requests.post', return_value=reponse))
            stack.enter_context(mock.patch.object(
                document_tools, 'sidecar_demarrage_auto',
                return_value=(True, '')))
            import app as portal
            with portal.app.test_request_context():
                gen = document_tools._exec_document_tool(
                    args, 'zz-test-doc', journal, pieces)
                resultat, battements = None, []
                while True:
                    try:
                        battements.append(next(gen))
                    except StopIteration as arret:
                        resultat = arret.value
                        break
        return resultat, battements

    def test_extraction_reussie(self):
        journal = []
        resultat, battements = self._lancer(
            {'fichier': 'facture.png'}, journal, [self.PIECE],
            _FauxOcr("Facture n° 42 — total 130 €"))
        self.assertIn("Facture n° 42", resultat)
        self.assertEqual(journal[-1]['etape'], 'ocr_finie')
        self.assertEqual(journal[-1]['question'], 'facture.png')
        self.assertEqual(journal[-1]['erreur'], None)
        self.assertGreater(journal[-1]['caracteres'], 0)

    def test_la_plus_recente_est_lue_sans_nom(self):
        journal = []
        resultat, _ = self._lancer(
            {}, journal, [self.PIECE, self.PIECE], _FauxOcr("la plus récente"))
        self.assertIn("la plus récente", resultat)

    def test_aucune_piece_jointe_dit_le_faire(self):
        journal = []
        resultat, _ = self._lancer({}, journal, [], _FauxOcr("inutile"))
        self.assertIn('Aucune image jointe', resultat)
        self.assertEqual(journal, [])

    def test_extraction_vide_dit_le_motif(self):
        journal = []
        resultat, _ = self._lancer({}, journal, [self.PIECE], _FauxOcr(""))
        self.assertIn('Aucun texte n\'a pu être extrait', resultat)
        self.assertEqual(journal[-1]['caracteres'], 0)
        self.assertIsNotNone(journal[-1]['erreur'])

    def test_service_indisponible_dit_le_motif(self):
        # The memory guard (or a failed start) says WHY: a bare « indisponible »
        # would leave the user clicking in the dark.
        journal = []
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                document_tools, 'sidecar_demarrage_auto',
                return_value=(False, "mémoire insuffisante")))
            import app as portal
            with portal.app.test_request_context():
                gen = document_tools._exec_document_tool(
                    {}, 'zz-test-doc', journal, [self.PIECE])
                try:
                    next(gen)
                except StopIteration as arret:
                    resultat = arret.value
        self.assertIn('indisponible', resultat)
        self.assertIn('mémoire insuffisante', resultat)
        self.assertEqual(journal[-1]['erreur'], "mémoire insuffisante")


class PhaseOutilsTest(unittest.TestCase):
    """The tool phase arms lire_document and reinjects the text to the model."""

    def test_phase_avec_document(self):
        import websearch_tools

        appels = {'tour': 0}

        def faux_litellm(url, **kw):
            appels['tour'] += 1
            r = mock.Mock()
            r.ok = True
            if appels['tour'] == 1:
                r.json = lambda: {'choices': [{'message': {
                    'content': '', 'tool_calls': [{'id': 'appel1', 'function': {
                        'name': 'lire_document',
                        'arguments': '{"fichier": "facture.png"}'}}]}}]}
            else:
                r.json = lambda: {'choices': [{'message': {
                    'content': 'Voici le texte.', 'tool_calls': []}}]}
            return r

        journal, trouvailles = [], []
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
                document_tools, 'sidecar_demarrage_auto', return_value=(True, '')))
            import app as portal
            with portal.app.test_request_context():
                msgs = [{'role': 'user', 'content': 'lis ce document'}]
                etapes = list(websearch_tools._phase_outils(
                    'modele-test', msgs, 'cle', journal, trouvailles,
                    web_ok=False, doc_ok=True, username='zz-test-doc',
                    pieces=[ExecutionTest.PIECE]))
        annonces = [e for e in etapes if '"ocr"' in e]
        self.assertTrue(annonces, etapes)
        self.assertTrue(any('Total : 130 €' in contenu for _n, contenu in trouvailles),
                        trouvailles)
        self.assertEqual(journal[-1]['outil'], 'unlimited-ocr')


if __name__ == '__main__':
    unittest.main()
