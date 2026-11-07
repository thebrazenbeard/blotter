"""Independent hostile checks using synthetic credentials and isolated HTTP servers.

These tests qualify a candidate's client boundaries. They make no production,
installation, recovery, global instrumentation, or authority claim.
"""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from agent_blotter import Blotter, probe
from agent_blotter.server import create_server


SYNTHETIC_TOKEN = "synthetic-hostile-credential-" + "A" * 32
PRIVATE_SENTINEL = "SYNTHETIC-PRIVATE-BODY-DO-NOT-EMIT"


@contextmanager
def running_server(server):
    worker = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def invoke(url, *, actor="synthetic.hostile", token=SYNTHETIC_TOKEN,
           extra=(), pin=""):
    output, errors = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, {"BLOTTER_URL": pin, "BLOTTER_TOKEN": token}), \
            redirect_stdout(output), redirect_stderr(errors):
        code = probe.main(["--url", url, "--actor", actor, *extra])
    return code, json.loads(output.getvalue()), output.getvalue() + errors.getvalue()


def scripted_server(respond):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append((self.command, self.path, dict(self.headers)))
            status, body, headers = respond(self.path)
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            requests.append((self.command, self.path, dict(self.headers)))
            self.send_error(405)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler), requests


class HostileHTTPBoundaryTests(unittest.TestCase):
    def test_missing_wrong_or_cross_actor_credentials_never_append(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Blotter(Path(directory) / "synthetic.sqlite3")
            server = create_server(store, {SYNTHETIC_TOKEN: "synthetic.hostile"}, port=0)
            with running_server(server) as url:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                                        timeout=2)
                try:
                    connection.request("GET", "/v1/tail?limit=20")
                    reply = connection.getresponse()
                    self.assertEqual(reply.status, 401)
                    reply.read()
                finally:
                    connection.close()
                for actor, token in (("synthetic.hostile", "B" * 40),
                                     ("synthetic.other", SYNTHETIC_TOKEN)):
                    with self.subTest(actor=actor):
                        code, result, emitted = invoke(url, actor=actor, token=token)
                        self.assertEqual((code, result["status"]),
                                         (2, "AUTHORIZATION_FAILED"))
                        self.assertNotIn(token, emitted)
                self.assertEqual(store.verify()["count"], 0)

    def test_all_redirect_statuses_stop_without_forwarding_bearer(self):
        sink, received = scripted_server(lambda _path: (200, {"ok": True}, {}))
        with running_server(sink) as sink_url:
            for redirect in (301, 302, 303, 307, 308):
                with self.subTest(status=redirect):
                    source, requests = scripted_server(
                        lambda _path: (redirect, {"error": PRIVATE_SENTINEL},
                                       {"Location": sink_url + "/stolen"}))
                    with running_server(source) as url:
                        code, result, emitted = invoke(url)
                    self.assertEqual((code, result["status"]), (2, "READ_FAILED"))
                    self.assertEqual(len(requests), 1)
                    self.assertNotIn(SYNTHETIC_TOKEN, emitted)
                    self.assertNotIn(PRIVATE_SENTINEL, emitted)
            self.assertEqual(received, [], "redirect destination received a credentialed request")

    def test_legacy_missing_tail_is_explicit_and_never_becomes_first_page_tail(self):
        def respond(path):
            if path == "/v1/health":
                return 200, {"ok": True}, {}
            if path == "/v1/events?after_seq=0&limit=1":
                return 200, {"events": [{"seq": 1, "message": PRIVATE_SENTINEL}]}, {}
            return 404, {"error": PRIVATE_SENTINEL}, {}

        server, requests = scripted_server(respond)
        with running_server(server) as url:
            code, result, emitted = invoke(url)
        self.assertEqual(code, 0)
        self.assertEqual(result, {"status": "OK", "stage": "read", "health": "VERIFIED",
                                  "read": "VERIFIED", "tail": "NOT_DEPLOYED",
                                  "append": "NOT_ATTEMPTED"})
        self.assertEqual([(method, path) for method, path, _headers in requests], [
            ("GET", "/v1/health"), ("GET", "/v1/events?after_seq=0&limit=1"),
            ("GET", "/v1/tail?limit=20")])
        self.assertNotIn(PRIVATE_SENTINEL, emitted)
        self.assertNotIn(SYNTHETIC_TOKEN, emitted)


