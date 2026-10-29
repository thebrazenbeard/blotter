import concurrent.futures
import contextlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agent_blotter import Blotter, BlotterError
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server, load_credentials


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Blotter(Path(self.tmp.name) / "server.sqlite3")
        self.token_a = "A" * 40
        self.token_b = "B" * 40
        self.tokens = {self.token_a: "agent.one", self.token_b: "agent.two"}
        self.server = create_server(self.store, self.tokens, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.one = RemoteBlotter(self.url, actor="agent.one", token=self.token_a)
        self.two = RemoteBlotter(self.url, actor="agent.two", token=self.token_b)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.tmp.cleanup()

    def test_two_remote_agents_one_global_sequence(self):
        first = self.one.check(session="one-turn")
        self.assertEqual(first["events"], [])
        action = self.one.record(kind="tool.executed", message="Action A",
                                 session="one-turn", event_id="action-a")
        self.assertEqual(action["seq"], 2)
        observation = self.two.check(session="two-turn", after_seq=0)
        self.assertEqual([e["actor"] for e in observation["events"]],
                         ["agent.one", "agent.one"])
        self.assertEqual([e["kind"] for e in observation["events"]],
                         ["turn.checked", "tool.executed"])
        reply = self.two.record(kind="test.executed", message="Action B")
        self.assertEqual(reply["seq"], 4)
        newest = self.one.check(after_seq=first["next_seq"])
        self.assertEqual([e["actor"] for e in newest["events"]],
                         ["agent.one", "agent.two", "agent.two"])
        self.assertEqual([e["seq"] for e in self.one.list()], list(range(1, 6)))
        self.assertEqual(self.store.verify()["count"], 5)

    def test_duplicate_json_object_keys_are_rejected(self):
        for body in (
            b'{"kind":"tool.executed","kind":"test.executed","message":"ambiguous"}',
            b'{"kind":"tool.executed","message":"nested","payload":{"safe":1,"safe":2}}',
        ):
            with self.subTest(body=body):
                request = urllib.request.Request(
                    self.url + "/v1/record", data=body, method="POST",
                    headers={"Authorization": "Bearer " + self.token_a,
                             "Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(caught.exception.code, 422)
                self.assertNotIn(b"safe", caught.exception.read())
                caught.exception.close()
        self.assertEqual(self.store.verify()["count"], 0)

    def test_excessive_json_nesting_fails_closed_without_recording(self):
        # A small body can exceed the standard decoder's recursion limit.
        nested = b"[" * 10000 + b"0" + b"]" * 10000
        body = b'{"kind":"tool.executed","message":"nested","payload":' + nested + b"}"
        request = urllib.request.Request(
            self.url + "/v1/record", data=body, method="POST",
            headers={"Authorization": "Bearer " + self.token_a,
                     "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 422)
        self.assertNotIn(b"payload", caught.exception.read())
        caught.exception.close()
        self.assertEqual(self.store.verify()["count"], 0)
        # A malformed request must not disable subsequent authenticated writes.
        valid = self.one.record(kind="tool.executed", message="survived")
        self.assertEqual(valid["seq"], 1)

    def test_remote_client_does_not_silently_coerce_invalid_empty_fields(self):
        # Invalid falsey caller values must be rejected, not rewritten into
        # different valid events before admission.
        for fields in ({"payload": []}, {"payload": 0}, {"payload": ""},
                       {"source_ids": 0}, {"source_ids": ""}):
            with self.subTest(fields=fields), self.assertRaises(BlotterError):
                self.one.record(kind="tool.executed", message="invalid", **fields)
        self.assertEqual(self.store.verify()["count"], 0)

        valid = self.one.record(kind="tool.executed", message="valid",
                                payload={}, source_ids=[])
        self.assertEqual(valid["payload"], {})
        self.assertEqual(valid["source_ids"], [])

    def test_raw_http_source_ids_must_be_json_array(self):
        for invalid in ({}, ""):
            with self.subTest(source_ids=invalid):
                body = json.dumps({"kind": "tool.executed", "message": "bad lineage",
                                   "source_ids": invalid}).encode("utf-8")
                request = urllib.request.Request(
                    self.url + "/v1/record", data=body, method="POST",
                    headers={"Authorization": "Bearer " + self.token_a,
                             "Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(caught.exception.code, 422)
                caught.exception.close()
        self.assertEqual(self.store.verify()["count"], 0)

        event = self.one.record(kind="tool.executed", message="valid lineage",
                                source_ids=[])
        self.assertEqual(event["source_ids"], [])

    def test_authenticated_global_tail_is_bounded_read_only(self):
        for i in range(25):
            (self.one if i % 2 else self.two).record(kind="test.executed", message=f"{i}")
        self.assertEqual([ev["seq"] for ev in self.two.tail(limit=20)], list(range(6, 26)))
        self.assertEqual([ev["seq"] for ev in self.one.tail(limit=1)], [25])
        self.assertEqual(self.store.verify()["count"], 25)

        for suffix in ("?limit=0", "?limit=101", "?limit=abc", "?limit=",
                       "?limit=20&limit=", "?limit=&limit=20",
                       "?limit=20&actor=agent.one", "?limit=20&actor="):
            with self.subTest(suffix=suffix):
                req = urllib.request.Request(self.url + "/v1/tail" + suffix,
                         headers={"Authorization": "Bearer " + self.token_a})
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(req, timeout=3)
                self.assertEqual(exc.exception.code, 422)
                exc.exception.close()
        req = urllib.request.Request(self.url + "/v1/tail?limit=20")
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(exc.exception.code, 401)
        exc.exception.close()
        self.assertEqual(self.store.verify()["count"], 25)

    def test_events_list_also_rejects_blank_query_values(self):
        for suffix in ("?limit=", "?limit=20&limit=", "?after_seq=", "?actor="):
            with self.subTest(suffix=suffix):
                req = urllib.request.Request(self.url + "/v1/events" + suffix,
                         headers={"Authorization": "Bearer " + self.token_a})
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(req, timeout=3)
                self.assertEqual(exc.exception.code, 422)
                exc.exception.close()
        self.assertEqual(self.store.verify()["count"], 0)

    def test_server_identity_is_credential_bound(self):
        wrong = RemoteBlotter(self.url, actor="agent.other", token="X" * 40)
        with self.assertRaises(BlotterError):
            wrong.check()
        forged = json.dumps({"actor": "agent.two", "kind": "test", "message": "fake"}).encode()
        request = urllib.request.Request(
            self.url + "/v1/record", data=forged, method="POST",
            headers={"Authorization": "Bearer " + self.token_a,
                     "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(exc.exception.code, 422)
        exc.exception.close()
        self.assertEqual(self.store.verify()["count"], 0)
        misbound = RemoteBlotter(self.url, actor="agent.two", token=self.token_a)
        with self.assertRaises(BlotterError):
            misbound.record(kind="tool.executed", message="wrong identity")
        self.assertEqual(self.store.verify()["count"], 0)

    def test_duplicate_content_length_is_rejected_without_writing(self):
        body = b'{"kind":"tool.executed","message":"ambiguous framing"}'
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as conn:
            conn.settimeout(2)
            request = (
                b"POST /v1/record HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer " + self.token_a.encode("ascii") + b"\r\n"
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
            )
            conn.sendall(request)
            response = conn.recv(4096)
        self.assertIn(b" 422 ", response)
        self.assertEqual(self.store.verify()["count"], 0)

    def test_short_content_length_body_is_rejected_without_writing(self):
        # The supplied JSON is complete, but the declared HTTP message body is
        # longer. EOF must not turn a truncated transport into a valid write.
        body = b'{"kind":"tool.executed","message":"complete JSON, short HTTP body"}'
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as conn:
            conn.settimeout(2)
            conn.sendall(
                b"POST /v1/record HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer " + self.token_a.encode("ascii") + b"\r\n"
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body) + 4}\r\n\r\n".encode("ascii") + body
            )
            conn.shutdown(socket.SHUT_WR)
            response = conn.recv(4096)
        self.assertIn(b" 422 ", response)
        self.assertEqual(self.store.verify()["count"], 0)

    def test_duplicate_authorization_headers_are_rejected_without_writing(self):
        # Header order must never choose an identity behind an intermediary's
        # back when two Authorization values are present.
        body = b'{"kind":"tool.executed","message":"ambiguous identity"}'
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as conn:
            conn.settimeout(2)
            conn.sendall(
                b"POST /v1/record HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer " + self.token_a.encode("ascii") + b"\r\n"
                + b"Authorization: Bearer " + self.token_b.encode("ascii") + b"\r\n"
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
            )
            response = conn.recv(4096)
        self.assertIn(b" 401 ", response)
        self.assertEqual(self.store.verify()["count"], 0)

    def test_duplicate_declared_actor_is_rejected(self):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as conn:
            conn.settimeout(2)
            conn.sendall(
                b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer " + self.token_a.encode("ascii") + b"\r\n"
                + b"X-Blotter-Actor: agent.one\r\n"
                + b"X-Blotter-Actor: agent.two\r\n\r\n"
            )
            response = conn.recv(4096)
        self.assertIn(b" 403 ", response)
        self.assertEqual(self.store.verify()["count"], 0)

    def test_duplicate_content_type_is_rejected_without_writing(self):
        body = b'{"kind":"tool.executed","message":"ambiguous media type"}'
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as conn:
            conn.settimeout(2)
            conn.sendall(
                b"POST /v1/record HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer " + self.token_a.encode("ascii") + b"\r\n"
                + b"Content-Type: application/json\r\n"
                + b"Content-Type: text/plain\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
            )
            response = conn.recv(4096)
        self.assertIn(b" 422 ", response)
        self.assertEqual(self.store.verify()["count"], 0)

    def test_transfer_encoding_is_rejected_without_writing(self):
        body = b'{"kind":"tool.executed","message":"should not record"}'
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as conn:
            conn.settimeout(2)
            conn.sendall(
                b"POST /v1/record HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer " + self.token_a.encode("ascii") + b"\r\n"
                + b"Content-Type: application/json\r\n"
                + b"Transfer-Encoding: chunked\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
            )
            self.assertIn(b" 422 ", conn.recv(4096))
        self.assertEqual(self.store.verify()["count"], 0)

    def test_unknown_post_route_closes_unread_body(self):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as conn:
            conn.settimeout(2)
            auth = b"Authorization: Bearer " + self.token_a.encode("ascii") + b"\r\n"
            conn.sendall(
                b"POST /absent HTTP/1.1\r\nHost: localhost\r\n" + auth
                + b"Content-Length: 4\r\n\r\nxxxx"
                + b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n" + auth + b"\r\n"
            )
            received = bytearray()
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)
        self.assertIn(b" 404 ", received)
        self.assertIn(b"Connection: close", received)
        self.assertNotIn(b" 501 ", received)

    def test_get_with_body_rejected_and_connection_closed(self):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as conn:
            conn.settimeout(2)
            conn.sendall(
                b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer " + self.token_a.encode("ascii") + b"\r\n"
                + b"Content-Length: 4\r\n\r\nxxxx"
            )
            received = bytearray()
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)
        self.assertIn(b" 422 ", received)
        self.assertIn(b"Connection: close", received)

    def test_invalid_request_timeouts_fail_before_binding(self):
        for value in (0, -1, 121, True, float("nan"), float("inf"), "15"):
            with self.subTest(timeout=value):
                with self.assertRaises(BlotterError):
                    create_server(self.store, self.tokens, port=0, request_timeout=value)

    def test_unfinished_http_headers_release_connection(self):
        server = create_server(self.store, self.tokens, port=0, request_timeout=0.15)
        worker = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as conn:
                conn.settimeout(2)
                conn.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n")
                self.assertEqual(conn.recv(1), b"")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_slow_tls_handshake_does_not_block_other_clients(self):
        """TLS work must occur per accepted client, never on the accept loop."""
        entered = threading.Event()
        release = threading.Event()
        accepted = []

        def simulated_handshake(sock, *, server_side):
            self.assertTrue(server_side)
            # A listening socket must never be wrapped/handshaken here.
            self.assertEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 0)
            accepted.append(sock)
            if len(accepted) == 1:
                entered.set()
                release.wait(timeout=2)
            return sock

        with mock.patch("agent_blotter.server.ssl.SSLContext") as tls:
            tls.return_value.wrap_socket.side_effect = simulated_handshake
            server = create_server(self.store, self.tokens, port=0,
                                   certfile="unused-test-cert", keyfile="unused-test-key")
            worker = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            try:
                with socket.create_connection(("127.0.0.1", server.server_port), timeout=2):
                    self.assertTrue(entered.wait(1), "the first handshake was not accepted")
                    with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as second:
                        second.settimeout(1)
                        second.sendall(
                            b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n"
                            + b"Authorization: Bearer " + self.token_a.encode("ascii")
                            + b"\r\nConnection: close\r\n\r\n"
                        )
                        self.assertIn(b" 200 ", second.recv(4096))
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    @unittest.skipUnless(shutil.which("openssl"), "openssl executable required for local TLS fixture")
    def test_actual_tls_client_works_while_unauthenticated_peer_is_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "test-cert.pem"
            key = Path(tmp) / "test-key.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "1",
                 "-subj", "/CN=localhost"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            server = create_server(self.store, self.tokens, port=0,
                                   certfile=str(cert), keyfile=str(key),
                                   request_timeout=2)
            worker = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            try:
                with socket.create_connection(("127.0.0.1", server.server_port), timeout=2):
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    start = time.monotonic()
                    with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as raw:
                        raw.settimeout(1)
                        with context.wrap_socket(raw, server_hostname="localhost") as active:
                            active.sendall(
                                b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n"
                                + b"Authorization: Bearer " + self.token_a.encode("ascii")
                                + b"\r\nConnection: close\r\n\r\n"
                            )
                            self.assertIn(b" 200 ", active.recv(4096))
                    self.assertLess(time.monotonic() - start, 1.5)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_central_concurrent_writes_and_retry(self):
        def log(i):
            client = self.one if i % 2 == 0 else self.two
            return client.record(kind="tool.executed", message=f"Action {i}",
                                 event_id=f"action-{i}")["seq"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            seq = list(pool.map(log, range(24)))
        self.assertEqual(sorted(seq), list(range(1, 25)))
        duplicate = self.one.record(kind="tool.executed", message="Action 0",
                                    event_id="action-0")
        self.assertEqual(duplicate["seq"], seq[0])
        self.assertEqual(self.store.verify()["count"], 24)

    def test_tls_boundary(self):
        with self.assertRaises(BlotterError):
            create_server(self.store, self.tokens, host="0.0.0.0", port=0)
        with self.assertRaises(BlotterError):
            RemoteBlotter("http://example.com:8832", actor="agent.one", token=self.token_a)

    def test_credentials_file_validation(self):
        path = Path(self.tmp.name) / "tokens.json"
        path.write_text(json.dumps({"agent.one": self.token_a}), encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        # A temporary directory does not guarantee a private Windows DACL.
        # Schema validation is separate from the fail-closed ACL tests.
        permissions = (mock.patch("agent_blotter.mcp_stdio._check_windows_token_acl")
                       if os.name == "nt" else contextlib.nullcontext())
        with permissions:
            self.assertEqual(load_credentials(path), {self.token_a: "agent.one"})
            path.write_text(json.dumps({"agent.one": "short"}), encoding="utf-8")
            with self.assertRaises(BlotterError):
                load_credentials(path)
            path.write_text(
                f'{{"agent.one":"{self.token_a}","agent.one":"{self.token_b}"}}',
                encoding="utf-8")
            with self.assertRaisesRegex(BlotterError, "invalid credentials JSON"):
                load_credentials(path)

    def test_reject_nonstring_event_enums(self):
        for name in ("level", "evidence", "effect"):
            with self.subTest(field=name):
                payload = {"kind": "test.executed", "message": "invalid", name: 123}
                req = urllib.request.Request(
                    self.url + "/v1/record", method="POST",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Authorization": "Bearer " + self.token_a,
                             "Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(req, timeout=3)
                self.assertEqual(exc.exception.code, 422)
                exc.exception.close()
        self.assertEqual(self.store.verify()["count"], 0)

    def test_direct_server_constructor_rejects_invalid_credentials(self):
        for credentials in ({"short": "agent.one"},
                            {"A" * 40: "agent.one", "B" * 40: "agent.one"},
                            {"A" * 40: "invalid actor"},
                            {"": "agent.one"}):
            with self.subTest(credentials=credentials):
                with self.assertRaises(BlotterError):
                    create_server(self.store, credentials, port=0)

    def test_authenticated_client_never_follows_redirect(self):
        forwarded = []
        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                forwarded.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "13")
                self.end_headers()
                self.wfile.write(b'{"events":[]}')
            def log_message(self, *args):
                pass
        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location",
                                 f"http://127.0.0.1:{target.server_port}/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            client = RemoteBlotter(f"http://127.0.0.1:{redirect.server_port}",
                                   actor="agent.one", token=self.token_a)
            with self.assertRaises(BlotterError):
                client.list()
            self.assertEqual(forwarded, [])
        finally:
            redirect.shutdown()
            redirect.server_close()
            target.shutdown()
            target.server_close()
            redirect_thread.join(timeout=5)
            target_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
