"""Regression tests for actionable, read-only Blotter diagnostics."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
import urllib.error

from agent_blotter import Blotter, BlotterError
from agent_blotter.doctor import diagnose
from agent_blotter.mcp_stdio import _safe_tool_failure
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Blotter(Path(self.temp.name) / "ledger.sqlite3")
        self.token = "diagnostic-test-token-" + ("T" * 32)
        self.server = create_server(
            self.ledger, {self.token: "doctor.agent"}, port=0)
        self.worker = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = RemoteBlotter(
            self.url, actor="doctor.agent", token=self.token)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.temp.cleanup()

    def test_default_diagnosis_is_read_only(self):
        result = diagnose(self.client)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checks"]["authenticated_health"]["status"], "passed")
        self.assertEqual(result["checks"]["latest20_read"]["status"], "passed")
        self.assertEqual(result["checks"]["append_test"]["status"], "not_requested")
        self.assertEqual(result["writes_attempted"], 0)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_explicit_append_is_verified_by_read_after_write(self):
        result = diagnose(self.client, test_append=True)
        self.assertTrue(result["ok"], result)
        append = result["checks"]["append_test"]
        self.assertEqual(append["status"], "passed")
        self.assertTrue(append["event_id"].startswith("doctor-"))
        self.assertGreater(append["seq"], 0)
        self.assertEqual(result["writes_attempted"], 1)
        self.assertEqual(result["writes_confirmed"], 1)
        events = self.client.list(after_seq=append["seq"] - 1, limit=100)
        self.assertTrue(any(
            item.get("id") == append["event_id"] and item.get("seq") == append["seq"]
            for item in events
        ))
        self.assertEqual(self.ledger.verify()["count"], 1)

    def test_missing_tail_route_is_distinct_from_service_or_auth_failure(self):
        class OldServerClient:
            actor = self.client.actor
            url = self.client.url

            def health(inner_self):
                return self.client.health()

            def tail(inner_self, *, limit=20):
                raise BlotterError("server rejected request (404): route not found")

        result = diagnose(OldServerClient())
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["authenticated_health"]["status"], "passed")
        self.assertEqual(result["checks"]["latest20_read"]["code"], "TAIL_ENDPOINT_MISSING")
        self.assertEqual(result["writes_attempted"], 0)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_authentication_failure_is_reported_without_secret(self):
        bad = RemoteBlotter(
            self.url, actor="doctor.agent", token="wrong-token-" + ("X" * 32))
        result = diagnose(bad)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["checks"]["authenticated_health"]["code"], "AUTHENTICATION_FAILED")
        rendered = json.dumps(result)
        self.assertNotIn("wrong-token", rendered)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_read_transport_failure_does_not_claim_write_outcome_unknown(self):
        with mock.patch(
            "agent_blotter.remote._NO_REDIRECT_OPENER.open",
            side_effect=urllib.error.URLError("private detail"),
        ):
            with self.assertRaises(BlotterError) as caught:
                self.client.health()
        self.assertIn("no read result was verified", str(caught.exception))
        self.assertNotIn("write outcome may be unknown", str(caught.exception))
        self.assertNotIn("private detail", str(caught.exception))

    def test_write_transport_failure_requires_same_event_id_reconciliation(self):
        with mock.patch(
            "agent_blotter.remote._NO_REDIRECT_OPENER.open",
            side_effect=urllib.error.URLError("private detail"),
        ):
            with self.assertRaises(BlotterError) as caught:
                self.client.record(
                    kind="diagnostic.probe", message="probe", event_id="doctor-test-event")
        self.assertIn("write outcome may be unknown", str(caught.exception))
        self.assertIn("event_id", str(caught.exception))
        self.assertNotIn("private detail", str(caught.exception))

    def test_mcp_failures_are_specific_but_never_echo_server_text(self):
        secret = "do-not-show-this-secret"
        missing_tail = _safe_tool_failure(
            BlotterError(f"server rejected request (404): {secret}"), "latest20")
        self.assertIn("/v1/tail", missing_tail)
        self.assertIn("HTTP 404", missing_tail)
        self.assertNotIn(secret, missing_tail)

        invalid_token = _safe_tool_failure(
            BlotterError("server rejected request (403): invalid credentials"), "latest20")
        self.assertIn("authentication failed", invalid_token.lower())

        identity_mismatch = _safe_tool_failure(
            BlotterError("server rejected request (403): credential identity mismatch"), "latest20")
        self.assertIn("identity", identity_mismatch.lower())

        auth = _safe_tool_failure(
            BlotterError(f"server rejected request (401): {secret}"), "latest20")
        self.assertIn("authentication failed", auth.lower())
        self.assertNotIn(secret, auth)

        ambiguous = _safe_tool_failure(
            BlotterError("connection failed; write outcome may be unknown"), "record_activity")
        self.assertIn("same event_id", ambiguous)
        self.assertIn("never a new ID", ambiguous)


if __name__ == "__main__":
    unittest.main()
