"""End-to-end filtered read remains a read-only view of main."""
import tempfile
import threading
import unittest
from pathlib import Path

from agent_blotter import Blotter
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server


class SubstantiveHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Blotter(Path(self.temp.name) / "main.sqlite3")
        self.server = create_server(self.db, {"A" * 40: "viewer"}, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.client = RemoteBlotter(
            f"http://127.0.0.1:{self.server.server_port}",
            actor="viewer", token="A" * 40)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_remote_main_activity_view_excludes_checks_but_not_audit(self):
        original = self.db.record(actor="worker", kind="code.changed",
                                  message="one substantive action")
        for i in range(31):
            self.db.check(actor="viewer", session=f"catchup-{i}",
                          after_seq=0, limit=1)
        before = self.db.verify()
        self.assertEqual([x["kind"] for x in self.client.tail(limit=20)],
                         ["turn.checked"] * 20)
        events = self.client.tail_substantive(limit=20)
        self.assertEqual([e["id"] for e in events], [original["id"]])
        self.assertEqual(self.db.verify(), before)


if __name__ == "__main__":
    unittest.main()
