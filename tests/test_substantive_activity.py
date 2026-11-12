"""Regression tests for additive non-check activity view on the main log."""
import tempfile
import unittest
from pathlib import Path

from agent_blotter import Blotter, BlotterError


class SubstantiveActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Blotter(Path(self.temp.name) / "main.sqlite3")

    def test_substantive_tail_skips_check_noise_without_mutation(self):
        first = self.db.record(actor="vera", kind="test.executed",
                               message="actual work one")
        for i in range(45):
            self.db.check(actor="reader", session=f"turn-{i}", after_seq=0, limit=1)
        second = self.db.record(actor="aeonith", kind="source.reviewed",
                                message="actual work two")
        for i in range(35):
            self.db.check(actor="reader", session=f"later-{i}", after_seq=0, limit=1)
        before = self.db.verify()
        self.assertTrue(all(e["kind"] == "turn.checked" for e in self.db.tail(limit=20)))
        actual = self.db.tail_substantive(limit=20)
        self.assertEqual([e["seq"] for e in actual], [first["seq"], second["seq"]])
        self.assertEqual([e["message"] for e in actual],
                         ["actual work one", "actual work two"])
        self.assertEqual(self.db.verify(), before)

    def test_substantive_tail_is_bounded_and_keeps_order(self):
        for i in range(30):
            self.db.record(actor="agent", kind="action", message=str(i))
            self.db.check(actor="reader", session=f"turn-{i}", after_seq=0, limit=1)
        self.assertEqual([e["message"] for e in self.db.tail_substantive(limit=5)],
                         [str(i) for i in range(25, 30)])
        for invalid in (0, -1, 101, True, 1.0, "5"):
            with self.subTest(invalid=invalid), self.assertRaises(BlotterError):
                self.db.tail_substantive(limit=invalid)
        self.assertEqual(self.db.verify()["count"], 60)


if __name__ == "__main__":
    unittest.main()
