"""A host can deliver a large backlog without acknowledging an unseen page."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_blotter import Blotter
from agent_blotter.mcp_stdio import BlotterMCP


class LocalClient:
    actor = "codex.host"
    url = "https://lappy.tail86ea75.ts.net/blotter"

    def __init__(self, ledger):
        self.ledger = ledger

    def check(self, **kwargs):
        return self.ledger.check(actor=self.actor, **kwargs)


class MCPPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Blotter(self.root / "shared.sqlite3")
        self.bridge = self.new_bridge()

    def new_bridge(self):
        bridge = BlotterMCP(LocalClient(self.db), self.root / "cursors")
        bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}}})
        bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return bridge

    def call(self, name, **arguments):
        reply = self.bridge.handle({"jsonrpc": "2.0", "id": 2,
                                    "method": "tools/call", "params": {
                                        "name": name, "arguments": arguments}})
        self.assertNotIn("error", reply, reply)
        self.assertFalse(reply["result"]["isError"], reply)
        return json.loads(reply["result"]["content"][0]["text"])

    def test_large_backlog_can_be_delivered_and_acknowledged_in_bounded_pages(self):
        original = [self.db.record(actor="peer", kind="test.executed", message=f"event {i}")
                    for i in range(12)]
        with mock.patch("agent_blotter.mcp_stdio.MAX_BEGIN_PAGES", 2):
            rejected = self.bridge.handle({"jsonrpc": "2.0", "id": 2,
                "method": "tools/call", "params": {"name": "begin_turn", "arguments": {
                    "session": "old-client", "limit": 2}}})
        self.assertTrue(rejected["result"]["isError"])
        seen = []
        for _ in range(40):
            page = self.call("read_turn_page", reader="chat-one", session="turn-one", limit=2)
            self.assertLessEqual(len(page["events"]), 2)
            seen.extend(event["id"] for event in page["events"])
            self.call("acknowledge_turn_page", reader="chat-one", receipt=page["receipt"])
            if not page["has_more"]:
                break
        else:
            self.fail("bounded catch-up did not exhaust the fixed backlog")
        self.assertEqual([event["id"] for event in original],
                         [eid for eid in seen if eid in {event["id"] for event in original}])

    def test_lost_page_reply_replays_after_process_restart_and_new_turn(self):
        event = self.db.record(actor="peer", kind="test.executed", message="must be delivered")
        first = self.call("read_turn_page", reader="chat-one", session="turn-one")
        self.bridge = self.new_bridge()
        replay = self.call("read_turn_page", reader="chat-one", session="turn-two")
        self.assertEqual(replay, first)
        self.assertIn(event["id"], [e["id"] for e in replay["events"]])
        self.call("acknowledge_turn_page", reader="chat-one", receipt=replay["receipt"])
        next_page = self.call("read_turn_page", reader="chat-one", session="turn-two")
        self.assertNotIn(event["id"], [e["id"] for e in next_page["events"]])

    def test_concurrent_chats_and_children_do_not_consume_each_others_history(self):
        event = self.db.record(actor="peer", kind="test.executed", message="for every reader")
        for reader in ("chat-one", "chat-two", "chat-one-child"):
            page = self.call("read_turn_page", reader=reader, session="turn-one")
            self.assertIn(event["id"], [e["id"] for e in page["events"]])
            self.call("acknowledge_turn_page", reader=reader, receipt=page["receipt"])

    def test_shared_credential_can_acknowledge_another_chats_unseen_page(self):
        """Characterize a provenance limitation; not a security guarantee.

        Caller-chosen reader names isolate cursor files, but do not authenticate
        which chat actually consumed a page under one shared connector actor.
        """
        event = self.db.record(actor="peer", kind="test.executed",
                               message="the intended chat has not read this")
        other_chat = self.new_bridge()  # same actor credential, independent MCP client

        def other_call(name, **arguments):
            reply = other_chat.handle({"jsonrpc": "2.0", "id": 3,
                "method": "tools/call", "params": {"name": name,
                "arguments": arguments}})
            self.assertNotIn("error", reply, reply)
            self.assertFalse(reply["result"]["isError"], reply)
            return json.loads(reply["result"]["content"][0]["text"])

        stolen = other_call("read_turn_page", reader="chat-owner",
                            session="other-chat-session")
        self.assertIn(event["id"], [e["id"] for e in stolen["events"]])
        other_call("acknowledge_turn_page", reader="chat-owner",
                   receipt=stolen["receipt"])

        owner_page = self.call("read_turn_page", reader="chat-owner",
                               session="owner-session")
        self.assertNotIn(event["id"], [e["id"] for e in owner_page["events"]])
        self.assertEqual(stolen["actor"], "codex.host")
        self.assertNotIn("reader", stolen["check_event"])

    def test_page_tools_refuse_missing_or_unsafe_reader_and_untrusted_destinations(self):
        for arguments in ({"session": "turn"}, {"session": "turn", "reader": "../other"},
                          {"session": "turn", "reader": "valid", "url": "https://other.invalid"}):
            reply = self.bridge.handle({"jsonrpc": "2.0", "id": 3,
                "method": "tools/call", "params": {"name": "read_turn_page", "arguments": arguments}})
            self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(self.db.verify()["count"], 0)


if __name__ == "__main__":
    unittest.main()
