"""Local append-only, provenance-aware activity and knowledge ledger.

No network access, automatic capture, LLM execution, or implicit promotion.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
GENESIS = "0" * 64
MAX_EVENT_BYTES = 65536
LEVELS = frozenset(("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "AUDIT"))
EVIDENCE = frozenset(("USER", "OBSERVATION", "RETRIEVED", "INFERENCE", "HYPOTHESIS",
                      "REVIEW", "SOURCE", "UNKNOWN"))
EFFECTS = frozenset(("NONE", "PROPOSED", "ATTEMPTED", "REPORTED",
                     "OBSERVED", "VERIFIED", "UNKNOWN"))
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
SENSITIVE_KEY = re.compile(
    r"(^|[_\-])(password|passwd|secret|api[_\-]?key|access[_\-]?token|refresh[_\-]?token|"
    r"auth(?:orization)?|cookie|credential|private[_\-]?key|session[_\-]?token)([_\-]|$)",
    re.IGNORECASE,
)


class BlotterError(ValueError):
    """An invalid operation or incompatible ledger."""


class IntegrityError(BlotterError):
    """The stored event sequence failed its consistency check."""


def _utc(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if not isinstance(value, str):
        raise BlotterError("occurred_at must be an ISO 8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BlotterError("invalid occurred_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BlotterError("occurred_at requires a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlotterError("event fields must be finite JSON values") from exc


def _redact(value: Any) -> Any:
    """Best-effort field-name redaction, not secret scanning of arbitrary strings."""
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if SENSITIVE_KEY.search(str(key)) else _redact(val))
                for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _valid_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise BlotterError(f"{field} must be a nonempty identifier (max 128 characters)")
    return value


def _digest(previous: str, event: dict[str, Any]) -> str:
    return hashlib.sha256((previous + "\n" + _json(event)).encode("utf-8")).hexdigest()


class Blotter:
    """One local SQLite ledger. Concurrency is transactional, not distributed."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists() and not self.path.is_file():
            raise BlotterError("ledger path is not a regular file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise BlotterError(f"unsupported ledger schema: {version}")
                if version == 0:
                    db.execute("""CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        actor TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        session TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        body TEXT NOT NULL,
                        prev_hash TEXT NOT NULL,
                        hash TEXT NOT NULL
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS events_actor_seq ON events(actor, seq)")
                    db.execute("CREATE INDEX IF NOT EXISTS events_kind_seq ON events(kind, seq)")
                    db.execute("PRAGMA user_version=1")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def record(
        self, *, actor: str, kind: str, message: str, level: str = "INFO",
        evidence: str = "UNKNOWN", session: str = "default",
        payload: dict[str, Any] | None = None, occurred_at: str | None = None,
        source_ids: Iterable[str] = (), effect: str = "NONE",
        event_id: str | None = None, correlation_id: str | None = None,
    ) -> dict[str, Any]:
        actor = _valid_id(actor, "actor")
        kind = _valid_id(kind, "kind")
        session = _valid_id(session, "session")
        level, evidence, effect = level.upper(), evidence.upper(), effect.upper()
        if level not in LEVELS or evidence not in EVIDENCE or effect not in EFFECTS:
            raise BlotterError("invalid level, evidence or effect")
        if not isinstance(message, str) or not message.strip() or len(message) > 8192:
            raise BlotterError("message must contain 1-8192 characters")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise BlotterError("payload must be a JSON object")
        if correlation_id is not None:
            _valid_id(correlation_id, "correlation_id")
        event_id = _valid_id(event_id, "event_id") if event_id else str(uuid.uuid4())
        sources = list(source_ids)
        if len(set(sources)) != len(sources):
            raise BlotterError("source_ids must be unique")
        for source_id in sources:
            _valid_id(source_id, "source_id")
        if kind == "knowledge.promoted" and not sources:
            raise BlotterError("knowledge promotion requires at least one source event")
        clean_payload = _redact(payload)
        base = {
            "v": SCHEMA_VERSION, "id": event_id, "actor": actor, "kind": kind,
            "session": session, "level": level, "evidence": evidence,
            "message": message, "payload": clean_payload, "source_ids": sources,
            "effect": effect, "correlation_id": correlation_id,
        }
        # A repeated explicit id is idempotent only for the same submitted content.
        # Auto-generated observation times do not create false conflicts on retry.
        explicit_time = _utc(occurred_at) if occurred_at is not None else None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT body FROM events WHERE id=?", (event_id,)).fetchone()
                if existing:
                    old = json.loads(existing[0])
                    compare = {key: old[key] for key in base}
                    if compare != base or (explicit_time and old["occurred_at"] != explicit_time):
                        raise BlotterError("event_id collision with different content")
                    db.execute("COMMIT")
                    return old
                for sid in sources:
                    if not db.execute("SELECT 1 FROM events WHERE id=?", (sid,)).fetchone():
                        raise BlotterError(f"unknown source event: {sid}")
                latest = db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                seq, previous = (latest[0] + 1, latest[1]) if latest else (1, GENESIS)
                event = {**base, "seq": seq, "occurred_at": explicit_time or _utc(None),
                         "recorded_at": _utc(None)}
                serialized = _json(event)
                if len(serialized.encode("utf-8")) > MAX_EVENT_BYTES:
                    raise BlotterError("event exceeds 64 KiB")
                digest = _digest(previous, event)
                db.execute("""INSERT INTO events
                    (seq, id, actor, kind, session, recorded_at, body, prev_hash, hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (seq, event_id, actor, kind, session, event["recorded_at"],
                     serialized, previous, digest))
                db.execute("COMMIT")
                return event
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def promote(self, *, actor: str, message: str, source_ids: Iterable[str],
                session: str = "default", payload: dict[str, Any] | None = None,
                event_id: str | None = None) -> dict[str, Any]:
        """Explicit knowledge promotion; source events must exist in this ledger."""
        return self.record(actor=actor, kind="knowledge.promoted", message=message,
                           evidence="REVIEW", session=session, source_ids=source_ids,
                           payload=payload, event_id=event_id)

    def list(self, *, actor: str | None = None, kind: str | None = None,
             session: str | None = None, after_seq: int = 0,
             limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise BlotterError("limit must be 1-1000")
        if not isinstance(after_seq, int) or after_seq < 0:
            raise BlotterError("after_seq must be nonnegative")
        query, params = "SELECT body FROM events WHERE seq>?", [after_seq]
        for field, value in (("actor", actor), ("kind", kind), ("session", session)):
            if value is not None:
                query += f" AND {field}=?"
                params.append(_valid_id(value, field))
        query += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, params)]

    def knowledge(self, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self.list(kind="knowledge.promoted", after_seq=after_seq, limit=limit)

    def export(self, *, after_seq: int = 0) -> Iterable[str]:
        """A read-only JSONL stream. Consumers should hold their own snapshot."""
        if not isinstance(after_seq, int) or after_seq < 0:
            raise BlotterError("after_seq must be nonnegative")
        with self._connect() as db:
            for (body,) in db.execute("SELECT body FROM events WHERE seq>? ORDER BY seq", (after_seq,)):
                yield body

    def verify(self) -> dict[str, Any]:
        """Detect a broken local chain. This does NOT authenticate an untrusted writer."""
        previous, count = GENESIS, 0
        with self._connect() as db:
            for seq, body, prior, digest in db.execute(
                "SELECT seq, body, prev_hash, hash FROM events ORDER BY seq"
            ):
                count += 1
                try:
                    event = json.loads(body)
                    calculated = _digest(previous, event)
                except (ValueError, TypeError, BlotterError) as exc:
                    raise IntegrityError(f"invalid event at sequence {seq}") from exc
                if seq != count or event.get("seq") != seq or prior != previous or digest != calculated:
                    raise IntegrityError(f"integrity mismatch at sequence {seq}")
                previous = digest
        return {"ok": True, "count": count, "head_hash": previous, "schema_version": SCHEMA_VERSION}
