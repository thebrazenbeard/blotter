"""Wire- and authority-boundary tests for the dependency-free MCP adapter."""
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_blotter import Blotter, BlotterError
from agent_blotter.mcp_stdio import (BlotterMCP, MAX_INPUT_BYTES,
                                     _check_windows_token_acl, _load_token_file,
                                     _validate_config_url)
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server


class LocalRemote:
    actor = "bt2.one"
    url = "https://lappy.tail86ea75.ts.net/blotter"

    def __init__(self, ledger):
        self.ledger = ledger

    def check(self, **kwargs):
        return self.ledger.check(actor=self.actor, **kwargs)

    def list(self, **kwargs):
        return self.ledger.list(**kwargs)

    def tail(self, **kwargs):
        # Only the test double uses this read; production calls PR7 /v1/tail.
        return self.ledger.list(after_seq=0, limit=1000)[-kwargs.get("limit", 20):]

    def record(self, **kwargs):
        return self.ledger.record(actor=self.actor, **kwargs)


def rpc(identifier, method, params=None):
    return {"jsonrpc": "2.0", "id": identifier, "method": method,
            "params": {} if params is None else params}


class StdioAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Blotter(Path(self.temp.name) / "ledger.sqlite3")
        self.bridge = BlotterMCP(LocalRemote(self.db), Path(self.temp.name) / "cursors")

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self):
        result = self.bridge.handle(rpc(1, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}))
        self.assertEqual(result["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def test_requires_handshake_and_exposes_only_three_tools(self):
        before = self.bridge.handle(rpc(1, "tools/list"))
        self.assertEqual(before["error"]["code"], -32002)
        self.initialize()
        tools = self.bridge.handle(rpc(2, "tools/list"))["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools],
                         ["begin_turn", "latest20", "record_activity"])
        for tool in tools:
            self.assertFalse(any("token" in str(prop).lower()
                                 for prop in tool["inputSchema"]["properties"]))
        self.assertEqual(self.bridge.handle(rpc(3, "resources/list"))["error"]["code"], -32601)

    def test_one_shared_lane_cursor_and_idempotent_record(self):
        self.initialize()
        first = self.bridge.handle(rpc(2, "tools/call", {"name": "record_activity", "arguments": {
            "session": "turn-1", "kind": "test.executed", "message": "First actual test",
            "event_id": "event-one", "evidence": "OBSERVATION"}}))
        self.assertFalse(first["result"]["isError"])
        dup = self.bridge.handle(rpc(3, "tools/call", {"name": "record_activity", "arguments": {
            "session": "turn-1", "kind": "test.executed", "message": "First actual test",
            "event_id": "event-one", "evidence": "OBSERVATION"}}))
        self.assertEqual(first["result"], dup["result"])
        begin = self.bridge.handle(rpc(4, "tools/call", {"name": "begin_turn",
                                                    "arguments": {"session": "turn-2"}}))
        value = json.loads(begin["result"]["content"][0]["text"])
        self.assertEqual([x["kind"] for x in value["events"]], ["test.executed"])
        self.assertTrue(value["cursor_hash"])
        self.assertEqual(len(list(Path(self.temp.name, "cursors").glob("*.json"))), 1)
        tail = self.bridge.handle(rpc(5, "tools/call", {"name": "latest20", "arguments": {}}))
        data = json.loads(tail["result"]["content"][0]["text"])
        self.assertEqual([x["kind"] for x in data["events"]], ["test.executed", "turn.checked"])
        self.assertEqual(self.db.verify()["count"], 2)

    def test_latest20_truly_returns_last_20_without_modifying_ledger(self):
        self.initialize()
        for i in range(235):
            self.db.record(actor="other", kind="test.executed", message=f"event-{i}")
        reply = self.bridge.handle(rpc(2, "tools/call", {"name": "latest20", "arguments": {}}))
        data = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual([v["seq"] for v in data["events"]], list(range(216, 236)))
        self.assertEqual(data["head_seq"], 235)
        self.assertEqual(self.db.verify()["count"], 235)

    def test_missing_tail_endpoint_fails_without_partial_history(self):
        self.initialize()
        self.bridge.client.tail = None
        reply = self.bridge.handle(rpc(2, "tools/call", {"name": "latest20", "arguments": {}}))
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(self.db.verify()["count"], 0)

    def test_oversize_begin_turn_does_not_persist_cursor_or_drop_history(self):
        for i in range(10):
            self.db.record(actor="peer", kind="test.executed",
                           message="A long audit event " + "x" * 150)
        self.initialize()
        with mock.patch("agent_blotter.mcp_stdio.MAX_BEGIN_BYTES", 500):
            rejected = self.bridge.handle(rpc(2, "tools/call", {"name": "begin_turn",
                                                "arguments": {"session": "oversize1"}}))
        self.assertTrue(rejected["result"]["isError"])
        self.assertEqual(list(Path(self.temp.name, "cursors").glob("*.json")), [])
        # The rejected check may append its own marker, but all old substantive
        # records are still unseen and must reappear on a subsequent full replay.
        accepted = self.bridge.handle(rpc(3, "tools/call", {"name": "begin_turn",
                                            "arguments": {"session": "replay2"}}))
        self.assertFalse(accepted["result"]["isError"])
        history = json.loads(accepted["result"]["content"][0]["text"])["events"]
        self.assertEqual(sum(ev["kind"] == "test.executed" for ev in history), 10)
        self.assertEqual(len(list(Path(self.temp.name, "cursors").glob("*.json"))), 1)

    def test_malicious_transport_error_cannot_echo_credential(self):
        secret = "S" * 48
        self.initialize()
        def reject(**kwargs):
            raise BlotterError("credential might leak: " + secret)
        self.bridge.client.record = reject
        reply = self.bridge.handle(rpc(2, "tools/call", {"name": "record_activity",
            "arguments": {"session": "s", "kind": "test.executed",
                          "message": "actual activity", "event_id": "error-1"}}))
        self.assertTrue(reply["result"]["isError"])
        self.assertNotIn(secret, json.dumps(reply))

    def test_hostile_inputs_do_not_change_ledger_or_allow_retarget(self):
        self.initialize()
        for args in (
            {"session": "s", "kind": "test.executed", "message": "m"},
            {"session": "s", "kind": "test.executed", "message": "m", "event_id": "e1",
             "url": "https://attacker.example/"},
            {"session": "s", "kind": "test.executed", "message": "m", "event_id": "e1",
             "payload": {"nested": "x" * 10000}},
        ):
            with self.subTest(args=args):
                reply = self.bridge.handle(rpc(2, "tools/call", {"name": "record_activity",
                                                        "arguments": args}))
                self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(self.bridge.handle(rpc(3, "tools/call", {
            "name": "begin_turn", "arguments": {"session": "s", "re_enroll_legacy": True}}))
            ["error"]["code"], -32602)
        self.assertEqual(self.bridge.handle(rpc(4, "tools/call", {
            "name": "begin_turn", "arguments": {"session": "../unsafe"}}))
            ["error"]["code"], -32602)
        self.assertEqual(self.db.verify()["count"], 0)

    def test_stdio_utf8_line_protocol_and_invalid_requests(self):
        traffic = [rpc(1, "initialize", {"protocolVersion": "2025-06-18",
                                         "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}),
                   {"jsonrpc": "2.0", "method": "notifications/initialized"},
                   rpc(2, "tools/list"), rpc(3, "tools/call", {"name": "latest20", "arguments": {}}),
                   rpc(4, "tools/call", {"name": "not-exposed", "arguments": {}})]
        inp = io.BytesIO(b"".join((json.dumps(v) + "\n").encode("utf-8") for v in traffic))
        out = io.BytesIO()
        self.bridge.serve(inp, out)
        msgs = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
        self.assertEqual([m["id"] for m in msgs], [1, 2, 3, 4])
        self.assertEqual(msgs[-1]["error"]["code"], -32602)
        self.assertEqual(self.db.verify()["count"], 0)

    def test_bounded_wire_rejects_oversize_and_nonnumeric_json_constant(self):
        request = (b"{" + b"x" * MAX_INPUT_BYTES + b"}\n")
        request += b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"n":NaN}}\n'
        request += (json.dumps(rpc(2, "ping")) + "\n").encode("utf-8")
        out = io.BytesIO()
        self.bridge.serve(io.BytesIO(request), out)
        messages = [json.loads(line) for line in out.getvalue().decode("ascii").splitlines()]
        self.assertEqual([value["error"]["code"] for value in messages[:2]], [-32700, -32700])
        self.assertEqual(messages[2]["result"], {})
        self.assertEqual(self.db.verify()["count"], 0)

    def test_missing_final_newline_is_rejected(self):
        out = io.BytesIO()
        self.bridge.serve(io.BytesIO(json.dumps(rpc(1, "ping")).encode("utf-8")), out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)

    def test_duplicate_keys_rejected_in_any_json_object(self):
        data = (b'{"jsonrpc":"2.0","id":1,"method":"ping","method":"tools/list"}\n'
                b'{"jsonrpc":"2.0","id":2,"method":"tools/call",'
                b'"params":{"name":"latest20","name":"record_activity","arguments":{}}}\n')
        out = io.BytesIO()
        self.bridge.serve(io.BytesIO(data), out)
        self.assertEqual([json.loads(line)["error"]["code"]
                          for line in out.getvalue().splitlines()], [-32700, -32700])
        self.assertEqual(self.db.verify()["count"], 0)

    def test_per_process_latest20_rate_limit(self):
        self.initialize()
        for i in range(10):
            reply = self.bridge.handle(rpc(i + 2, "tools/call",
                    {"name": "latest20", "arguments": {}}))
            self.assertFalse(reply["result"]["isError"])
        rejected = self.bridge.handle(rpc(20, "tools/call",
                    {"name": "latest20", "arguments": {}}))
        self.assertTrue(rejected["result"]["isError"])
        self.assertEqual(self.db.verify()["count"], 0)

    def test_private_token_file_rejects_world_read_permissions(self):
        token_path = Path(self.temp.name, "token")
        token_path.write_text("S" * 40, encoding="utf-8")
        if os.name != "nt":
            token_path.chmod(0o644)
            with self.assertRaises(ValueError):
                _load_token_file(token_path)
            token_path.chmod(0o600)
        self.assertEqual(_load_token_file(token_path), "S" * 40)

    def test_windows_acl_validation_fails_closed(self):
        class Process:
            returncode = 0
            stdout = "OK\n"
        with mock.patch("agent_blotter.mcp_stdio.subprocess.run", return_value=Process()) as run:
            _check_windows_token_acl(Path("C:/private/agent.token"))
            self.assertIn("BLOTTER_MCP_TOKEN_PATH", run.call_args.kwargs["env"])
            self.assertNotIn("S" * 40, str(run.call_args))
        for status, output in ((1, ""), (0, "not OK")):
            with self.subTest(status=status, output=output):
                with mock.patch("agent_blotter.mcp_stdio.subprocess.run") as run:
                    run.return_value.returncode = status
                    run.return_value.stdout = output
                    with self.assertRaises(ValueError):
                        _check_windows_token_acl(Path("C:/private/agent.token"))

    def test_pinned_urls_reject_credential_exfiltration_destinations(self):
        self.assertEqual(_validate_config_url(self.bridge.client.url), self.bridge.client.url)
        for url in ("https://attacker.example/blotter", "http://attacker.example",
                    "http://127.0.0.1:19083",
                    "https://lappy.tail86ea75.ts.net/blotter/../evil",
                    "https://lappy.tail86ea75.ts.net/blotter?dest=evil"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _validate_config_url(url)


class AuthenticatedRemoteIntegrationTests(unittest.TestCase):
    def test_mcp_real_remote_identity_and_backend_redaction(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Blotter(Path(temp) / "central.sqlite3")
            token = "S" * 40
            server = create_server(ledger, {token: "bt2.one"}, port=0)
            worker = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                client = RemoteBlotter(url, actor="bt2.one", token=token)
                bridge = BlotterMCP(client, Path(temp) / "cursors")
                bridge.handle(rpc(1, "initialize", {"protocolVersion": "2025-06-18",
                             "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}))
                bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
                note = bridge.handle(rpc(2, "tools/call", {"name": "record_activity",
                    "arguments": {"session": "remote1", "kind": "test.executed",
                    "message": "Observed → test", "event_id": "unique-test",
                    "payload": {"token": token}}}))
                self.assertFalse(note["result"]["isError"])
                self.assertNotIn(token, json.dumps(note))
                begin = bridge.handle(rpc(3, "tools/call", {"name": "begin_turn",
                    "arguments": {"session": "remote2"}}))
                self.assertFalse(begin["result"]["isError"])
                tail = bridge.handle(rpc(4, "tools/call", {"name": "latest20",
                    "arguments": {}}))
                self.assertNotIn(token, json.dumps(tail))
                self.assertEqual(ledger.verify()["count"], 2)
                self.assertEqual(json.loads(tail["result"]["content"][0]["text"])
                                 ["events"][0]["payload"]["token"], "[REDACTED]")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
