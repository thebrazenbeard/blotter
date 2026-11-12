"""MCP 2026 self-contained requests preserve classic and ledger boundaries.

Wire shapes follow openai/tunnel-client v0.0.20's Go SDK v1.7.0 dependency,
including that SDK's server/discover conformance fixture.
"""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_blotter import Blotter
from agent_blotter.mcp_stdio import BlotterMCP


VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"


def modern_meta():
    return {VERSION_KEY: "2026-07-28", CAPABILITIES_KEY: {},
            CLIENT_INFO_KEY: {"name": "synthetic-modern-client", "version": "1"}}


def rpc(method, params=None, *, modern=False, identifier=1):
    values = {} if params is None else copy.deepcopy(params)
    if modern:
        values["_meta"] = modern_meta()
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": values}


class LocalClient:
    actor = "synthetic.actor"
    url = "https://synthetic-ledger.invalid/blotter"

    def __init__(self, ledger):
        self.ledger = ledger

    def check(self, **kwargs):
        return self.ledger.check(actor=self.actor, **kwargs)

    def tail(self, *, limit):
        return self.ledger.tail(limit=limit)

    def record(self, **kwargs):
        return self.ledger.record(actor=self.actor, **kwargs)


class ModernMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Blotter(Path(self.temp.name) / "synthetic.sqlite3")
        self.client = LocalClient(self.ledger)
        self.adapter = BlotterMCP(self.client, Path(self.temp.name) / "cursors")

    def initialize_classic(self):
        reply = self.adapter.handle(rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "synthetic-classic", "version": "1"}}))
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.adapter.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def test_modern_discovery_uses_primary_protocol_shape_without_legacy_handshake(self):
        reply = self.adapter.handle(rpc("server/discover", modern=True))
        self.assertNotIn("error", reply)
        result = reply["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(result["_meta"][SERVER_INFO_KEY]["name"], "agent-blotter-stdio")
        self.assertEqual(result["ttlMs"], 0)
        self.assertEqual(result["cacheScope"], "private")
        for phrase in ("read_turn_page", "acknowledge_turn_page", "has_more", "session",
                       "reader", "untrusted"):
            self.assertIn(phrase, result["instructions"])
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_modern_tools_list_does_not_enable_later_unmarked_requests(self):
        reply = self.adapter.handle(rpc("tools/list", modern=True))
        self.assertIn("tools", reply.get("result", {}), reply)
        self.assertEqual(reply["result"]["resultType"], "complete")
        denied = self.adapter.handle(rpc("tools/list"))
        self.assertEqual(denied["error"]["code"], -32002)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_modern_record_read_and_ack_use_fixed_actor_and_strip_transport_metadata(self):
        args = {"session": "turn-1", "kind": "test.executed", "message": "actual fixture action",
                "event_id": "modern-fixture-action"}
        request = rpc("tools/call", {"name": "record_activity", "arguments": args}, modern=True)
        request["params"]["_meta"].update({"url": "https://untrusted.invalid",
                                          "actor": "different.actor", "token": "not-a-credential"})
        with mock.patch.object(self.client, "record", wraps=self.client.record) as record:
            reply = self.adapter.handle(request)
        self.assertFalse(reply.get("result", {}).get("isError", True), reply)
        self.assertNotIn("_meta", record.call_args.kwargs)
        self.assertNotIn("url", record.call_args.kwargs)
        self.assertNotIn("actor", record.call_args.kwargs)
        event = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(event["actor"], self.client.actor)
        page_reply = self.adapter.handle(rpc("tools/call", {"name": "read_turn_page",
            "arguments": {"reader": "chat.stable", "session": "turn-2"}}, modern=True))
        self.assertFalse(page_reply.get("result", {}).get("isError", True), page_reply)
        page = json.loads(page_reply["result"]["content"][0]["text"])
        self.assertEqual([item["id"] for item in page["events"]], [event["id"]])
        ack = self.adapter.handle(rpc("tools/call", {"name": "acknowledge_turn_page",
            "arguments": {"reader": "chat.stable", "receipt": page["receipt"]}}, modern=True))
        self.assertFalse(ack["result"]["isError"])
        self.assertTrue(json.loads(ack["result"]["content"][0]["text"])["acknowledged"])
        tail = self.adapter.handle(rpc("tools/call", {"name": "latest20", "arguments": {}}, modern=True))
        self.assertFalse(tail["result"]["isError"])
        self.assertEqual(self.ledger.verify()["count"], 2)

    def test_incomplete_or_invalid_modern_metadata_never_bypasses_classic_gate(self):
        invalid = [None, [], {VERSION_KEY: "2026-07-28"},
                   {VERSION_KEY: "2026-07-28", CAPABILITIES_KEY: None},
                   {VERSION_KEY: "2026-07-28", CAPABILITIES_KEY: []},
                   {VERSION_KEY: 20260728, CAPABILITIES_KEY: {}},
                   {**modern_meta(), CLIENT_INFO_KEY: "not-an-object"},
                   {**modern_meta(), "progressToken": True}]
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                reply = self.adapter.handle(rpc("tools/list", {"_meta": metadata}))
                self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(self.adapter.handle(rpc("tools/list"))["error"]["code"], -32002)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_future_protocol_version_returns_standard_negotiation_error(self):
        metadata = modern_meta()
        metadata[VERSION_KEY] = "2099-01-01"
        reply = self.adapter.handle(rpc("server/discover", {"_meta": metadata}))
        self.assertEqual(reply["error"]["code"], -32022)
        self.assertEqual(reply["error"]["data"]["requested"], "2099-01-01")
        self.assertIn("2026-07-28", reply["error"]["data"]["supported"])

    def test_modern_initialize_cannot_open_a_classic_session(self):
        result = self.adapter.handle(rpc("initialize", {
            "protocolVersion": "2026-07-28", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}, modern=True))
        self.assertEqual(result["error"]["code"], -32601)
        self.adapter.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(self.adapter.handle(rpc("tools/list"))["error"]["code"], -32002)

    def test_classic_session_accepts_known_metadata_without_forwarding_it(self):
        initialized = self.initialize_classic()
        self.assertIn("instructions", initialized["result"])
        reply = self.adapter.handle(rpc("tools/list", {"_meta": {"progressToken": "fixture-progress",
            VERSION_KEY: "2025-06-18", CLIENT_INFO_KEY: {"name": "classic", "version": "1"}}}))
        self.assertIn("tools", reply.get("result", {}), reply)
        self.assertNotIn("resultType", reply["result"])
        reply = self.adapter.handle(rpc("tools/call", {"name": "latest20", "arguments": {},
                                                       "_meta": {"progressToken": 1}}))
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_metadata_cannot_retarget_tool_arguments_or_disable_legacy_notification_guard(self):
        self.adapter.handle(rpc("initialize", {"protocolVersion": "2025-06-18",
            "capabilities": {}, "clientInfo": {"name": "classic", "version": "1"}}))
        self.adapter.handle({"jsonrpc": "2.0", "method": "notifications/initialized",
                             "params": {"_meta": modern_meta()}})
        self.assertEqual(self.adapter.handle(rpc("tools/list"))["error"]["code"], -32002)
        for addition in ({"url": "https://untrusted.invalid"},
                         {"_meta": {"actor": "untrusted.actor"}}):
            reply = self.adapter.handle(rpc("tools/call", {"name": "latest20",
                "arguments": addition}, modern=True))
            self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_tool_annotations_truthfully_describe_reads_and_writes(self):
        reply = self.adapter.handle(rpc("tools/list", modern=True))
        self.assertIn("tools", reply.get("result", {}), reply)
        tools = {tool["name"]: tool for tool in reply["result"]["tools"]}
        for name, tool in tools.items():
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])
            self.assertEqual(tool["annotations"]["readOnlyHint"], name in ("latest20", "latest_activity20"))
            self.assertEqual(tool["annotations"]["idempotentHint"],
                             name in ("latest20", "latest_activity20", "record_activity", "acknowledge_turn_page"))

    def test_modern_wire_discovery_and_tool_list_are_self_contained(self):
        requests = [rpc("server/discover", modern=True, identifier=1),
                    rpc("tools/list", modern=True, identifier=2),
                    rpc("tools/call", {"name": "latest20", "arguments": {}},
                        modern=True, identifier=3)]
        incoming = io.BytesIO(b"".join((json.dumps(value) + "\n").encode("utf-8")
                                      for value in requests))
        outgoing = io.BytesIO()
        self.adapter.serve(incoming, outgoing)
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in responses], [1, 2, 3])
        self.assertTrue(all("result" in reply for reply in responses), responses)
        self.assertEqual(self.ledger.verify()["count"], 0)


if __name__ == "__main__":
    unittest.main()
