"""Read private server credentials from one opened, verified file object.

The server loads these once at startup. The actor/token mapping is never logged,
and this module does not create credentials or change filesystem ACLs.
"""
from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
import stat
import subprocess

from .store import BlotterError

MAX_CREDENTIAL_FILE_BYTES = 1_048_576


def _read_posix_private_bytes(path: Path) -> bytes:
    """Walk from / using no-follow directory fds, then fstat/read one leaf fd."""
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")):
        raise BlotterError("private credentials require no-follow descriptor support")
    full = Path(os.path.abspath(os.fspath(path)))
    parts = full.parts
    if not parts or parts[0] != os.sep or len(parts) < 2:
        raise BlotterError("credentials file path is invalid")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    # O_NONBLOCK keeps a swapped FIFO/device from hanging before fstat rejects it.
    leaf_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    parent_fd = None
    file_fd = None
    try:
        parent_fd = os.open(parts[0], directory_flags)
        for component in parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(parts[-1], leaf_flags, dir_fd=parent_fd)
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_uid != os.geteuid()):
            raise BlotterError("credentials file permissions are unsafe")
        if not 0 < info.st_size <= MAX_CREDENTIAL_FILE_BYTES:
            raise BlotterError("credentials file size is invalid")
        with os.fdopen(file_fd, "rb", closefd=True) as stream:
            file_fd = None
            data = stream.read(MAX_CREDENTIAL_FILE_BYTES + 1)
        if not 0 < len(data) <= MAX_CREDENTIAL_FILE_BYTES:
            raise BlotterError("credentials file size is invalid")
        return data
    except OSError as exc:
        raise BlotterError("private credentials file could not be opened or verified") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _read_windows_private_bytes(path: Path) -> bytes:
    """Validate ACL, reparse status, resolved path, and bytes on one Win32 handle."""
    from .mcp_stdio import _trusted_windows_powershell

    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

public static class BlotterServerCredentialsFile {
    [StructLayout(LayoutKind.Sequential)]
    private struct FILE_ATTRIBUTE_TAG_INFO {
        public uint FileAttributes;
        public uint ReparseTag;
    }

    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern SafeFileHandle CreateFileW(
        string path, uint access, uint share, IntPtr security,
        uint disposition, uint flags, IntPtr templateHandle);

    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool GetFileInformationByHandleEx(
        SafeFileHandle handle, int informationClass,
        out FILE_ATTRIBUTE_TAG_INFO info, uint infoSize);

    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern uint GetFinalPathNameByHandleW(
        SafeFileHandle handle, StringBuilder path, uint size, uint flags);

    public static SafeFileHandle OpenNoReparse(string path) {
        SafeFileHandle handle = CreateFileW(
            path, 0x80000000U, 1U, IntPtr.Zero, 3U, 0x00200000U, IntPtr.Zero);
        if (handle.IsInvalid) {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error);
        }
        try {
            FILE_ATTRIBUTE_TAG_INFO info;
            if (!GetFileInformationByHandleEx(handle, 9, out info,
                    (uint)Marshal.SizeOf(typeof(FILE_ATTRIBUTE_TAG_INFO)))) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            if ((info.FileAttributes & 0x400U) != 0) {
                throw new IOException("Private credentials reparse point rejected");
            }
            string full = Path.GetFullPath(path);
            string expected = full.StartsWith(@"\\?\", StringComparison.Ordinal)
                ? full
                : full.StartsWith(@"\\", StringComparison.Ordinal)
                    ? @"\\?\UNC\" + full.Substring(2)
                    : @"\\?\" + full;
            StringBuilder actual = new StringBuilder(32768);
            uint size = GetFinalPathNameByHandleW(handle, actual, (uint)actual.Capacity, 0);
            if (size == 0 || size >= actual.Capacity) {
                throw new IOException("Private credentials final path unavailable");
            }
            if (!String.Equals(expected, actual.ToString(),
                    StringComparison.OrdinalIgnoreCase)) {
                throw new IOException("Private credentials resolved parent path rejected");
            }
            return handle;
        } catch {
            handle.Dispose();
            throw;
        }
    }
}
'@
$handle = [BlotterServerCredentialsFile]::OpenNoReparse($env:BLOTTER_SERVER_CREDENTIAL_PATH)
$stream = [IO.FileStream]::new($handle, [IO.FileAccess]::Read, 4096, $false)
try {
    $acl = $stream.GetAccessControl()
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $permitted = @($me, 'S-1-5-18')
    $ownerSid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -notin $permitted) { exit 5 }
    $foundMe = $false
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { continue }
        $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $permitted) { exit 3 }
        if ($sid -eq $me) { $foundMe = $true }
    }
    if (-not $foundMe) { exit 4 }
    if ($stream.Length -lt 1 -or $stream.Length -gt 1048576) { exit 6 }
    $bytes = New-Object byte[] ([int]$stream.Length)
    $offset = 0
    while ($offset -lt $bytes.Length) {
        $received = $stream.Read($bytes, $offset, $bytes.Length - $offset)
        if ($received -le 0) { exit 7 }
        $offset += $received
    }
    [Console]::Out.Write([Convert]::ToBase64String($bytes))
} finally {
    $stream.Dispose()
}
"""
    env = dict(os.environ, BLOTTER_SERVER_CREDENTIAL_PATH=str(path))
    try:
        check = subprocess.run(
            [_trusted_windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
            env=env, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise BlotterError("private credentials file could not be opened or verified") from exc
    if check.returncode != 0:
        raise BlotterError("private credentials file permissions or read could not be verified")
    try:
        data = base64.b64decode(check.stdout, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BlotterError("private credentials file read returned invalid bytes") from exc
    if not 0 < len(data) <= MAX_CREDENTIAL_FILE_BYTES:
        raise BlotterError("credentials file size is invalid")
    return data


def read_private_credentials_bytes(path: str | Path) -> bytes:
    """Fail closed on any platform where handle-bound private read is unavailable."""
    path = Path(path)
    if os.name == "nt":
        return _read_windows_private_bytes(path)
    if os.name == "posix":
        return _read_posix_private_bytes(path)
    raise BlotterError("private credentials unsupported on this platform")
