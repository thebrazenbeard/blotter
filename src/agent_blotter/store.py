"""Single-lane, local activity blotter. Every record is an event, never a message."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

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
    r"(^|[_\-])(password|passwd|secret|token|api[_\-]?key|access[_\-]?token|refresh[_\-]?token|"
    r"auth(?:orization)?|cookie|credential|private[_\-]?key|session[_\-]?token)([_\-]|$)",
    re.IGNORECASE,
)


class BlotterError(ValueError):
    """Invalid operation or incompatible ledger."""


class IntegrityError(BlotterError):
    """The activity sequence failed an integrity check."""


def _utc(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if not isinstance(value, str):
        raise BlotterError("occurred_at must be an ISO 8601 string")
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
    """Field-name redaction only. Does not protect secrets in free text."""
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if SENSITIVE_KEY.search(str(k)) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _valid_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise BlotterError(f"{field} must be a nonempty identifier (max 128 characters)")
    return value


def _digest(previous: str, event: dict[str, Any]) -> str:
    return hashlib.sha256((previous + "\n" + _json(event)).encode("utf-8")).hexdigest()


def _paging(after_seq: int, limit: int) -> None:
    if type(after_seq) is not int or after_seq < 0:
        raise BlotterError("after_seq must be nonnegative")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise BlotterError("limit must be 1-1000")


class Blotter:
    """One local ordered ledger for all participating agent identities."""

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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            yield db
        finally:
            db.close()

    def _base(
        self, *, actor: str, kind: str, message: str, level: str = "INFO",
        evidence: str = "UNKNOWN", session: str = "default",
        payload: dict[str, Any] | None = None, source_ids: Iterable[str] = (),
        effect: str = "NONE", event_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        actor, kind, session = (_valid_id(actor, "actor"), _valid_id(kind, "kind"),
                                _valid_id(session, "session"))
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
        sources = list(source_ids)
        if len(set(sources)) != len(sources):
            raise BlotterError("source_ids must be unique")
        for source_id in sources:
            _valid_id(source_id, "source_id")
        base = {
            "v": SCHEMA_VERSION, "id": _valid_id(event_id, "event_id") if event_id else str(uuid.uuid4()),
            "actor": actor, "kind": kind, "session": session, "level": level,
            "evidence": evidence, "message": message, "payload": _redact(payload),
            "source_ids": sources, "effect": effect, "correlation_id": correlation_id,
        }
        _json(base)
        return base

    def _append_locked(self, db: sqlite3.Connection, base: dict[str, Any],
                       occurred_at: str | None) -> dict[str, Any]:
        explicit_time = _utc(occurred_at) if occurred_at is not None else None
        existing = db.execute("SELECT body FROM events WHERE id=?", (base["id"],)).fetchone()
        if existing:
            old = json.loads(existing[0])
            if any(old.get(key) != val for key, val in base.items()) or (
                explicit_time is not None and old.get("occurred_at") != explicit_time
            ):
                raise BlotterError("event_id collision with different content")
            return old
        for source_id in base["source_ids"]:
            if not db.execute("SELECT 1 FROM events WHERE id=?", (source_id,)).fetchone():
                raise BlotterError(f"unknown source event: {source_id}")
        latest = db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, previous = (latest[0] + 1, latest[1]) if latest else (1, GENESIS)
        event = {**base, "seq": seq, "occurred_at": explicit_time or _utc(None),
                 "recorded_at": _utc(None)}
        encoded = _json(event)
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise BlotterError("event exceeds 64 KiB")
        db.execute("""INSERT INTO events (seq, id, actor, kind, session, recorded_at,
                     body, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (seq, base["id"], base["actor"], base["kind"], base["session"],
                    event["recorded_at"], encoded, previous, _digest(previous, event)))
        return event

    def record(
        self, *, actor: str, kind: str, message: str, level: str = "INFO",
        evidence: str = "UNKNOWN", session: str = "default",
        payload: dict[str, Any] | None = None, occurred_at: str | None = None,
        source_ids: Iterable[str] = (), effect: str = "NONE",
        event_id: str | None = None, correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an action in the *same* lane as every other agent."""
        if kind == "turn.checked":
            raise BlotterError("turn.checked is reserved for check()")
        base = self._base(actor=actor, kind=kind, message=message, level=level,
                          evidence=evidence, session=session, payload=payload,
                          source_ids=source_ids, effect=effect, event_id=event_id,
                          correlation_id=correlation_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                event = self._append_locked(db, base, occurred_at)
                db.execute("COMMIT")
                return event
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def check(self, *, actor: str, session: str = "default", after_seq: int = 0,
              limit: int = 100) -> dict[str, Any]:
        """Inspect the shared activity since a cursor, recording this inspection.

        Reads a consistent pre-check cut while holding the writer transaction.
        If has_more is true, the caller MUST page again before claiming to have
        reviewed the entire unseen history.
        """
        _paging(after_seq, limit)
        actor, session = _valid_id(actor, "actor"), _valid_id(session, "session")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                latest = db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
                if after_seq > latest:
                    raise BlotterError("cursor is beyond this ledger head")
                rows = db.execute(
                    "SELECT body FROM events WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?",
                    (after_seq, latest, limit + 1),
                ).fetchall()
                has_more = len(rows) > limit
                events = [json.loads(row[0]) for row in rows[:limit]]
                last_seen = events[-1]["seq"] if events else after_seq
                marker = self._base(
                    actor=actor, session=session, kind="turn.checked",
                    message="Read shared Blotter activity", level="AUDIT",
                    evidence="OBSERVATION",
                    payload={"from_seq": after_seq, "through_seq": latest,
                             "returned_count": len(events), "has_more": has_more},
                )
                check_event = self._append_locked(db, marker, None)
                db.execute("COMMIT")
                return {
                    "events": events, "has_more": has_more, "visible_head_seq": latest,
                    "check_event": check_event,
                    "next_seq": last_seen if has_more else check_event["seq"],
                }
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def list(self, *, actor: str | None = None, kind: str | None = None,
             session: str | None = None, after_seq: int = 0,
             limit: int = 100) -> list[dict[str, Any]]:
        """Read the single global log. Filtering is a view, not a separate lane."""
        _paging(after_seq, limit)
        query, params = "SELECT body FROM events WHERE seq>?", [after_seq]
        for field, value in (("actor", actor), ("kind", kind), ("session", session)):
            if value is not None:
                query += f" AND {field}=?"
                params.append(_valid_id(value, field))
        query += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, params)]

    def export(self, *, after_seq: int = 0) -> Iterable[str]:
        if type(after_seq) is not int or after_seq < 0:
            raise BlotterError("after_seq must be nonnegative")
        with self._connect() as db:
            for (body,) in db.execute("SELECT body FROM events WHERE seq>? ORDER BY seq", (after_seq,)):
                yield body

    def verify(self) -> dict[str, Any]:
        """Check local integrity, not writer authenticity or action truthfulness."""
        previous, count = GENESIS, 0
        with self._connect() as db:
            for seq, eid, actor, kind, session, recorded_at, body, prior, digest in db.execute(
                "SELECT seq,id,actor,kind,session,recorded_at,body,prev_hash,hash "
                "FROM events ORDER BY seq"
            ):
                count += 1
                try:
                    event = json.loads(body)
                    calculated = _digest(previous, event)
                except (ValueError, TypeError, BlotterError) as exc:
                    raise IntegrityError(f"invalid event at sequence {seq}") from exc
                if (seq != count or event.get("seq") != seq or prior != previous or
                    digest != calculated or
                    any(event.get(k) != v for k, v in (
                        ("id", eid), ("actor", actor), ("kind", kind),
                        ("session", session), ("recorded_at", recorded_at)))):
                    raise IntegrityError(f"integrity mismatch at sequence {seq}")
                previous = digest
        return {"ok": True, "count": count, "head_hash": previous, "schema_version": SCHEMA_VERSION}
