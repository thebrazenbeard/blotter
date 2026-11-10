import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
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

    def test_idempotent_retry_rejects_noncanonical_existing_body(self):
        self.note(event_id="stable-1", payload={"n": 2})
        with closing(sqlite3.connect(self.ledger.path)) as db:
            with db:
                body = db.execute("SELECT body FROM events WHERE seq=1").fetchone()[0]
                db.execute("UPDATE events SET body=? WHERE seq=1",
                           (body[:-1] + ',"seq":1}',))
        with self.assertRaises(IntegrityError):
            self.note(event_id="stable-1", payload={"n": 2})

    def test_idempotent_retry_accepts_valid_non_genesis_event(self):
        self.note(event_id="prefix")
        first = self.note(event_id="stable-2", payload={"n": 2})
        self.assertEqual(first, self.note(event_id="stable-2", payload={"n": 2}))
        self.assertEqual(self.ledger.verify()["count"], 2)

    def test_idempotent_retry_rejects_tampered_stored_hash(self):
        self.note(event_id="stable-1", payload={"n": 2})
        with closing(sqlite3.connect(self.ledger.path)) as db:
            with db:
                db.execute("UPDATE events SET hash=? WHERE seq=1", ("f" * 64,))
        with self.assertRaises(IntegrityError):
            self.note(event_id="stable-1", payload={"n": 2})

    def test_idempotent_retry_rejects_forged_canonical_field(self):
        self.note(event_id="stable-1", payload={"n": 2})
        with closing(sqlite3.connect(self.ledger.path)) as db:
            with db:
                body = db.execute("SELECT body FROM events WHERE seq=1").fetchone()[0]
                event = json.loads(body)
                event["operator_attested"] = True
                db.execute("UPDATE events SET body=? WHERE seq=1",
                           (json.dumps(event, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False),))
        with self.assertRaises(IntegrityError):
            self.note(event_id="stable-1", payload={"n": 2})

    def test_redaction_and_export(self):
        ev = self.note(payload={"password": "secret", "token": "secret-token", "nested": [{"api_key": "hidden"}]})
        self.assertEqual(ev["payload"]["password"], "[REDACTED]")
        self.assertEqual(ev["payload"]["token"], "[REDACTED]")
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

    def test_timestamp_precision_is_not_silently_truncated(self):
        for timestamp in (
            "2026-10-27T01:02:03.0000001Z",
            "2026-10-27T01:02:03.0000009+00:00",
            "2026-10-27T03:02:03,123456789+02:00",
        ):
            with self.subTest(timestamp=timestamp), self.assertRaises(BlotterError):
                self.note(occurred_at=timestamp)
        self.assertEqual(self.ledger.verify()["count"], 0)
        accepted = self.note(occurred_at="2026-10-27T03:02:03.123456+02:00")
        self.assertEqual(accepted["occurred_at"], "2026-10-27T01:02:03.123456Z")
        self.assertEqual(self.ledger.verify()["count"], 1)

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

    def test_full_verify_rejects_noncanonical_event_bytes(self):
        self.note(event_id="archive-a")
        self.note(event_id="archive-b")
        with closing(sqlite3.connect(self.ledger.path)) as db:
            with db:
                body = db.execute("SELECT body FROM events WHERE seq=1").fetchone()[0]
                # The decoded event and existing stored digest remain equal;
                # the actual stored bytes have nevertheless been rewritten.
                db.execute("UPDATE events SET body=? WHERE seq=1",
                           (body[:-1] + ',"seq":1}',))
        with self.assertRaises(IntegrityError):
            Blotter(self.ledger.path).verify()

    def test_check_rejects_modified_cursor_event_before_append(self):
        self.note(event_id="checkpoint-a")
        self.note(event_id="checkpoint-b")
        with closing(sqlite3.connect(self.ledger.path)) as db:
            with db:
                prior_hash = db.execute("SELECT hash FROM events WHERE seq=1").fetchone()[0]
                db.execute("UPDATE events SET body=? WHERE seq=1", ('{"seq":1}',))
        with self.assertRaises(IntegrityError):
            Blotter(self.ledger.path).check(actor="reader", after_seq=1,
                                            expected_after_hash=prior_hash)
        self.assertEqual(len(self.ledger.list()), 2)

    def test_check_rejects_modified_page_event_before_append(self):
        self.note(event_id="page-a")
        self.note(event_id="page-b")
        with closing(sqlite3.connect(self.ledger.path)) as db:
            with db:
                db.execute("UPDATE events SET body=? WHERE seq=2", ('{"seq":2}',))
        with self.assertRaises(IntegrityError):
            Blotter(self.ledger.path).check(actor="reader", after_seq=0)
        self.assertEqual(len(self.ledger.list()), 2)

    def test_check_rejects_noncanonical_json_without_digest_change(self):
        self.note(event_id="canonical-a")
        with closing(sqlite3.connect(self.ledger.path)) as db:
            with db:
                body = db.execute("SELECT body FROM events WHERE seq=1").fetchone()[0]
                # Duplicate seq parses to the same object, so merely recomputing
                # the parsed JSON's digest would incorrectly accept this rewrite.
                db.execute("UPDATE events SET body=? WHERE seq=1",
                           (body[:-1] + ',"seq":1}',))
        with self.assertRaises(IntegrityError):
            Blotter(self.ledger.path).check(actor="reader", after_seq=0)
        self.assertEqual(len(self.ledger.list()), 1)

    def test_check_accepts_valid_cross_reader_hash_checkpoint(self):
        self.note(event_id="stable-a")
        first = self.ledger.check(actor="reader-one", after_seq=0)
        self.note(event_id="stable-b")
        second = Blotter(self.ledger.path).check(
            actor="reader-two", after_seq=first["next_seq"],
            expected_after_hash=first["next_hash"],
        )
        self.assertIn("stable-b", [event["id"] for event in second["events"]])
        self.assertTrue(self.ledger.verify()["ok"])

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

    def test_tail_is_bounded_global_chronological_read_without_write(self):
        self.assertEqual(self.ledger.tail(limit=20), [])
        for i in range(25):
            self.note(actor="one" if i % 2 else "two", event_id=f"tail-{i}")
        self.assertEqual([e["seq"] for e in self.ledger.tail(limit=20)], list(range(6, 26)))
        self.assertEqual([e["seq"] for e in self.ledger.tail(limit=1)], [25])
        self.assertEqual(self.ledger.verify()["count"], 25)
        for invalid in (0, -1, 101, True, 1.0, "20"):
            with self.subTest(invalid=invalid), self.assertRaises(BlotterError):
                self.ledger.tail(limit=invalid)


if __name__ == "__main__":
    unittest.main()
