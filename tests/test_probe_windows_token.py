"""Probe must reuse the existing verified Windows reader, without an env token."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_blotter import probe


class WindowsProbeCredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.token"

    def test_windows_file_uses_existing_handle_verified_reader(self):
        with mock.patch.object(probe.os, "name", "nt"), mock.patch.object(
                probe, "_load_token_file", create=True, return_value="A" * 40) as reader:
            self.assertEqual(probe._read_private_token(self.path), "A" * 40)
        reader.assert_called_once_with(self.path)

    def test_windows_verification_denial_is_not_environment_fallback(self):
        with mock.patch.object(probe.os, "name", "nt"), mock.patch.object(
                probe, "_load_token_file", create=True, side_effect=ValueError("untrusted file")):
            with self.assertRaises(PermissionError):
                probe._read_private_token(self.path)

    def test_cli_token_file_failure_is_redacted_before_network(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), mock.patch.object(
                probe, "_read_private_token", side_effect=PermissionError("private-details")), mock.patch.object(
                probe, "RemoteBlotter") as client:
            result = probe.main(["--url", "http://127.0.0.1:1", "--actor", "synthetic.probe",
                                 "--token-file", str(self.path)])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(stream.getvalue())["status"], "CONFIG_PERMISSION_DENIED")
        self.assertNotIn("private-details", stream.getvalue())
        client.assert_not_called()
