"""No-live-credential isolation probe for the Vera Works channel candidate."""
import tempfile
import threading
import unittest
from pathlib import Path

from agent_blotter.channels import ChannelCatalog
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server
from agent_blotter.store import Blotter, BlotterError


class VeraWorksChannelProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.global_log = Blotter(root / "main.sqlite3")
        channels = ChannelCatalog(root / "channels")
        channels.provision("vera-works", owner_actor="vera-works",
                           purpose="Synthetic private economic log")
        self.server = create_server(
            self.global_log, {"A" * 40: "vera-works", "B" * 40: "other.agent"},
            port=0, channels=channels)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        url = "http://127.0.0.1:" + str(self.server.server_port)
        self.owner = RemoteBlotter(url, actor="vera-works", token="A" * 40)
        self.other = RemoteBlotter(url, actor="other.agent", token="B" * 40)
        self.same_owner_second_chat = RemoteBlotter(
            url, actor="vera-works", token="A" * 40)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_channel_is_separate_and_private_to_different_principal(self):
        own = self.owner.channel("vera-works")
        own.record(kind="financial.evidence", message="synthetic confidential",
                   event_id="probe-private-1", session="owner")
        self.assertEqual(self.other.channels(), [])
        self.assertEqual([e["message"] for e in own.tail()],
                         ["synthetic confidential"])
        with self.assertRaises(BlotterError):
            self.other.channel("vera-works").tail()
        with self.assertRaises(BlotterError):
            self.other.channel("vera-works").record(
                kind="forged", message="wrong owner",
                event_id="probe-other-forge", session="unauthorized")
        self.assertEqual(self.owner.tail(), [])
        self.assertEqual(self.other.tail(), [])
        main = self.owner.record(kind="status", message="main-safe pointer",
                                 event_id="probe-global-1", session="main")
        self.assertEqual(main["seq"], 1)
        self.assertEqual([e["message"] for e in self.other.tail()],
                         ["main-safe pointer"])

    def test_second_chat_sharing_the_owner_token_can_read(self):
        # A token authenticates an ACTOR, not an individual ChatGPT chat.
        self.owner.channel("vera-works").record(
            kind="confidential", message="same principal can read",
            event_id="probe-private-2", session="chat-a")
        self.assertEqual([e["message"] for e in
                          self.same_owner_second_chat.channel("vera-works").tail()],
                         ["same principal can read"])
        # This confirms a release gate: the existing shared ChatGPT credential
        # MUST NOT be used as the Vera Works-private owner identity.

if __name__ == "__main__":
    unittest.main()
