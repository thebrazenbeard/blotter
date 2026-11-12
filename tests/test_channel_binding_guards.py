"""Fail-closed channel ownership provisioning and server startup boundaries."""
import tempfile
import unittest
from pathlib import Path

from agent_blotter import Blotter, BlotterError
from agent_blotter.channels import ChannelCatalog
from agent_blotter.server import create_server


class ChannelBindingGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.catalog = ChannelCatalog(root / "channels")
        self.main = Blotter(root / "main.sqlite3")

    def test_one_bearer_actor_cannot_own_multiple_channels(self):
        self.catalog.provision("vera", owner_actor="shared.actor")
        with self.assertRaisesRegex(BlotterError, "already owns"):
            self.catalog.provision("aeonith", owner_actor="shared.actor")
        self.assertEqual([r["channel"] for r in
                          self.catalog.allowed(actor="shared.actor")], ["vera"])

    def test_server_rejects_orphaned_channel_owner(self):
        self.catalog.provision("vera", owner_actor="vera.private")
        with self.assertRaisesRegex(BlotterError, "credential"):
            create_server(self.main, {"A"*40: "some.other"},
                          port=0, channels=self.catalog)
        server = create_server(self.main, {"A"*40: "vera.private"},
                               port=0, channels=self.catalog)
        server.server_close()

    def test_reopen_rejects_legacy_duplicate_owner_mapping(self):
        self.catalog.provision("vera", owner_actor="shared.actor")
        # Simulate a pre-guard legacy database with ambiguous owner mapping.
        with self.catalog._connect() as db:
            db.execute("DROP INDEX channels_owner_unique")
            db.execute("INSERT INTO channels(id,owner_actor,purpose) "
                       "VALUES (?,?,?)", ("aeonith", "shared.actor", ""))
        with self.assertRaisesRegex(BlotterError, "ambiguous"):
            ChannelCatalog(self.catalog.root)


if __name__ == "__main__":
    unittest.main()