class HostileConfigurationTests(unittest.TestCase):
    def test_untrusted_target_rejected_before_credential_file_or_network_access(self):
        with mock.patch.object(probe, "_read_private_token") as reader, \
                mock.patch.object(probe, "RemoteBlotter") as client:
            code, result, emitted = invoke(
                "https://synthetic-attacker.invalid/ledger",
                extra=("--token-file", PRIVATE_SENTINEL))
        self.assertEqual((code, result["status"]), (2, "CONFIG_TARGET_UNTRUSTED"))
        reader.assert_not_called()
        client.assert_not_called()
        self.assertNotIn(PRIVATE_SENTINEL, emitted)

    def test_pinned_endpoint_cannot_be_retargeted_even_to_loopback(self):
        with mock.patch.object(probe, "_read_private_token") as reader, \
                mock.patch.object(probe, "RemoteBlotter") as client:
            code, result, _emitted = invoke(
                "http://127.0.0.1:1", pin="https://synthetic-approved.invalid/blotter",
                extra=("--token-file", PRIVATE_SENTINEL))
        self.assertEqual((code, result["status"]), (2, "CONFIG_TARGET_UNTRUSTED"))
        reader.assert_not_called()
        client.assert_not_called()

    def test_header_injection_or_oversized_credentials_never_construct_client(self):
        for token in (SYNTHETIC_TOKEN + "\r\nX-Evil: yes", SYNTHETIC_TOKEN + "\x00",
                      SYNTHETIC_TOKEN + "\x7f", SYNTHETIC_TOKEN + "\u00e9", "A" * 513):
            with self.subTest(token_type=repr(token[-10:])), \
                    mock.patch.object(probe, "_read_private_token", return_value=token), \
                    mock.patch.object(probe, "RemoteBlotter") as client:
                # A NUL cannot be placed in an OS environment. Exercise all
                # untrusted file-returned values at the same public boundary.
                code, result, emitted = invoke(
                    "http://127.0.0.1:1", extra=("--token-file", PRIVATE_SENTINEL))
            self.assertEqual((code, result["status"]), (2, "CONFIG_MISSING"))
            client.assert_not_called()
            self.assertNotIn(SYNTHETIC_TOKEN, emitted)

    def test_bad_file_never_falls_back_to_valid_environment_credential(self):
        for failure, expected in ((PermissionError(PRIVATE_SENTINEL), "CONFIG_PERMISSION_DENIED"),
                                  (FileNotFoundError(PRIVATE_SENTINEL), "CONFIG_MISSING"),
                                  (UnicodeError(PRIVATE_SENTINEL), "CONFIG_MISSING")):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(probe, "_read_private_token", side_effect=failure), \
                    mock.patch.object(probe, "RemoteBlotter") as client:
                code, result, emitted = invoke(
                    "http://127.0.0.1:1", extra=("--token-file", PRIVATE_SENTINEL))
            self.assertEqual((code, result["status"]), (2, expected))
            client.assert_not_called()
            self.assertNotIn(PRIVATE_SENTINEL, emitted)
            self.assertNotIn(SYNTHETIC_TOKEN, emitted)


@unittest.skipUnless(os.name == "nt", "requires native Windows DACL semantics")
class NativeWindowsProbeFileTests(unittest.TestCase):
    def test_real_verified_file_and_explicit_public_read_denial(self):
        # Synthetic fixture only. ACL changes never target a production path.
        script = r"""
$ErrorActionPreference = 'Stop'
$path = $env:BLOTTER_HOSTILE_FIXTURE
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$security = [System.Security.AccessControl.FileSecurity]::new()
$security.SetOwner($me)
$security.SetAccessRuleProtection($true, $false)
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$full = [System.Security.AccessControl.FileSystemRights]::FullControl
$security.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($me, $full, $allow))
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$security.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($system, $full, $allow))
if ($env:BLOTTER_HOSTILE_PUBLIC -eq '1') {
    $everyone = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
    $read = [System.Security.AccessControl.FileSystemRights]::Read
    $security.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($everyone, $read, $allow))
}
[IO.File]::SetAccessControl($path, $security)
"""
        from agent_blotter.mcp_stdio import _trusted_windows_powershell
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.token"
            path.write_text(SYNTHETIC_TOKEN, encoding="utf-8")
            for public in ("0", "1"):
                fixture = subprocess.run(
                    [_trusted_windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
                    env=dict(os.environ, BLOTTER_HOSTILE_FIXTURE=str(path), BLOTTER_HOSTILE_PUBLIC=public),
                    capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(fixture.returncode, 0, "synthetic ACL fixture failed")
                if public == "0":
                    self.assertEqual(probe._read_private_token(path), SYNTHETIC_TOKEN)
                else:
                    with mock.patch.object(probe, "RemoteBlotter") as client:
                        code, result, emitted = invoke(
                            "http://127.0.0.1:1", extra=("--token-file", str(path)))
                    self.assertEqual((code, result["status"]), (2, "CONFIG_PERMISSION_DENIED"))
                    self.assertNotIn(SYNTHETIC_TOKEN, emitted)
                    self.assertNotIn(str(path), emitted)
                    client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
