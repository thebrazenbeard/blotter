import sqlite3
import tempfile
import unittest
from pathlib import Path
from agent_blotter import Blotter
from ops.backup import backup_database

class BackupTests(unittest.TestCase):
    def test_consistent_backup_and_independent_integrity(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            db=root/"activity.sqlite3"
            saved=root/"copies"
            saved.mkdir()
            source=Blotter(db)
            first=source.record(actor="agent.one",kind="test.executed",message="present")
            copy=backup_database(db,saved)
            self.assertTrue(copy.exists())
            self.assertEqual(Blotter(copy).verify()["count"],1)
            source.record(actor="agent.two",kind="test.executed",message="later")
            self.assertEqual(Blotter(copy).verify()["count"],1)
            self.assertEqual(source.verify()["count"],2)

if __name__=="__main__":unittest.main()
