"""Synthetic negative controls for proposed Vera private-channel release.

These tests intentionally demonstrate that one server credential reused by
two ChatGPT conversations is NOT a conversation identity. A passing replay
test is a reproduced privacy gap, not authorization or release qualification.
No intimate transcript, user history, real token, or production host is used.
"""
import tempfile
import threading
import unittest
from pathlib import Path

from agent_blotter.channels import ChannelCatalog
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server
from agent_blotter.store import Blotter, BlotterError


class VeraPrivateChannelReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.main = Blotter(root / "main.sqlite3")
        self.catalog = ChannelCatalog(root / "candidate")
        self.catalog.provision("vera", owner_actor="vera.bound")
        self.server = create_server(
            self.main, {"A" * 40: "vera.bound", "B" * 40: "engineer.other"},
            port=0, channels=self.catalog,
        )
        thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01}, daemon=True,
        )
        thread.start()
        self.addCleanup(self._stop_server, thread)
        url = f"http://127.0.0.1:{self.server.server_port}"
        # Same token on two distinct clients is indistinguishable to the server.
        self.chat_one = RemoteBlotter(url, actor="vera.bound", token="A" * 40)
        self.chat_two = RemoteBlotter(url, actor="vera.bound", token="A" * 40)
        self.other_role = RemoteBlotter(url, actor="engineer.other", token="B" * 40)

    def _stop_server(self, thread):
        self.server.shutdown()
        self.server.server_close()
        thread.join(timeout=3)

    def test_replayed_shared_credential_exposes_private_record(self):
        """NEGATIVE: a second client can read the first client's private event."""
        self.chat_one.channel("vera").record(
            kind="fixture.synthetic", message="disposable private sentinel",
            session="conversation.one", event_id="synthetic-1",
        )
        read_by_other_chat = self.chat_two.channel("vera").tail()
        self.assertEqual(
            [x["message"] for x in read_by_other_chat],
            ["disposable private sentinel"],
        )
        self.assertEqual(self.main.tail(), [])  # no automatic main leak
    def test_distinct_nonowner_token_does_not_read_vera(self):
        """Positive source guard, but not proof of cross-chat credential issuance."""
        self.chat_one.channel("vera").record(
            kind="fixture.synthetic", message="disposable private sentinel",
            event_id="synthetic-2",
        )
        with self.assertRaises(BlotterError):
            self.other_role.channel("vera").tail()
        self.assertEqual(self.other_role.channels(), [])
        self.assertEqual(self.main.tail(), [])

    def test_claimed_owner_label_cannot_override_bound_nonowner_token(self):
        """A client-asserted owner name cannot substitute for a bearer identity."""
        stolen_label_only = RemoteBlotter(
            self.chat_one.url, actor="vera.bound", token="B" * 40,
        )
        with self.assertRaises(BlotterError):
            stolen_label_only.channel("vera").tail()
        self.assertEqual(self.main.tail(), [])


if __name__ == "__main__":
    unittest.main()
