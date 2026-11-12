"""In-process HTTP trial of first-class channel routes (no deployment)."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_blotter.channels import ChannelCatalog
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server
from agent_blotter.store import Blotter, BlotterError


class ChannelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.main = Blotter(root / "main.sqlite3")
        self.catalog = ChannelCatalog(root / "private")
        self.catalog.provision("vera", owner_actor="agent.one",
                               purpose="Vera private work and provenance")
        self.server = create_server(self.main, {"A" * 40: "agent.one",
                                                "B" * 40: "agent.two"},
                                    port=0, channels=self.catalog)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.one = RemoteBlotter(self.url, actor="agent.one", token="A" * 40)
        self.two = RemoteBlotter(self.url, actor="agent.two", token="B" * 40)
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_channel_is_first_class_and_hidden_from_global(self):
        self.assertEqual([c["channel"] for c in self.one.channels()], ["vera"])
        self.assertEqual(self.two.channels(), [])
        private = self.one.channel("vera")
        saved = private.record(kind="identity.audit", message="private-record",
                               session="first", event_id="private-note-1")
        self.assertEqual(saved["seq"], 1)
        self.assertEqual([x["message"] for x in private.tail()], ["private-record"])
        self.assertEqual(self.two.list(), [])
        global_record = self.one.record(kind="engineering.changed",
                                        message="global-record", event_id="main-note-1")
        self.assertEqual(global_record["seq"], 1)
        self.assertEqual([x["message"] for x in self.two.tail()], ["global-record"])
        self.assertEqual(self.main.verify()["count"], 1)
        self.assertEqual(self.catalog.ledger("vera", actor="agent.one")
                         .verify()["count"], 1)

    def test_nonowner_read_and_write_fail_closed(self):
        for route, method in [
            ("/v1/channels/vera/events", "GET"),
            ("/v1/channels/vera/tail", "GET"),
            ("/v1/channels/vera/check", "POST"),
            ("/v1/channels/vera/record", "POST"),
            ("/v1/channels/nonexistent/tail", "GET"),
        ]:
            with self.subTest(route=route):
                data = None
                if method == "POST":
                    data = (b'{"kind":"test","message":"should not write"}'
                            if route.endswith("/record") else b'{}')
                req = urllib.request.Request(
                    self.url + route, data=data, method=method,
                    headers={"Authorization": "Bearer " + "B" * 40,
                             "Content-Type": "application/json",
                             "X-Blotter-Actor": "agent.two"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=3)
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
        self.assertEqual(self.main.verify()["count"], 0)
        self.assertEqual(self.catalog.ledger("vera", actor="agent.one")
                         .verify()["count"], 0)

    def test_channel_hash_cursor_and_main_are_independent(self):
        channel = self.one.channel("vera")
        first = channel.check(session="scope", after_seq=0)
        channel.record(kind="test.note", message="second", session="scope",
                       event_id="trial")
        second = channel.check(session="scope", after_seq=first["next_seq"],
                               expected_after_hash=first["next_hash"])
        self.assertEqual([x["kind"] for x in second["events"]], ["test.note"])
        with self.assertRaises(BlotterError):
            channel.check(session="scope", after_seq=first["next_seq"],
                          expected_after_hash="0" * 64)
        self.assertEqual(self.main.verify()["count"], 0)
        self.assertEqual(self.catalog.ledger("vera", actor="agent.one")
                         .verify()["count"], 3)

    def test_minotaur_owner_and_nonowner_credentials_are_distinct(self):
        self.catalog.provision("minotaur", owner_actor="agent.two",
                               purpose="Minotaur test-only continuity")
        owner = self.two.channel("minotaur")
        initial = owner.check(session="minotaur-test", after_seq=0)
        owner.record(kind="evaluation.receipt", message="synthetic private",
                     event_id="minotaur-private-http-1")
        recovered = owner.check(session="minotaur-test",
                                after_seq=initial["next_seq"],
                                expected_after_hash=initial["next_hash"])
        self.assertEqual([row["kind"] for row in recovered["events"]],
                         ["evaluation.receipt"])
        self.assertEqual([row["message"] for row in owner.tail()
                          if row["kind"] == "evaluation.receipt"],
                         ["synthetic private"])
        self.assertEqual(self.one.channels()[0]["channel"], "vera")
        self.assertEqual(self.one.tail(), [])
        for path, method, data in (
            ("/v1/channels/minotaur/events", "GET", None),
            ("/v1/channels/minotaur/tail", "GET", None),
            ("/v1/channels/minotaur/check", "POST", b"{}"),
            ("/v1/channels/minotaur/record", "POST",
             b'{"kind":"test","message":"spoof"}'),
        ):
            with self.subTest(path=path):
                req = urllib.request.Request(
                    self.url + path, data=data, method=method,
                    headers={"Authorization": "Bearer " + "A" * 40,
                             "Content-Type": "application/json",
                             "X-Blotter-Actor": "agent.one"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=3)
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
                # Supplying a stolen principal *label* with the wrong token
                # is explicitly rejected before channel authorization.
                spoof = urllib.request.Request(
                    self.url + path, data=data, method=method,
                    headers={"Authorization": "Bearer " + "A" * 40,
                             "Content-Type": "application/json",
                             "X-Blotter-Actor": "agent.two"})
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(spoof, timeout=3)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()
        self.assertEqual(self.main.verify()["count"], 0)
        self.assertEqual(self.catalog.ledger("minotaur", actor="agent.two")
                         .verify()["count"], 3)

    def test_default_server_has_no_channel_routes(self):
        # Legacy main-only behavior remains unchanged if channels are disabled.
        server = create_server(Blotter(Path(self.tmp.name) / "legacy.sqlite3"),
                               {"C" * 40: "agent.three"}, port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            client = RemoteBlotter(url, actor="agent.three", token="C" * 40)
            self.assertEqual(client.record(kind="old", message="works")["seq"], 1)
            with self.assertRaises(BlotterError):
                client.channels()
            with self.assertRaises(BlotterError):
                client.channel("vera").tail()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_named_channel_invalid_segment_is_not_interpreted(self):
        with self.assertRaises(BlotterError):
            self.one.channel("../vera")
        self.assertEqual(self.one.channel("vera").tail(), [])


if __name__ == "__main__":
    unittest.main()
