"""`log_audit` archives what it evicts (2026-10-02).

The live `audit_log` table is bounded to its last 5000 rows — but the eviction
used to be a plain DELETE, so the entries one asks for months later (« who
raised whose quota ») were gone. The trace of admin actions now lands in
`audit_log_archive` (same schema), which rides along the SQLite backups.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class ArchivageAuditTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, 'test.db')
        c = sqlite3.connect(self.db_path)
        c.execute("""CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT, action TEXT, detail TEXT, created_at TEXT)""")
        c.commit()
        c.close()

    def _compte(self, sql):
        c = sqlite3.connect(self.db_path)
        try:
            return c.execute(sql).fetchone()[0]
        finally:
            c.close()

    def test_les_lignes_evacuees_sont_archivees_pas_perdues(self):
        import db
        with patch('db.DB_PATH', self.db_path):
            for i in range(5010):
                db.log_audit('demo', 'test.action', f'ligne {i}')
        self.assertEqual(self._compte("SELECT COUNT(*) FROM audit_log"), 5000)
        self.assertEqual(self._compte("SELECT COUNT(*) FROM audit_log_archive"), 10)
        # Nothing is lost: the 5010 identifiers are spread over the two tables.
        self.assertEqual(self._compte(
            "SELECT COUNT(*) FROM (SELECT id FROM audit_log "
            "UNION SELECT id FROM audit_log_archive)"), 5010)

    def test_l_archive_conserve_le_detail_des_vieilles_lignes(self):
        import db
        with patch('db.DB_PATH', self.db_path):
            for i in range(5003):
                db.log_audit('demo', 'test.action', f'ligne {i}')
        c = sqlite3.connect(self.db_path)
        try:
            lignes = c.execute("SELECT detail FROM audit_log_archive ORDER BY id").fetchall()
        finally:
            c.close()
        self.assertEqual([l[0] for l in lignes], ['ligne 0', 'ligne 1', 'ligne 2'])

    def test_une_ecriture_legere_ne_declenche_rien_d_autre(self):
        import db
        with patch('db.DB_PATH', self.db_path):
            db.log_audit('demo', 'test.action', 'une seule ligne')
        self.assertEqual(self._compte("SELECT COUNT(*) FROM audit_log"), 1)
        self.assertEqual(self._compte("SELECT COUNT(*) FROM audit_log_archive"), 0)


if __name__ == '__main__':
    unittest.main()
