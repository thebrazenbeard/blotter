"""Default-deny tests: a shared bearer cannot open provisional private HTTP routes."""
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from pathlib import Path

from agent_blotter.channels import ChannelCatalog
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server
from agent_blotter.store import Blotter, BlotterError


class PrivatePrincipalGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.main = Blotter(root / "main.sqlite3")
        self.catalog = ChannelCatalog(root / "channels")
        self.catalog.provision("vera", owner_actor="vera.bound")
        self.server = create_server(
            self.main, {"A" * 40: "vera.bound"}, port=0, channels=self.catalog
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop)
        self.client = RemoteBlotter(
            f"http://127.0.0.1:{self.server.server_port}",
            actor="vera.bound", token="A" * 40,
        )

    def test_spoofed_principal_header_cannot_override_default_denial(self):
        url = f"http://127.0.0.1:{self.server.server_port}"
        request = urllib.request.Request(
            url + "/v1/channels/vera/tail",
            headers={
                "Authorization": "Bearer " + "A" * 40,
                "X-Blotter-Actor": "vera.bound",
                "X-Verified-Principal": "vera.bound",
                "X-Issuer": "claimed-trusted-host",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()
        self.assertEqual(self.main.tail(), [])

    def test_verifier_exception_denies_instead_of_opening_channel(self):
        failing = create_server(
            self.main, {"A" * 40: "vera.bound"}, port=0,
            channels=self.catalog,
            private_principal_verifier=lambda *_args: 1 / 0,
        )
        worker = threading.Thread(
            target=failing.serve_forever,
            kwargs={"poll_interval": 0.01}, daemon=True,
        )
        worker.start()
        try:
            client = RemoteBlotter(
                f"http://127.0.0.1:{failing.server_port}",
                actor="vera.bound", token="A" * 40,
            )
            with self.assertRaisesRegex(BlotterError, "404"):
                client.channel("vera").tail()
            self.assertEqual(client.channels(), [])
        finally:
            failing.shutdown()
            failing.server_close()
            worker.join(timeout=3)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_private_http_deny_by_default_even_for_credential_owner(self):
        for operation in (
            lambda: self.client.channels(),
            lambda: self.client.channel("vera").tail(),
            lambda: self.client.channel("vera").list(),
            lambda: self.client.channel("vera").record(
                kind="fixture.synthetic", message="not admitted", event_id="deny-1"),
            lambda: self.client.channel("vera").check(session="deny"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(BlotterError, "404"):
                    operation()
        self.assertEqual(self.catalog.ledger("vera", actor="vera.bound").tail(), [])
        self.assertEqual(self.main.tail(), [])
        self.assertEqual(
            self.client.record(kind="fixture.main", message="main still works")["seq"], 1
        )


if __name__ == "__main__":
    unittest.main()