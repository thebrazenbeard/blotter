import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

class CLITests(unittest.TestCase):
    def test_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            def run(*args):
                return subprocess.run([sys.executable, "-m", "agent_blotter", "--db", str(path), *args],
                                      env=env, capture_output=True, text=True)
            self.assertEqual(run("init").returncode, 0)
            logged = run("record", "--actor", "agent.one", "--kind", "run.summary",
                         "--message", "Observed test execution", "--evidence", "OBSERVATION",
                         "--payload", '{"status":"passed"}')
            self.assertEqual(logged.returncode, 0, logged.stderr)
            record = json.loads(logged.stdout)
            promoted = run("promote", "--actor", "reviewer", "--message", "Useful observation",
                           "--source-id", record["id"])
            self.assertEqual(promoted.returncode, 0, promoted.stderr)
            self.assertEqual(len(json.loads(run("knowledge").stdout)), 1)
            self.assertEqual(len(json.loads(run("list").stdout)), 2)
            self.assertEqual(len(run("export").stdout.splitlines()), 2)
            self.assertEqual(json.loads(run("verify").stdout)["count"], 2)
            self.assertNotEqual(run("record", "--actor", "agent", "--kind", "x",
                                    "--message", "m", "--payload", "badjson").returncode, 0)
if __name__ == "__main__":
    unittest.main()
