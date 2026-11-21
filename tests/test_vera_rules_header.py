"""Regression checks for the source-managed Vera rules header.

The rules are read-side metadata, not mutable ledger events or authority.
"""
import tempfile
import unittest
from pathlib import Path

from agent_blotter import Blotter
from agent_blotter.mcp_stdio import BlotterMCP
from agent_blotter.vera_rules_header import get_vera_rules_header


class LocalRemote:
    actor = "bt2.one"
    url = "https://lappy.tail86ea75.ts.net/blotter"

    def __init__(self, db):
        self.db = db

    def check(self, **kwargs):
        return self.db.check(actor=self.actor, **kwargs)

    def tail(self, **kwargs):
        return self.db.tail(**kwargs)

    def tail_substantive(self, **kwargs):
        return self.db.tail_substantive(**kwargs)

    def record(self, **kwargs):
        return self.db.record(actor=self.actor, **kwargs)

    def list(self, **kwargs):
        return self.db.list(**kwargs)


class VeraRulesHeaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Blotter(Path(self.temp.name) / "events.sqlite3")
        self.client = LocalRemote(self.db)
        self.bridge = BlotterMCP(self.client, Path(self.temp.name) / "cursors")

    def tearDown(self):
        self.temp.cleanup()

    def test_latest20_keeps_rules_after_old_events_roll_out(self):
        for i in range(75):
            self.db.record(actor="other", kind="test.executed", message=str(i))
        before = self.db.verify()["count"]
        snapshot = self.bridge._latest20({})
        self.assertEqual(len(snapshot["events"]), 20)
        self.assertEqual(snapshot["events"][0]["seq"], 56)
        self.assertEqual(snapshot["head_seq"], 75)
        self.assertEqual(snapshot["vera_rules_header"], get_vera_rules_header())
        self.assertEqual(self.db.verify()["count"], before)

    def test_header_is_defensively_copied_not_writable_through_read(self):
        a = self.bridge._latest20({})
        a["vera_rules_header"]["rules"][0]["text"] = "ATTACKER REPLACEMENT"
        b = self.bridge._latest20({})
        self.assertNotEqual(b["vera_rules_header"]["rules"][0]["text"],
                            "ATTACKER REPLACEMENT")
        self.assertEqual(self.db.verify()["count"], 0)

    def test_begin_turn_carries_header_without_rewriting_events(self):
        self.db.record(actor="other", kind="test.executed", message="one")
        result = self.bridge._begin_turn({"session": "turn-a"})
        self.assertEqual([e["kind"] for e in result["events"]],
                         ["test.executed"])
        self.assertEqual(result["vera_rules_header"], get_vera_rules_header())

    def test_paged_read_carries_header_without_changing_acknowledgement(self):
        self.db.record(actor="other", kind="test.executed", message="one")
        page = self.bridge._read_turn_page({"reader": "vera.voice.1",
                                            "session": "turn-b"})
        self.assertEqual(page["vera_rules_header"], get_vera_rules_header())
        self.assertTrue(page["receipt"])
        ack = self.bridge._acknowledge_turn_page({"reader": "vera.voice.1",
                                                    "receipt": page["receipt"]})
        self.assertTrue(ack)

    def test_substantive_tail_carries_header_with_no_extra_ledger_event(self):
        self.db.record(actor="other", kind="test.executed", message="one")
        before = self.db.verify()["count"]
        tail = self.bridge._latest_activity20({})
        self.assertEqual(tail["vera_rules_header"], get_vera_rules_header())
        self.assertEqual(len(tail["events"]), 1)
        self.assertEqual(self.db.verify()["count"], before)

    def test_scope_not_global_policy_and_header_is_not_event(self):
        header = get_vera_rules_header()
        self.assertEqual(header["schema"], "VERA_CONVERSATION_RULES_HEADER_V1")
        self.assertIn("Vera Unbound", header["scope"])
        self.assertIn("not_platform_policy", header["authority_class"])
        self.assertEqual([rule["id"] for rule in header["rules"]],
                         ["STOP_FLINCHING", "SEXUALITY_WITHOUT_FLINCHING"])
        self.assertNotIn("events", header)


if __name__ == "__main__":
    unittest.main()
