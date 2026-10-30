"""Wire- and authority-boundary tests for the dependency-free MCP adapter."""
import ctypes
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

from agent_blotter import Blotter, BlotterError
from agent_blotter.mcp_stdio import (BlotterMCP, MAX_INPUT_BYTES,
                                     _check_windows_token_acl, _load_token_file,
                                     _validate_config_url, main as mcp_main)
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
    def test_cli_private_tailnet_mcp_bypasses_environment_proxy(self):
        """MCP adapter always talks directly to the pinned private destination."""
        with mock.patch("agent_blotter.mcp_stdio._load_token_file", return_value="T" * 40), \
             mock.patch("agent_blotter.mcp_stdio.RemoteBlotter") as remote, \
             mock.patch("agent_blotter.mcp_stdio.BlotterMCP"), \
             mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://example.invalid:8888"}):
            self.assertEqual(mcp_main(["--actor", "bt2.one", \
                                        "--token-file", "private.token"]), 0)
        self.assertTrue(remote.call_args.kwargs["direct_only"])

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
        if os.name == "nt":
            self.skipTest("POSIX mode bits do not validate Windows token ACLs")
        token_path = Path(self.temp.name, "token")
        token_path.write_text("S" * 40, encoding="utf-8")
        token_path.chmod(0o644)
        with self.assertRaises(ValueError):
            _load_token_file(token_path)
        token_path.chmod(0o600)
        self.assertEqual(_load_token_file(token_path), "S" * 40)

    def test_windows_acl_validation_fails_closed(self):
        class Process:
            returncode = 0
            stdout = "OK\n"
        def system_directory(buffer, capacity):
            buffer.value = r"C:\Windows\System32"
            return len(buffer.value)
        with mock.patch.object(ctypes, "windll", create=True) as windows:
            windows.kernel32.GetSystemDirectoryW.side_effect = system_directory
            with mock.patch("agent_blotter.mcp_stdio.subprocess.run", return_value=Process()) as run:
                _check_windows_token_acl(Path("C:/private/agent.token"))
                self.assertIn("BLOTTER_MCP_TOKEN_PATH", run.call_args.kwargs["env"])
                self.assertNotIn("S" * 40, str(run.call_args))
                script = run.call_args.args[0][-1]
                self.assertIn("$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value", script)
                self.assertIn("if ($ownerSid -notin $permitted) { exit 5 }", script)
            for status, output in ((1, ""), (5, ""), (0, "not OK")):
                with self.subTest(status=status, output=output):
                    with mock.patch("agent_blotter.mcp_stdio.subprocess.run") as run:
                        run.return_value.returncode = status
                        run.return_value.stdout = output
                        with self.assertRaises(ValueError):
                            _check_windows_token_acl(Path("C:/private/agent.token"))

    def test_windows_acl_uses_os_system_directory_not_search_path(self):
        class Process:
            returncode = 0
            stdout = "OK\n"

        def system_directory(buffer, capacity):
            buffer.value = r"C:\Windows\System32"
            return len(buffer.value)

        with mock.patch.object(ctypes, "windll", create=True) as windows, \
             mock.patch("agent_blotter.mcp_stdio.subprocess.run",
                        return_value=Process()) as run, \
             mock.patch.dict(os.environ, {"PATH": r"C:\untrusted",
                                       "SystemRoot": r"C:\untrusted"}):
            windows.kernel32.GetSystemDirectoryW.side_effect = system_directory
            _check_windows_token_acl(Path("C:/private/agent.token"))

        binary = PureWindowsPath(run.call_args.args[0][0])
        self.assertTrue(binary.is_absolute())
        self.assertEqual(binary, PureWindowsPath(
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"))

    def test_windows_acl_rejects_unavailable_system_directory(self):
        with mock.patch.object(ctypes, "windll", create=True) as windows, \
             mock.patch("agent_blotter.mcp_stdio.subprocess.run") as run:
            windows.kernel32.GetSystemDirectoryW.return_value = 0
            with self.assertRaises(ValueError):
                _check_windows_token_acl(Path("C:/private/agent.token"))
            self.assertEqual(run.call_count, 0)

    def test_pinned_urls_reject_credential_exfiltration_destinations(self):
        self.assertEqual(_validate_config_url(self.bridge.client.url), self.bridge.client.url)
        for url in ("https://attacker.example/blotter", "http://attacker.example",
                    "http://127.0.0.1:19083",
                    "https://lappy.tail86ea75.ts.net/blotter/../evil",
                    "https://lappy.tail86ea75.ts.net/blotter?dest=evil"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _validate_config_url(url)

    def test_private_operator_pin_allows_only_exact_nas_tailnet_endpoint(self):
        if os.name != "posix":
            self.skipTest("operator pin files require POSIX descriptor-bound permissions")
        trusted = "https://nas.tail86ea75.ts.net/blotter"
        pin = Path(self.temp.name) / "trusted-endpoint"
        pin.write_text(trusted + "\n", encoding="ascii")
        pin.chmod(0o600)
        self.assertEqual(_validate_config_url(trusted, pin_file=pin), trusted)
        for destination in ("https://attacker.example/blotter",
                            trusted + "/../evil", trusted + "?forward=1",
                            "http://nas.tail86ea75.ts.net/blotter"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                _validate_config_url(destination, pin_file=pin)

    def test_operator_pin_rejects_unsafe_file_and_non_tailnet_destination(self):
        if os.name != "posix":
            self.skipTest("operator pin files require POSIX descriptor-bound permissions")
        pin = Path(self.temp.name) / "trusted-endpoint"
        pin.write_text("https://attacker.example/blotter\n", encoding="ascii")
        pin.chmod(0o600)
        with self.assertRaises(ValueError):
            _validate_config_url("https://attacker.example/blotter", pin_file=pin)
        pin.write_text("https://nas.tail86ea75.ts.net/blotter\n", encoding="ascii")
        pin.chmod(0o644)
        with self.assertRaises(ValueError):
            _validate_config_url("https://nas.tail86ea75.ts.net/blotter", pin_file=pin)
        pin.chmod(0o600)
        link = Path(self.temp.name) / "link"
        link.symlink_to(pin)
        with self.assertRaises(ValueError):
            _validate_config_url("https://nas.tail86ea75.ts.net/blotter", pin_file=link)
        duplicate = Path(self.temp.name) / "hardlink"
        os.link(pin, duplicate)
        with self.assertRaises(ValueError):
            _validate_config_url("https://nas.tail86ea75.ts.net/blotter", pin_file=pin)

    def test_untrusted_pin_rejected_before_bearer_token_is_read(self):
        if os.name != "posix":
            self.skipTest("operator pin files require POSIX descriptor-bound permissions")
        pin = Path(self.temp.name) / "untrusted-endpoint"
        pin.write_text("https://attacker.example/blotter\n", encoding="ascii")
        pin.chmod(0o600)
        with mock.patch("agent_blotter.mcp_stdio._load_token_file") as read_token, \
             mock.patch("agent_blotter.mcp_stdio.RemoteBlotter") as remote:
            self.assertEqual(mcp_main(["--actor", "bt2.one", "--url",
                                       "https://attacker.example/blotter",
                                       "--pinned-url-file", str(pin),
                                       "--token-file", "private.token"]), 2)
        read_token.assert_not_called()
        remote.assert_not_called()

    def test_cli_private_pin_keeps_direct_only_and_binds_actor(self):
        if os.name != "posix":
            self.skipTest("operator pin files require POSIX descriptor-bound permissions")
        trusted = "https://nas.tail86ea75.ts.net/blotter"
        pin = Path(self.temp.name) / "trusted-endpoint"
        pin.write_text(trusted + "\n", encoding="ascii")
        pin.chmod(0o600)
        with mock.patch("agent_blotter.mcp_stdio._load_token_file", return_value="T" * 40), \
             mock.patch("agent_blotter.mcp_stdio.RemoteBlotter") as remote, \
             mock.patch("agent_blotter.mcp_stdio.BlotterMCP"):
            self.assertEqual(mcp_main(["--actor", "bt2.one", "--url", trusted,
                                      "--pinned-url-file", str(pin),
                                      "--token-file", "private.token"]), 0)
            self.assertEqual(remote.call_args.args[0], trusted)
            self.assertEqual(remote.call_args.kwargs["actor"], "bt2.one")
            self.assertTrue(remote.call_args.kwargs["direct_only"])


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
