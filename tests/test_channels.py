import tempfile
import unittest
from pathlib import Path

from agent_blotter.channels import ChannelCatalog
from agent_blotter.store import Blotter, BlotterError


class ChannelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.main = Blotter(root / "main.sqlite3")
        self.channels = ChannelCatalog(root / "private")

    def test_owner_provisioned_channel_is_not_a_main_filter(self):
        self.channels.provision("vera", owner_actor="vera",
                                purpose="Private Vera continuity")
        mine = self.channels.ledger("vera", actor="vera")
        e = mine.record(actor="vera", kind="note.saved",
                        message="private", event_id="private-1")
        self.assertEqual(e["seq"], 1)
        self.assertEqual(mine.verify()["count"], 1)
        self.assertEqual(self.main.list(), [])
        public = self.main.record(actor="other", kind="tool.used",
                                  message="public", event_id="public-1")
        self.assertEqual(public["seq"], 1)
        self.assertEqual([x["message"] for x in mine.tail()], ["private"])
        self.assertEqual([x["message"] for x in self.main.tail()], ["public"])

    def test_nonowner_cannot_list_or_access_channel(self):
        self.channels.provision("vera", owner_actor="vera")
        self.assertEqual(self.channels.allowed(actor="other"), [])
        with self.assertRaisesRegex(BlotterError, "channel unavailable"):
            self.channels.ledger("vera", actor="other")
        with self.assertRaisesRegex(BlotterError, "channel unavailable"):
            self.channels.ledger("nonexistent", actor="other")
        self.assertEqual([x["channel"] for x in self.channels.allowed(actor="vera")],
                         ["vera"])

    def test_immutable_owner_and_identity_safe_names(self):
        a = self.channels.provision("vera", owner_actor="vera", purpose="x")
        self.assertEqual(a, self.channels.provision(
            "vera", owner_actor="vera", purpose="x"))
        with self.assertRaises(BlotterError):
            self.channels.provision("vera", owner_actor="other", purpose="x")
        with self.assertRaises(BlotterError):
            self.channels.provision("vera", owner_actor="vera", purpose="y")
        for name in ("main", "../other", "chat/name", "other%2f", "", "Ä", "VERA",
                     "new-agent", "other", "unapproved-minotaur-copy"):
            with self.subTest(name=name), self.assertRaises(BlotterError):
                self.channels.provision(name, owner_actor="vera")

    def test_only_user_approved_channels_can_be_provisioned(self):
        for channel in ("vera", "aeonith", "vera-works", "igor", "minotaur"):
            with self.subTest(channel=channel):
                self.assertEqual(self.channels.provision(
                    channel, owner_actor="actor." + channel)["channel"], channel)
        self.assertEqual(
            sorted(x["channel"] for x in self.channels.allowed(actor="actor.igor")),
            ["igor"])
        for channel in ("igor-dev", "vera-work", "new-chat", "other-team"):
            with self.subTest(reject=channel), self.assertRaises(BlotterError):
                self.channels.provision(channel, owner_actor="actor." + channel)

    def test_minotaur_is_owner_authorized_but_not_main_or_other_actors(self):
        authorized = self.channels.provision(
            "minotaur", owner_actor="bt2.minotaur",
            purpose="Minotaur engineering activity and evaluator receipts")
        self.assertEqual(authorized["channel"], "minotaur")
        private = self.channels.ledger("minotaur", actor="bt2.minotaur")
        saved = private.record(actor="bt2.minotaur",
                               kind="trial.observed", message="private-case-receipt",
                               event_id="minotaur-test-event-1")
        self.assertEqual(saved["seq"], 1)
        self.assertEqual(self.main.tail(), [])
        self.assertEqual([x["channel"] for x in
                          self.channels.allowed(actor="bt2.minotaur")],
                         ["minotaur"])
        self.assertEqual(self.channels.allowed(actor="vera"), [])
        for actor in ("vera", "bt2.one.lappy"):
            with self.subTest(actor=actor), self.assertRaisesRegex(
                    BlotterError, "channel unavailable"):
                self.channels.ledger("minotaur", actor=actor)
        self.assertEqual(private.verify()["count"], 1)

    def test_reopen_preserves_channel_separate_cursor(self):
        self.channels.provision("vera", owner_actor="vera")
        mine = self.channels.ledger("vera", actor="vera")
        first = mine.check(actor="vera", after_seq=0)
        mine.record(actor="vera", kind="note", message="new")
        again = ChannelCatalog(self.channels.root).ledger("vera", actor="vera")
        checked = again.check(actor="vera", after_seq=first["next_seq"],
                              expected_after_hash=first["next_hash"])
        self.assertEqual([e["kind"] for e in checked["events"]], ["note"])
        self.assertEqual(self.main.verify()["count"], 0)


if __name__ == "__main__":
    unittest.main()
