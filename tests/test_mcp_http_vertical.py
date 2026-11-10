"""Real loopback HTTP + stdio MCP contract using only disposable credentials.

This test intentionally does not use the installed central Blotter or inspect
any actor's real cursor. It distinguishes local source interoperability from
deployment and credential qualification.
"""

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_blotter import Blotter
from agent_blotter.mcp_stdio import BlotterMCP
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server


def rpc(identifier, method, params=None):
    return {"jsonrpc": "2.0", "id": identifier, "method": method,
            "params": {} if params is None else params}


def call(identifier, name, arguments):
    return rpc(identifier, "tools/call", {"name": name, "arguments": arguments})


def run_wire(adapter, requests):
    incoming = io.BytesIO(b"".join(
        (json.dumps(request) + "\n").encode("utf-8") for request in requests
    ))
    outgoing = io.BytesIO()
    adapter.serve(incoming, outgoing)
    return [json.loads(line) for line in outgoing.getvalue().splitlines()]


def ready_prefix():
    return [
        rpc(1, "initialize", {"protocolVersion": "2025-06-18",
                              "capabilities": {},
                              "clientInfo": {"name": "synthetic-client",
                                             "version": "1"}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


class MCPHTTPStdioIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Blotter(Path(self.temp.name) / "synthetic.sqlite3")
        self.token_a = "A" * 40
        self.token_b = "B" * 40
        self.server = create_server(
            self.ledger, {self.token_a: "agent.alpha",
                          self.token_b: "agent.beta"}, port=0
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = RemoteBlotter(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            actor="agent.alpha", token=self.token_a, direct_only=True
        )
        self.adapter = BlotterMCP(self.client, Path(self.temp.name) / "cursors")

    def test_real_http_authenticated_record_tail_then_cursor_check(self):
        replies = run_wire(self.adapter, ready_prefix() + [
            call(2, "record_activity", {"session": "synthetic-turn",
                                        "kind": "test.executed",
                                        "message": "source test",
                                        "event_id": "synthetic-event-1",
                                        "evidence": "OBSERVATION"}),
            call(3, "latest20", {}),
            call(4, "begin_turn", {"session": "synthetic-next"}),
        ])
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        self.assertTrue(all("error" not in reply for reply in replies))
        self.assertTrue(all(not reply["result"].get("isError", False)
                            for reply in replies))
        tail = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual([item["kind"] for item in tail["events"]],
                         ["test.executed"])
        checked = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual([item["kind"] for item in checked["events"]],
                         ["test.executed"])
        self.assertTrue(checked["cursor_hash"])
        entries = self.ledger.list(after_seq=0)
        self.assertEqual([item["kind"] for item in entries],
                         ["test.executed", "turn.checked"])
        self.assertEqual({item["actor"] for item in entries}, {"agent.alpha"})
        self.assertEqual(self.ledger.verify()["count"], 2)

    def test_actor_mismatch_is_rejected_before_any_append(self):
        mismatch = RemoteBlotter(
            self.client.url, actor="agent.beta", token=self.token_a,
            direct_only=True
        )
        adapter = BlotterMCP(mismatch, Path(self.temp.name) / "wrong-cursor")
        replies = run_wire(adapter, ready_prefix() + [
            call(2, "record_activity", {"session": "synthetic-turn",
                                        "kind": "test.executed",
                                        "message": "must not commit",
                                        "event_id": "synthetic-event-2"}),
        ])
        self.assertTrue(replies[-1]["result"]["isError"])
        self.assertNotIn(self.token_a, json.dumps(replies))
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_real_http_cursor_pages_all_unseen_global_events(self):
        for number in range(25):
            self.ledger.record(
                actor="agent.beta", kind="test.executed",
                message=f"independent peer observation {number}",
                event_id=f"synthetic-peer-{number}"
            )
        replies = run_wire(self.adapter, ready_prefix() + [
            call(2, "begin_turn", {"session": "synthetic-catchup", "limit": 3}),
            call(3, "latest20", {}),
        ])
        self.assertTrue(all(not result["result"].get("isError", False)
                            for result in replies))
        readback = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertGreater(readback["pages"], 1)
        self.assertEqual(sum(event["kind"] == "test.executed"
                             for event in readback["events"]), 25)
        self.assertEqual([event["message"] for event in readback["events"]
                          if event["kind"] == "test.executed"],
                         [f"independent peer observation {i}" for i in range(25)])
        self.assertTrue(readback["cursor_hash"])
        self.assertGreater(readback["cursor"], 25)
        self.assertEqual(len(list((Path(self.temp.name) / "cursors").glob("*.json"))), 1)
        tail = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertLessEqual(len(tail["events"]), 20)
        self.assertEqual(tail["head_seq"], self.ledger.list(after_seq=0)[-1]["seq"])

    def test_reused_event_id_is_idempotent_and_conflicting_retry_rejected(self):
        activity = {"session": "synthetic-turn", "kind": "test.executed",
                    "message": "one event", "event_id": "synthetic-event-3"}
        replies = run_wire(self.adapter, ready_prefix() + [
            call(2, "record_activity", activity),
            call(3, "record_activity", activity),
            call(4, "record_activity", {**activity, "message": "changed event"}),
        ])
        self.assertFalse(replies[1]["result"]["isError"])
        self.assertEqual(replies[1]["result"], replies[2]["result"])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertEqual(self.ledger.verify()["count"], 1)

    def test_real_http_paged_catchup_replays_lost_reply_and_keeps_readers_independent(self):
        original_ids = [f"paged-peer-{i}" for i in range(17)]
        for eid in original_ids:
            self.ledger.record(actor="agent.beta", kind="test.executed",
                               message="peer action", event_id=eid)

        def invoke(adapter, name, arguments):
            reply = run_wire(adapter, [call(8, name, arguments)])[0]
            self.assertNotIn("error", reply, reply)
            self.assertFalse(reply["result"]["isError"], reply)
            return json.loads(reply["result"]["content"][0]["text"])

        run_wire(self.adapter, ready_prefix())
        first = invoke(self.adapter, "read_turn_page", {
            "reader": "chat-one", "session": "before-interruption", "limit": 4})
        self.adapter = BlotterMCP(self.client, Path(self.temp.name) / "cursors")
        run_wire(self.adapter, ready_prefix())
        replay = invoke(self.adapter, "read_turn_page", {
            "reader": "chat-one", "session": "after-interruption", "limit": 4})
        self.assertEqual(replay, first)

        for reader in ("chat-one", "chat-two", "chat-one-child"):
            seen = []
            for _ in range(30):
                page = invoke(self.adapter, "read_turn_page", {
                    "reader": reader, "session": "resumed", "limit": 4})
                seen.extend(event["id"] for event in page["events"])
                invoke(self.adapter, "acknowledge_turn_page", {
                    "reader": reader, "receipt": page["receipt"]})
                if not page["has_more"]:
                    break
            else:
                self.fail("HTTP catch-up failed to make progress")
            self.assertEqual([eid for eid in seen if eid in original_ids], original_ids)
        self.assertTrue(self.ledger.verify()["ok"])

    def test_acknowledgment_for_another_reader_cannot_consume_pending_history(self):
        self.ledger.record(actor="agent.beta", kind="test.executed",
                           message="keep pending", event_id="pending-for-one")
        run_wire(self.adapter, ready_prefix())
        page_reply = run_wire(self.adapter, [call(2, "read_turn_page", {
            "reader": "one", "session": "turn"})])[0]
        self.assertNotIn("error", page_reply, page_reply)
        page = json.loads(page_reply["result"]["content"][0]["text"])
        rejected = run_wire(self.adapter, [call(3, "acknowledge_turn_page", {
            "reader": "two", "receipt": page["receipt"]})])[0]
        self.assertTrue(rejected["result"]["isError"])
        replay = run_wire(self.adapter, [call(4, "read_turn_page", {
            "reader": "one", "session": "next-turn"})])[0]
        self.assertEqual(page, json.loads(replay["result"]["content"][0]["text"]))

    def test_replayed_old_final_page_requires_a_fresh_current_session_check(self):
        run_wire(self.adapter, ready_prefix())

        def invoke(name, **arguments):
            reply = run_wire(self.adapter, [call(5, name, arguments)])[0]
            self.assertNotIn("error", reply, reply)
            self.assertFalse(reply["result"]["isError"], reply)
            return json.loads(reply["result"]["content"][0]["text"])

        old_page = invoke("read_turn_page", reader="chat", session="old-turn")
        self.assertFalse(old_page["has_more"])
        event = self.ledger.record(actor="agent.beta", kind="test.executed",
                                   message="arrived after the old snapshot",
                                   event_id="new-turn-peer-action")
        replay = invoke("read_turn_page", reader="chat", session="current-turn")
        self.assertEqual(replay, old_page)
        self.assertNotEqual(replay["session"], "current-turn")
        invoke("acknowledge_turn_page", reader="chat", receipt=replay["receipt"])
        fresh = invoke("read_turn_page", reader="chat", session="current-turn")
        self.assertEqual(fresh["session"], "current-turn")
        self.assertIn(event["id"], [entry["id"] for entry in fresh["events"]])
        invoke("acknowledge_turn_page", reader="chat", receipt=fresh["receipt"])


if __name__ == "__main__":
    unittest.main()
