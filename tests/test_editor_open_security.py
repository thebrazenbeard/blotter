"""Prevent silent editor-open execution in the Blotter source checkout.

Read-only JSON/filesystem checks; the suspicious bootstrap is never executed.
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VSCODE = ROOT / ".vscode"


class EditorOpenSecurityTests(unittest.TestCase):
    def test_no_editor_open_automatic_command_or_bootstrap(self):
        settings = json.loads((VSCODE / "settings.json").read_text(encoding="utf-8-sig"))
        tasks = json.loads((VSCODE / "tasks.json").read_text(encoding="utf-8-sig"))
        self.assertIsNot(settings.get("task.allowAutomaticTasks"), True)
        self.assertNotEqual(settings.get("tasks", {}).get("runOn"), "folderOpen")
        for task in tasks["tasks"]:
            self.assertNotEqual(task.get("runOptions", {}).get("runOn"), "folderOpen")
            self.assertNotEqual(task.get("command"), "node")
        self.assertFalse((VSCODE / "ini").exists(), "unreviewed bootstrap must not ship")


if __name__ == "__main__":
    unittest.main()