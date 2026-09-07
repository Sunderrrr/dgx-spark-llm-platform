"""Détecteur de base réinitialisée (db._detecte_base_reinitialisee).

Le 04/09/2026 le contenu de portal.db a été réinitialisé sans cause
identifiée et personne ne l'a vu pendant trois jours. Contrat figé ici :
une base peuplée crée le marqueur ; une base VIDE alors que le marqueur
existe alerte (email infra) ; une base vierge d'usine n'alerte jamais.
"""
import os
import sqlite3
import unittest
from unittest import mock

from db import DB_PATH, DB_RESET_MARKER, _detecte_base_reinitialisee
import app as portal   # noqa: F401  (déclenche init_db : schéma complet)


class DetecteurTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(DB_RESET_MARKER):
            os.remove(DB_RESET_MARKER)
        self.db = sqlite3.connect(DB_PATH)

    def tearDown(self):
        try:
            self.db.rollback()
            self.db.close()
        except sqlite3.ProgrammingError:
            pass  # connexion volontairement fermée par un test
        if os.path.exists(DB_RESET_MARKER):
            os.remove(DB_RESET_MARKER)

    def _peuple(self):
        self.db.execute(
            "INSERT INTO conversations (username, client_id, title, model, messages, updated_at) "
            "VALUES ('zz-det', 'c1', 't', 'm', '[]', '2026-09-07T00:00:00')")

    def test_base_vierge_sans_marqueur_n_alerte_pas(self):
        with mock.patch('notify.notify_infra_alert_email') as alerte:
            _detecte_base_reinitialisee(self.db)
        alerte.assert_not_called()
        self.assertFalse(os.path.exists(DB_RESET_MARKER))

    def test_base_peuplee_cree_le_marqueur(self):
        self._peuple()
        _detecte_base_reinitialisee(self.db)
        self.assertTrue(os.path.exists(DB_RESET_MARKER))

    def test_marqueur_present_et_base_vide_alerte(self):
        self._peuple()
        _detecte_base_reinitialisee(self.db)          # crée le marqueur
        self.db.execute("DELETE FROM conversations")
        with mock.patch('notify.notify_infra_alert_email') as alerte:
            _detecte_base_reinitialisee(self.db)
        alerte.assert_called_once()
        sujet = alerte.call_args[0][0]
        self.assertIn('reset', sujet.lower())

    def test_rien_apres_lecture_impossible(self):
        # Le détecteur ne doit JAMAIS faire échouer init_db : sur une vraie
        # connexion fermée, il avale l'erreur (pas d'exception propagée).
        self.db.close()
        _detecte_base_reinitialisee(self.db)  # ne doit pas lever


if __name__ == '__main__':
    unittest.main()
