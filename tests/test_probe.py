"""Remote-only Blotter diagnostics never create a second local ledger."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from agent_blotter import Blotter
from agent_blotter.probe import main
from agent_blotter.server import create_server


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Blotter(Path(self.tmp.name) / "central.sqlite3")
        self.token = "A" * 40
        self.secret_file = Path(self.tmp.name) / "actor.token"
        self.secret_file.write_text(self.token, encoding="utf-8")
        if os.name == "posix":
            self.secret_file.chmod(0o600)
        self.server = create_server(self.store, {self.token: "probe.agent"}, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.tmp.cleanup()

    def invoke(self, *argv, env=None):
        stream = io.StringIO()
        # Keep proxy bypass and system SSL environment intact; only erase
        # Blotter defaults to exercise explicit client configuration.
        with contextlib.redirect_stdout(stream), mock.patch.dict(
                os.environ, {"BLOTTER_URL": "", "BLOTTER_TOKEN": "", **(env or {})}):
            code = main(list(argv))
        return code, json.loads(stream.getvalue())

    def args(self):
        return ("--url", self.url, "--actor", "probe.agent",
                "--token-file", str(self.secret_file))

    def test_missing_configuration_does_not_create_local_ledger(self):
        home = Path(self.tmp.name) / "untouched-home"
        with mock.patch("pathlib.Path.home", return_value=home):
            code, result = self.invoke("--actor", "probe.agent")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CONFIG_MISSING")
        self.assertFalse(home.exists())

    def test_readonly_probe_reports_health_read_and_missing_tail_without_write(self):
        code, result = self.invoke(*self.args())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["health"], "VERIFIED")
        self.assertEqual(result["read"], "VERIFIED")
        self.assertEqual(result["tail"], "NOT_DEPLOYED")
        self.assertEqual(result["append"], "NOT_ATTEMPTED")
        self.assertEqual(self.store.verify()["count"], 0)
        self.assertNotIn(self.token, json.dumps(result))

    def test_bad_token_is_distinct_and_never_appends(self):
        self.secret_file.write_text("X" * 40, encoding="utf-8")
        code, result = self.invoke(*self.args())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "AUTHORIZATION_FAILED")
        self.assertEqual(self.store.verify()["count"], 0)
        self.assertNotIn("X" * 40, json.dumps(result))

    def test_control_characters_in_token_fail_before_network(self):
        with mock.patch("agent_blotter.probe.RemoteBlotter") as client:
            code, result = self.invoke("--url", self.url, "--actor", "probe.agent",
                                       env={"BLOTTER_TOKEN": "A" * 40 + "\r\n"})
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CONFIG_MISSING")
        client.assert_not_called()

    def test_malformed_recent_history_never_reports_available(self):
        with mock.patch("agent_blotter.probe.RemoteBlotter") as cls:
            client = cls.return_value
            client._request.side_effect = [
                {"ok": True}, {"events": [{"seq": "1"}]}]
            client.list.return_value = []
            code, result = self.invoke(*self.args())
        self.assertEqual(code, 0)
        self.assertEqual(result["tail"], "INVALID_RESPONSE")

    def test_wrong_bound_actor_fails_closed(self):
        args = ("--url", self.url, "--actor", "wrong.actor",
                "--token-file", str(self.secret_file))
        code, result = self.invoke(*args)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "AUTHORIZATION_FAILED")

    def test_unreachable_port_is_not_reported_as_auth_failure(self):
        args = ("--url", "http://127.0.0.1:1", "--actor", "probe.agent",
                "--token-file", str(self.secret_file), "--timeout", "0.25")
        code, result = self.invoke(*args)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CONNECTIVITY_FAILED")

    def test_explicit_append_repeated_with_event_id_is_exactly_once(self):
        argv = (*self.args(), "--append-test", "--event-id", "probe-event-20261027",
                "--session", "probe-test-001")
        code, result = self.invoke(*argv)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["append"], "VERIFIED")
        self.assertEqual(result["append_seq"], 1)
        self.assertEqual(self.store.verify()["count"], 1)
        code2, second = self.invoke(*argv)
        self.assertEqual(code2, 0, second)
        self.assertEqual(second["append_seq"], 1)
        self.assertEqual(self.store.verify()["count"], 1)

    def test_write_requires_explicit_idempotency_key(self):
        code, result = self.invoke(*self.args(), "--append-test")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CONFIG_MISSING")
        self.assertEqual(self.store.verify()["count"], 0)

    @unittest.skipUnless(os.name == "posix", "POSIX token custody only")
    def test_world_readable_token_file_fails_closed(self):
        self.secret_file.chmod(0o644)
        code, result = self.invoke(*self.args())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CONFIG_PERMISSION_DENIED")
        self.assertEqual(self.store.verify()["count"], 0)

    @unittest.skipUnless(os.name == "posix", "POSIX token custody only")
    def test_symlinked_token_file_fails_closed(self):
        alias = Path(self.tmp.name) / "alias.token"
        alias.symlink_to(self.secret_file)
        args = ("--url", self.url, "--actor", "probe.agent",
                "--token-file", str(alias))
        code, result = self.invoke(*args)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CONFIG_PERMISSION_DENIED")
        self.assertEqual(self.store.verify()["count"], 0)

    def test_token_file_is_denied_when_windows_acl_cannot_be_verified(self):
        from agent_blotter.probe import _read_private_token
        with mock.patch("agent_blotter.probe.os.name", "nt"):
            with self.assertRaises(PermissionError):
                _read_private_token(self.secret_file)

    def test_private_probe_http_client_does_not_inherit_proxy(self):
        from agent_blotter.remote import RemoteBlotter, _DIRECT_NO_REDIRECT_OPENER
        import urllib.request
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:8555"}):
            client = RemoteBlotter(self.url, actor="probe.agent", token=self.token,
                                   direct_only=True)
        self.assertIs(client._opener, _DIRECT_NO_REDIRECT_OPENER)
        proxies = [handler.proxies for handler in client._opener.handlers
                   if isinstance(handler, urllib.request.ProxyHandler)]
        self.assertFalse(any(proxies), proxies)

    def test_unpinned_remote_destination_fails_before_network(self):
        with mock.patch("agent_blotter.probe.RemoteBlotter") as client:
            code, result = self.invoke("--url", "https://attacker.example/blotter",
                                       "--actor", "probe.agent", "--token-file",
                                       str(self.secret_file))
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CONFIG_TARGET_UNTRUSTED")
        client.assert_not_called()

    def test_retarged_url_differs_from_operator_pin(self):
        with mock.patch("agent_blotter.probe.RemoteBlotter") as client:
            code, result = self.invoke("--url", "https://other.example/blotter",
                                       "--actor", "probe.agent",
                                       env={"BLOTTER_URL": "https://lappy.tailnet/blotter"})
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CONFIG_TARGET_UNTRUSTED")
        client.assert_not_called()

    def test_loopback_retarget_is_denied_when_operator_pins_remote(self):
        with mock.patch("agent_blotter.probe.RemoteBlotter") as client:
            code, result = self.invoke("--url", self.url, "--actor", "probe.agent",
                                       env={"BLOTTER_URL": "https://lappy.tailnet/blotter"})
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CONFIG_TARGET_UNTRUSTED")
        client.assert_not_called()

    def test_malformed_readback_is_reported_without_traceback(self):
        response = {"seq": 7, "id": "probe-event-007"}
        with mock.patch("agent_blotter.probe.RemoteBlotter") as cls:
            client = cls.return_value
            client._request.return_value = {"ok": True, "events": []}
            client.list.side_effect = [[], None]
            client.record.return_value = response
            code, result = self.invoke(*self.args(), "--append-test",
                                       "--event-id", "probe-event-007")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "VERIFICATION_FAILED")
        self.assertEqual(result["append"], "ATTEMPTED_UNCONFIRMED")

    def test_second_append_transport_failure_preserves_attempt_state(self):
        from agent_blotter.store import BlotterError
        with mock.patch("agent_blotter.probe.RemoteBlotter") as cls:
            client = cls.return_value
            client._request.return_value = {"ok": True, "events": []}
            client.list.return_value = []
            client.record.side_effect = [
                {"seq": 7, "id": "probe-event-007"},
                BlotterError("connection failed")]
            code, result = self.invoke(*self.args(), "--append-test",
                                       "--event-id", "probe-event-007")
        self.assertEqual(code, 2)
        self.assertEqual(result["append"], "ATTEMPTED_UNCONFIRMED")
        self.assertNotEqual(result["status"], "OK")


if __name__ == "__main__":
    unittest.main()
