import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from agent_blotter import Blotter, BlotterError, IntegrityError


class BlotterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Blotter(Path(self.temp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def note(self, actor="agent.one", **kw):
        return self.ledger.record(actor=actor, kind="tool.used",
                                  message="Observed activity", evidence="OBSERVATION", **kw)

    def test_one_lane_interleaves_actors(self):
        first = self.note(actor="one")
        second = self.note(actor="two")
        third = self.note(actor="one")
        self.assertEqual([e["actor"] for e in self.ledger.list()], ["one", "two", "one"])
        self.assertEqual([first["seq"], second["seq"], third["seq"]], [1, 2, 3])
        self.assertEqual(self.ledger.verify()["count"], 3)

    def test_turn_check_writes_in_same_lane(self):
        self.note(actor="one")
        result = self.ledger.check(actor="two", session="turn-2", after_seq=0)
        self.assertEqual([e["actor"] for e in result["events"]], ["one"])
        self.assertEqual(result["check_event"]["kind"], "turn.checked")
        self.assertEqual(result["next_seq"], 2)
        self.note(actor="one")
        other = self.ledger.check(actor="three", after_seq=0)
        self.assertEqual([e["kind"] for e in other["events"]],
                         ["tool.used", "turn.checked", "tool.used"])
        self.assertEqual([e["seq"] for e in self.ledger.list()], [1, 2, 3, 4])
        self.assertFalse(other["has_more"])

    def test_turn_check_paging(self):
        for _ in range(3):
            self.note(actor="one")
        first = self.ledger.check(actor="two", limit=2)
        self.assertEqual([e["seq"] for e in first["events"]], [1, 2])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_seq"], 2)
        second = self.ledger.check(actor="two", after_seq=first["next_seq"], limit=3)
        self.assertIn(3, [e["seq"] for e in second["events"]])
        self.assertFalse(second["has_more"])
        self.assertEqual(self.ledger.verify()["count"], 5)

    def test_no_fake_check_event(self):
        with self.assertRaises(BlotterError):
            self.ledger.record(
                actor="one", kind="turn.checked", message="fake")
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_idempotency_and_collision(self):
        first = self.note(event_id="stable-1", payload={"n": 2})
        self.assertEqual(first, self.note(event_id="stable-1", payload={"n": 2}))
        with self.assertRaises(BlotterError):
            self.note(event_id="stable-1", payload={"n": 3})
        self.assertEqual(self.ledger.verify()["count"], 1)

    def test_redaction_and_export(self):
        ev = self.note(payload={"password": "secret", "nested": [{"api_key": "hidden"}]})
        self.assertEqual(ev["payload"]["password"], "[REDACTED]")
        self.assertEqual(ev["payload"]["nested"][0]["api_key"], "[REDACTED]")
        self.assertEqual(json.loads(list(self.ledger.export())[0])["id"], ev["id"])
        self.assertEqual(list(self.ledger.export(after_seq=1)), [])

    def test_invalid_inputs(self):
        with self.assertRaises(BlotterError):
            self.note(occurred_at="2026-10-09T10:00:00")
        with self.assertRaises(BlotterError):
            self.note(payload={"nan": float("nan")})
        with self.assertRaises(BlotterError):
            self.note(source_ids=["missing"])
        with self.assertRaises(BlotterError):
            self.ledger.check(actor="a", after_seq=-1)
        with self.assertRaises(BlotterError):
            self.ledger.check(actor="a", after_seq=10000)
        with self.assertRaises(BlotterError):
            self.ledger.list(limit=0)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_modified_chain_detected(self):
        self.note()
        self.note()
        db = sqlite3.connect(self.ledger.path)
        try:
            db.execute("UPDATE events SET body=? WHERE seq=1", ('{"seq":1}',))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(IntegrityError):
            self.ledger.verify()

    def test_concurrent_writers(self):
        def write(i):
            return self.note(actor=f"agent.{i % 3}", event_id=f"w-{i}")["seq"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(40)))
        self.assertEqual(sorted(results), list(range(1, 41)))
        self.assertEqual(self.ledger.verify()["count"], 40)

    def test_pagination_and_reopen(self):
        for i in range(5):
            self.note(event_id=f"n-{i}")
        other = Blotter(self.ledger.path)
        self.assertEqual([e["seq"] for e in other.list(after_seq=2, limit=2)], [3, 4])
        self.assertEqual(other.verify()["count"], 5)


if __name__ == "__main__":
    unittest.main()
