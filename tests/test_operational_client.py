"""Real process/HTTP/persistence qualification, with no production inputs."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ops.verify_operational_client import qualify


class OperationalClientTests(unittest.TestCase):
    def test_existing_clients_survive_cold_restart_without_duplicate_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            untouched = folder / "must-not-open.sqlite3"
            inherited = "inherited-synthetic-bearer-must-not-be-used-" + "x" * 32
            with mock.patch.dict(os.environ, {
                "BLOTTER_URL": "https://production.invalid/do-not-contact",
                "BLOTTER_TOKEN": inherited, "BLOTTER_DB": str(untouched),
            }):
                report = qualify(folder / "evidence")
            self.assertEqual(report["status"], "PASS", report)
            checks = {check["name"]: check for check in report["checks"]}
            self.assertTrue(all(check["passed"] for check in checks.values()), report)
            self.assertEqual(checks["unauthenticated_get_events"]["http_status"], 401)
            self.assertEqual(checks["bad_bearer_post_rejected"]["http_status"], 403)
            self.assertEqual(checks["wrong_actor_post_rejected"]["http_status"], 403)
            self.assertEqual(checks["changed_body_same_id_rejected"]["http_status"], 422)
            self.assertEqual(checks["cold_restart_exact_http_readback"]["readback_count"], 2)
            self.assertEqual(checks["sdk_same_id_retry_after_restart"]["readback_count"], 2)
            self.assertEqual(checks["probe_same_id_retry_after_restart"]["readback_count"], 2)
            self.assertEqual(checks["post_restart_hash_chain_unchanged"]["count"], 2)
            self.assertEqual(checks["post_restart_hash_chain_unchanged"]["sqlite_quick_check"], "ok")
            self.assertTrue(checks["first_process_controlled_stop"]["controlled_stop"])
            self.assertTrue(checks["restarted_process_controlled_stop"]["controlled_stop"])
            self.assertFalse(untouched.exists())
            evidence = Path(report["evidence_directory"])
            self.assertTrue((evidence / "synthetic.sqlite3").is_file())
            persisted = json.loads((evidence / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted, report)
            self.assertNotIn(inherited, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
