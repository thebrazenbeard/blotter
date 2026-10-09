import concurrent.futures
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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
        self.assertEqual(load_credentials(path), {self.token_a: "agent.one"})
        path.write_text(json.dumps({"agent.one": "short"}), encoding="utf-8")
        with self.assertRaises(BlotterError):
            load_credentials(path)


if __name__ == "__main__":
    unittest.main()
