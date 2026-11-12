"""Optional owner-provisioned channels, isolated from the shared main log.

A channel is an authorization scope, not a filter on the main ledger. It
doesn't authenticate a model identity; its owner must already be bound to a
distinct credential at the HTTP service. Not a memory-admission system.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .store import Blotter, BlotterError, _valid_id

CHANNEL_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
RESERVED = frozenset({"main"})
# Current project owner explicitly authorized these supplemental channels.
# main remains the existing universal log, not an owner-provisioned channel.
# No agent/chat may extend this set by inference or self-registration.
AUTHORIZED_CHANNEL_IDS = frozenset({"aeonith", "vera", "vera-works", "igor", "driftspace", "minotaur", "realm"})


class ChannelCatalog:
    """Operator-provisioned, one-owner channels; no client-side self-enrollment."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        # Do not let an attacker redirect the catalog to a different root.
        if self.root.is_symlink():
            raise BlotterError("channels root may not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledgers = self.root / "ledgers"
        if self.ledgers.is_symlink():
            raise BlotterError("channels directory may not be a symlink")
        self.ledgers.mkdir(exist_ok=True)
        self.registry = self.root / "channels.sqlite3"
        if self.registry.is_symlink():
            raise BlotterError("channel catalog may not be a symlink")
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                owner_actor TEXT NOT NULL,
                purpose TEXT NOT NULL DEFAULT ''
            )""")

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
                db.execute("INSERT INTO channels(id, owner_actor, purpose) VALUES (?,?,?)",
                           (channel_id, owner_actor, purpose))
        return {"channel": channel_id, "owner_actor": owner_actor, "purpose": purpose}

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
        if path.is_symlink():
            raise BlotterError("channel ledger may not be a symlink")
        return Blotter(path)
