"""Security checks that do not need a listening HTTP server."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_blotter.cli import main
from agent_blotter.server import _check_credentials_file_permissions
from agent_blotter.store import BlotterError


class CredentialFilePermissionsTests(unittest.TestCase):
    def test_windows_acl_check_must_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "server-credentials.json")
            path.write_text("{}", encoding="utf-8")
            with mock.patch("agent_blotter.mcp_stdio._check_windows_token_acl") as acl:
                _check_credentials_file_permissions(path, platform="nt")
                acl.assert_called_once_with(path)

    def test_windows_acl_failure_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "server-credentials.json")
            path.write_text("{}", encoding="utf-8")
            with mock.patch("agent_blotter.mcp_stdio._check_windows_token_acl",
                            side_effect=ValueError("private check failed")):
                with self.assertRaisesRegex(BlotterError, "credentials file permissions"):
                    _check_credentials_file_permissions(path, platform="nt")

    def test_windows_third_party_owner_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "server-credentials.json")
            path.write_text("{}", encoding="utf-8")
            with mock.patch("agent_blotter.mcp_stdio.subprocess.run") as power_shell:
                power_shell.return_value.returncode = 5
                power_shell.return_value.stdout = ""
                with self.assertRaisesRegex(BlotterError, "permissions"):
                    _check_credentials_file_permissions(path, platform="nt")

    def test_posix_permissions_reject_group_readable_credentials(self):
        if os.name == "nt":
            self.skipTest("POSIX mode bits do not validate Windows DACL")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "server-credentials.json")
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(BlotterError):
                _check_credentials_file_permissions(path, platform="posix")
            path.chmod(0o600)
            _check_credentials_file_permissions(path, platform="posix")

    def test_symlink_credentials_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "server-credentials.json")
            target = Path(tmp, "target.json")
            target.write_text("{}", encoding="utf-8")
            try:
                path.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("Creating symlinks is not permitted on this machine")
            with self.assertRaises(BlotterError):
                _check_credentials_file_permissions(path, platform="posix")


class PrivateRemoteCLITests(unittest.TestCase):
    def test_remote_cli_ignores_environment_proxy_unless_explicitly_requested(self):
        with mock.patch.dict(os.environ, {"BLOTTER_TOKEN": "T" * 40,
                                       "HTTPS_PROXY": "http://intercept.example:3128"}), \
             mock.patch("agent_blotter.cli.RemoteBlotter") as remote, \
             contextlib.redirect_stdout(io.StringIO()):
            remote.return_value.tail.return_value = []
            args = ["--url", "https://lappy.tail86ea75.ts.net/blotter",
                    "--identity", "agent.one", "tail"]
            self.assertEqual(main(args), 0)
            self.assertTrue(remote.call_args.kwargs["direct_only"])
            self.assertEqual(main(["--allow-environment-proxy", *args]), 0)
            self.assertFalse(remote.call_args.kwargs["direct_only"])


if __name__ == "__main__":
    unittest.main()
