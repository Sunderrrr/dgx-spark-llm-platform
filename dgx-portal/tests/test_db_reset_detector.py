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

from db import DB_PATH, DB_RESET_MARKER, _detect_reset_database
import app as portal   # noqa: F401  (triggers init_db: full schema)


class DetectorTest(unittest.TestCase):
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

    def _populate(self):
        self.db.execute(
            "INSERT INTO conversations (username, client_id, title, model, messages, updated_at) "
            "VALUES ('zz-det', 'c1', 't', 'm', '[]', '2026-09-07T00:00:00')")

    def test_blank_db_without_marker_does_not_alert(self):
        with mock.patch('notify.notify_infra_alert_email') as alert:
            _detect_reset_database(self.db)
        alert.assert_not_called()
        self.assertFalse(os.path.exists(DB_RESET_MARKER))

    def test_populated_db_creates_marker(self):
        self._populate()
        _detect_reset_database(self.db)
        self.assertTrue(os.path.exists(DB_RESET_MARKER))

    def test_marker_present_and_empty_db_alerts(self):
        self._populate()
        _detect_reset_database(self.db)          # creates the marker
        self.db.execute("DELETE FROM conversations")
        with mock.patch('notify.notify_infra_alert_email') as alert:
            _detect_reset_database(self.db)
        alert.assert_called_once()
        subject_ = alert.call_args[0][0]
        self.assertIn('reset', subject_.lower())

    def test_nothing_after_unreadable_db(self):
        # The detector must NEVER fail init_db: on a really closed connection it
        # swallows the error (no exception propagated).
        self.db.close()
        _detect_reset_database(self.db)  # must not raise


if __name__ == '__main__':
    unittest.main()
