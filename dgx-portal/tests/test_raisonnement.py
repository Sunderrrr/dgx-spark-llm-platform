# -*- coding: utf-8 -*-
"""`raisonnement.py` — faut-il faire réfléchir le modèle ?

« Active le en fonction de ce que les user demandent, pas tout le temps »
(2026-10-03). Le raisonnement coûte des tokens et de la latence : ces tests
verrouillent les DEUX directions — les demandes qui le méritent le déclenchent,
le bavardage courant ne le déclenche PAS (plutôt sous-déclencher, assumé).
"""
import unittest

from raisonnement import raisonnablement_complexe


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
        for texte in pour_oui:
            self.assertTrue(raisonnablement_complexe(texte), f"aurait dû réfléchir : {texte!r}")

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
        for texte in pour_non:
            self.assertFalse(raisonnablement_complexe(texte), f"n'aurait pas dû réfléchir : {texte!r}")

    def test_un_message_long_ou_multi_questions_declenche(self):
        self.assertTrue(raisonnablement_complexe("Raconte-moi " + "l'histoire de la machine. " * 30))
        self.assertTrue(raisonnablement_complexe("C'est où ? Et depuis quand ?"))

    def test_le_texte_des_pieces_jointes_est_ignore(self):
        """Ce qui compte, c'est la DEMANDE — pas le fichier joint."""
        self.assertTrue(raisonnablement_complexe("Analyse ce fichier"))
        self.assertFalse(raisonnablement_complexe("Voilà le fichier demandé."))


if __name__ == '__main__':
    unittest.main()
