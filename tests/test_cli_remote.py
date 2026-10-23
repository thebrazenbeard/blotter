import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agent_blotter import Blotter
from agent_blotter.server import create_server


class RemoteCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Blotter(Path(self.tmp.name) / "central.sqlite3")
        self.token_a = "A" * 40
        self.token_b = "B" * 40
        self.server = create_server(
            self.db, {self.token_a: "agent.one", self.token_b: "agent.two"}, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def run_cli(self, token, *args):
        env = dict(os.environ)
        env["BLOTTER_URL"] = self.url
        env["BLOTTER_TOKEN"] = token
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return subprocess.run(
            [sys.executable, "-m", "agent_blotter", *args],
            env=env, capture_output=True, text=True, timeout=12)

    def test_remote_cli_two_agents_one_log(self):
        first = self.run_cli(self.token_a, "check", "--actor", "agent.one",
                             "--session", "one", "--after-seq", "0")
        self.assertEqual(first.returncode, 0, first.stderr)
        log = self.run_cli(self.token_a, "record", "--actor", "agent.one",
                           "--kind", "tool.executed", "--message", "Action one")
        self.assertEqual(log.returncode, 0, log.stderr)
        second = self.run_cli(self.token_b, "check", "--actor", "agent.two",
                              "--session", "two")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual([v["actor"] for v in json.loads(second.stdout)["events"]],
                         ["agent.one", "agent.one"])
        reading = self.run_cli(self.token_b, "--identity", "agent.two", "list")
        self.assertEqual(reading.returncode, 0, reading.stderr)
        self.assertEqual(len(json.loads(reading.stdout)), 3)
        exported = self.run_cli(self.token_b, "--identity", "agent.two", "export")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(len(exported.stdout.splitlines()), 3)
        self.assertEqual(self.db.verify()["count"], 3)

    def test_remote_identity_mismatch_fails(self):
        mismatch = self.run_cli(self.token_a, "record", "--actor", "agent.two",
                                "--kind", "tool.executed", "--message", "fake")
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertEqual(self.db.verify()["count"], 0)


if __name__ == "__main__":
    unittest.main()
