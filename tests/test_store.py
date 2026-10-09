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
        self.db = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = Blotter(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def note(self, **kw):
        return self.ledger.record(actor="agent.one", kind="test.observation",
                                  message="saw result", evidence="OBSERVATION", **kw)

    def test_empty_and_append(self):
        self.assertEqual(self.ledger.verify()["count"], 0)
        event = self.note(session="build-1", payload={"count": 1})
        self.assertEqual(event["seq"], 1)
        self.assertEqual(self.ledger.list(actor="agent.one")[0]["id"], event["id"])
        self.assertEqual(self.ledger.verify()["count"], 1)
        self.assertTrue(event["occurred_at"].endswith("Z"))

    def test_promotion_and_local_sources(self):
        source = self.note()
        with self.assertRaises(BlotterError):
            self.ledger.promote(actor="reviewer", message="claim", source_ids=["absent"])
        promoted = self.ledger.promote(actor="reviewer", message="reviewed fix",
                                      source_ids=[source["id"]])
        self.assertEqual(promoted["evidence"], "REVIEW")
        self.assertEqual(self.ledger.knowledge()[0]["source_ids"], [source["id"]])
        with self.assertRaises(BlotterError):
            self.ledger.record(actor="reviewer", kind="knowledge.promoted", message="orphan")

    def test_idempotency(self):
        first = self.note(event_id="stable-1", payload={"part": 2})
        self.assertEqual(first, self.note(event_id="stable-1", payload={"part": 2}))
        with self.assertRaises(BlotterError):
            self.note(event_id="stable-1", payload={"part": 3})
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
            self.ledger.list(limit=0)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_modified_chain_detected(self):
        self.note()
        self.note()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE events SET body=? WHERE seq=1", ('{"seq":1}',))
        with self.assertRaises(IntegrityError):
            self.ledger.verify()

    def test_concurrent_writers(self):
        def write(i):
            return self.note(event_id=f"worker-{i}", payload={"n": i})["seq"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(30)))
        self.assertEqual(sorted(results), list(range(1, 31)))
        self.assertEqual(self.ledger.verify()["count"], 30)

    def test_pagination_and_no_automatic_promotion(self):
        for i in range(5):
            self.note(event_id=f"n-{i}")
        other = Blotter(self.db)
        self.assertEqual([e["seq"] for e in other.list(after_seq=2, limit=2)], [3, 4])
        self.assertEqual(other.knowledge(), [])

if __name__ == "__main__":
    unittest.main()
