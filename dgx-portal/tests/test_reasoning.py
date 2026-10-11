# -*- coding: utf-8 -*-
"""`raisonnement.py` — should the model be made to think about it?

« Active le en fonction de ce que les user demandent, pas tout le temps »
(2026-10-03). Reasoning costs tokens and latency: these tests lock in BOTH
directions — the requests that deserve it trigger it, everyday chit-chat
does NOT trigger it (rather under-trigger, deliberate).
"""
import unittest

from reasoning import reasonably_complex


class RaisonnementAutoTest(unittest.TestCase):

    def test_les_demandes_qui_le_meritent(self):
        pour_oui = [
            "Pourquoi le service redémarre-t-il tout seul ?",
            "Explique-moi la différence entre ces deux architectures.",
            "Voici mon code : ```python\ndef f():\n    return 1\n``` que corriger ?",
            "Calcule 1250 * 3.6 - 42 / 7",
            "Compare les avantages et inconvénients des deux options.",
            "Mon traceback : ImportError ... comment déboguer ?",
            "Aide-moi à construire un plan de migration de la base.",
            "Réfléchis étape par étape avant de répondre.",
            "C'est quoi la meilleure stratégie de sauvegarde ?",
            "Analyse cette requête SQL et optimise-la.",
            "Peux-tu expliquer ce chiffrement ?" ,
        ]
        for text in pour_oui:
            self.assertTrue(reasonably_complex(text), f"aurait dû réfléchir : {text!r}")

    def test_le_quotidien_ne_declenche_pas(self):
        pour_non = [
            "",
            "   ",
            "Salut",
            "Bonjour !",
            "Merci beaucoup",
            "ok",
            "Peux-tu reformuler ?",
            "Continue",
            "Traduis : The cat sleeps.",
            "Écris une phrase sur la pluie.",
        ]
        for text in pour_non:
            self.assertFalse(reasonably_complex(text), f"n'aurait pas dû réfléchir : {text!r}")

    def test_un_message_long_ou_multi_questions_declenche(self):
        self.assertTrue(reasonably_complex("Raconte-moi " + "l'histoire de la machine. " * 30))
        self.assertTrue(reasonably_complex("C'est où ? Et depuis quand ?"))

    def test_le_texte_des_pieces_jointes_est_ignore(self):
        """What counts is the REQUEST — not the attached file."""
        self.assertTrue(reasonably_complex("Analyse ce fichier"))
        self.assertFalse(reasonably_complex("Voilà le fichier demandé."))


if __name__ == '__main__':
    unittest.main()
