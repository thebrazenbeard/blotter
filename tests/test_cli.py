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
            path = Path(tmp) / "blotter.sqlite3"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

            def run(*args):
                return subprocess.run([sys.executable, "-m", "agent_blotter", "--db", str(path), *args],
                                      env=env, capture_output=True, text=True)

            self.assertEqual(run("init").returncode, 0)
            initial = run("check", "--actor", "one", "--session", "turn-1")
            self.assertEqual(initial.returncode, 0, initial.stderr)
            self.assertEqual(json.loads(initial.stdout)["events"], [])
            first = run("record", "--actor", "one", "--session", "turn-1",
                        "--kind", "command.executed", "--message", "Ran tests",
                        "--payload", '{"exit_code":0}')
            self.assertEqual(first.returncode, 0, first.stderr)
            second = run("check", "--actor", "two", "--session", "turn-2")
            self.assertEqual(second.returncode, 0, second.stderr)
            seen = json.loads(second.stdout)["events"]
            self.assertEqual([e["kind"] for e in seen],
                             ["turn.checked", "command.executed"])
            self.assertEqual([e["seq"] for e in json.loads(run("list").stdout)],
                             [1, 2, 3])
            self.assertEqual(len(run("export").stdout.splitlines()), 3)
            self.assertEqual(json.loads(run("verify").stdout)["count"], 3)
            self.assertNotEqual(run("record", "--actor", "a", "--kind", "x",
                                    "--message", "m", "--payload", "badjson").returncode, 0)


if __name__ == "__main__":
    unittest.main()
