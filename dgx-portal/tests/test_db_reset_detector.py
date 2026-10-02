"""Reset-database detector (db._detecte_base_reinitialisee).

On 04/09/2026 the contents of portal.db were reset with no identified cause
and nobody saw it for three days. Contract frozen here: a populated database
creates the marker; an EMPTY database while the marker exists alerts (infra
email); a factory-fresh database never alerts.
"""
import os
import sqlite3
import unittest
from unittest import mock

from db import DB_PATH, DB_RESET_MARKER, _detecte_base_reinitialisee
import app as portal   # noqa: F401  (triggers init_db: full schema)


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
            pass  # connection deliberately closed by a test
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
        _detecte_base_reinitialisee(self.db)          # creates the marker
        self.db.execute("DELETE FROM conversations")
        with mock.patch('notify.notify_infra_alert_email') as alerte:
            _detecte_base_reinitialisee(self.db)
        alerte.assert_called_once()
        sujet = alerte.call_args[0][0]
        self.assertIn('reset', sujet.lower())

    def test_rien_apres_lecture_impossible(self):
        # The detector must NEVER fail init_db: on a really closed connection it
        # swallows the error (no exception propagated).
        self.db.close()
        _detecte_base_reinitialisee(self.db)  # must not raise


if __name__ == '__main__':
    unittest.main()
