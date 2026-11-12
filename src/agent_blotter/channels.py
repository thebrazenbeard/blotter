"""Optional owner-provisioned channels, isolated from the shared main log.

A channel is an authorization scope, not a filter on the main ledger. It
doesn't authenticate a model identity; its owner must already be bound to a
distinct credential at the HTTP service. Not a memory-admission system.
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .store import Blotter, BlotterError, _valid_id

CHANNEL_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
RESERVED = frozenset({"main"})
# Current project owner explicitly authorized these supplemental channels.
# main remains the existing universal log, not an owner-provisioned channel.
# No agent/chat may extend this set by inference or self-registration.
AUTHORIZED_CHANNEL_IDS = frozenset({"aeonith", "vera", "vera-works", "igor", "driftspace", "minotaur", "realm"})


def _private_windows_acl(path: Path, *, directory: bool) -> None:
    """Reject unsafe Windows ACEs; never repair ACLs as a side effect.

    The current service owner/SYSTEM/admins and current-file OWNER RIGHTS are
    trusted here. This does not provide per-chat identity, protect against a
    hostile same-UID process, or attest path handles against renaming races.
    """
    from .mcp_stdio import _trusted_windows_powershell

    script = r"""
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($env:BLOTTER_CHANNEL_ACL_PATH)
$item = Get-Item -LiteralPath $target -Force -ErrorAction Stop
if (([bool]$item.PSIsContainer) -ne ($env:BLOTTER_CHANNEL_ACL_DIRECTORY -eq '1')) { exit 4 }
if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { exit 5 }
$acl = Get-Acl -LiteralPath $target -ErrorAction Stop
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($me, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-4', 'S-1-3-0')
$owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
if ($owner -notin @($me, 'S-1-5-18', 'S-1-5-32-544')) { exit 6 }
$hasOwnerAccess = $false
foreach ($rule in $acl.Access) {
    if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
    $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($sid -notin $allowed) { exit 7 }
    if ($sid -eq $me -or $sid -eq 'S-1-3-4') { $hasOwnerAccess = $true }
}
if (-not $hasOwnerAccess) { exit 8 }
"""
    env = dict(os.environ, BLOTTER_CHANNEL_ACL_PATH=str(path),
               BLOTTER_CHANNEL_ACL_DIRECTORY="1" if directory else "0")
    try:
        result = subprocess.run(
            [_trusted_windows_powershell(), "-NoProfile", "-NonInteractive",
             "-Command", script],
            env=env, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise BlotterError("private channel Windows ACL could not be verified") from exc
    if result.returncode != 0:
        raise BlotterError("unsafe private channel Windows permissions")


def _private_directory(path: Path) -> None:
    """Fail closed on a POSIX directory that another user can traverse.

    This is not a Windows ACL implementation; an issuer-bound service identity
    and OS-level deployment controls must be qualified separately.
    """
    if path.is_symlink():
        raise BlotterError("channels directory may not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise BlotterError("unsafe private channel directory permissions")
    elif os.name == "nt":
        _private_windows_acl(path, directory=True)


def _private_database_file(path: Path) -> None:
    """Atomically precreate POSIX database files with restrictive mode.

    SQLITE journal companions live beneath a 0700 parent directory. The
    same-UID file replacement threat is not solved by this helper.
    """
    if path.is_symlink():
        raise BlotterError("channel database may not be a symlink")
    if os.name == "nt":
        # The parent directory has already passed ACL checks. A new file
        # inherits its restricted DACL before contents are added; refuse any
        # pre-existing file with unexpected inherited or explicit ACEs.
        try:
            with path.open("xb"):
                pass
        except FileExistsError:
            pass
        _private_windows_acl(path, directory=False)
        return
    if os.name != "posix":
        raise BlotterError("private channel file protection unsupported")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise BlotterError("unsafe private channel database permissions")


class ChannelCatalog:
    """Operator-provisioned, one-owner channels; no client-side self-enrollment."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        # Do not let an attacker redirect the catalog to a different root.
        if self.root.is_symlink():
            raise BlotterError("channels root may not be a symlink")
        _private_directory(self.root)
        self.ledgers = self.root / "ledgers"
        _private_directory(self.ledgers)
        self.registry = self.root / "channels.sqlite3"
        _private_database_file(self.registry)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                owner_actor TEXT NOT NULL,
                purpose TEXT NOT NULL DEFAULT ''
            )""")
            try:
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS "
                           "channels_owner_unique ON channels(owner_actor)")
            except sqlite3.IntegrityError as exc:
                raise BlotterError("channel catalog has ambiguous owner mappings") from exc

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.registry, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _id(channel_id: str) -> str:
        if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
            raise BlotterError("invalid channel ID")
        return channel_id

    def provision(self, channel_id: str, *, owner_actor: str,
                  purpose: str = "") -> dict[str, str]:
        """Administrator-side local operation. Never exposed by HTTP."""
        self._id(channel_id)
        if channel_id in RESERVED:
            raise BlotterError("main is the existing shared ledger")
        if channel_id not in AUTHORIZED_CHANNEL_IDS:
            raise BlotterError("channel is not user-authorized")
        _valid_id(owner_actor, "owner_actor")
        if not isinstance(purpose, str) or len(purpose) > 512:
            raise BlotterError("invalid channel purpose")
        with self._connect() as db:
            row = db.execute("SELECT owner_actor,purpose FROM channels WHERE id=?",
                             (channel_id,)).fetchone()
            if row and row != (owner_actor, purpose):
                raise BlotterError("channel already provisioned for another owner or purpose")
            if not row:
                try:
                    db.execute("INSERT INTO channels(id, owner_actor, purpose) VALUES (?,?,?)",
                               (channel_id, owner_actor, purpose))
                except sqlite3.IntegrityError as exc:
                    raise BlotterError("owner actor already owns another channel") from exc
        return {"channel": channel_id, "owner_actor": owner_actor, "purpose": purpose}

    def owner_actors(self) -> set[str]:
        """Read owner bindings for startup credential reconciliation."""
        with self._connect() as db:
            return {row[0] for row in db.execute("SELECT owner_actor FROM channels")}

    def allowed(self, *, actor: str) -> list[dict[str, str]]:
        _valid_id(actor, "actor")
        with self._connect() as db:
            return [{"channel": row[0], "owner_actor": row[1], "purpose": row[2]}
                    for row in db.execute(
                        "SELECT id,owner_actor,purpose FROM channels "
                        "WHERE owner_actor=? ORDER BY id", (actor,))]

    def ledger(self, channel_id: str, *, actor: str) -> Blotter:
        """Fail closed without revealing whether the channel exists."""
        self._id(channel_id)
        _valid_id(actor, "actor")
        if channel_id in RESERVED:
            raise BlotterError("main is accessed through the standard endpoints")
        if channel_id not in AUTHORIZED_CHANNEL_IDS:
            raise BlotterError("channel unavailable")
        with self._connect() as db:
            row = db.execute("SELECT owner_actor FROM channels WHERE id=?",
                             (channel_id,)).fetchone()
        if row is None or row[0] != actor:
            raise BlotterError("channel unavailable")
        path = self.ledgers / (channel_id + ".sqlite3")
        _private_directory(self.ledgers)
        _private_database_file(path)
        return Blotter(path)
