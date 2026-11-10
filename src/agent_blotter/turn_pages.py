"""Bounded durable delivery: read a page, then explicitly acknowledge its receipt.

The cursor and pending response share one atomically replaced actor document.
Unacknowledged responses replay exactly across process/session restarts. The
caller supplies a separate state directory for each independent reader.
"""
from __future__ import annotations

import json
from pathlib import Path
import secrets

from .remote import RemoteBlotter
from .store import BlotterError, _digest, _valid_id
from .turn import (_cursor_hash_valid, _cursor_key, _exclusive_cursor,
                   _load_cursor_state, _write_cursor_state)


MAX_PAGE_BYTES = 1_750_000


def _encoded_size(page: dict) -> int:
    try:
        return len(json.dumps(page, ensure_ascii=True, sort_keys=True,
                              allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlotterError("turn page is not finite JSON") from exc


def _validate_result(result: dict, *, actor: str, session: str, cursor: int,
                     cursor_hash: str, limit: int) -> None:
    """Reject gaps, invented checkpoints and mismatched check metadata."""
    try:
        events = result["events"]
        more = result["has_more"]
        head = result["visible_head_seq"]
        marker = result["check_event"]
        if (not isinstance(events, list) or len(events) > limit
                or type(more) is not bool or type(head) is not int or head < cursor
                or not isinstance(marker, dict)):
            raise ValueError()
        previous = cursor_hash
        for seq, event in enumerate(events, start=cursor + 1):
            if not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] != seq:
                raise ValueError()
            previous = _digest(previous, event)
        last = cursor + len(events)
        if ((more and (len(events) != limit or head <= last))
                or (not more and head != last)):
            raise ValueError()
        metadata = marker.get("payload")
        if (not isinstance(metadata, dict)
                or any(type(metadata.get(key)) is not int
                       for key in ("from_seq", "through_seq", "returned_count"))
                or type(metadata.get("has_more")) is not bool):
            raise ValueError()
        if (type(marker.get("seq")) is not int or marker["seq"] != head + 1
                or marker.get("actor") != actor or marker.get("session") != session
                or marker.get("kind") != "turn.checked"
                or marker.get("payload") != {"from_seq": cursor, "through_seq": head,
                                              "returned_count": len(events), "has_more": more}):
            raise ValueError()
        next_seq = last if more else marker["seq"]
        if not more:
            previous = _digest(previous, marker)
        if (type(result["next_seq"]) is not int or result["next_seq"] != next_seq
                or not _cursor_hash_valid(result["next_hash"])
                or result["next_hash"] != previous):
            raise ValueError()
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise BlotterError("turn page sequence, metadata or hash checkpoint is invalid") from exc


def _validate_pending(saved: dict, actor: str) -> dict:
    try:
        page = saved["pending_page"]
        if (not isinstance(page, dict) or page.get("actor") != actor
                or type(page.get("previous_cursor")) is not int
                or page.get("previous_cursor") != saved["cursor"]
                or not _cursor_hash_valid(page.get("receipt"))):
            raise ValueError()
        _valid_id(page["session"], "session")
        result = {"events": page["events"], "has_more": page["has_more"],
                  "visible_head_seq": page["visible_head_seq"],
                  "check_event": page["check_event"], "next_seq": page["cursor"],
                  "next_hash": page["cursor_hash"]}
        _validate_result(result, actor=actor, session=page["session"],
                         cursor=saved["cursor"], cursor_hash=saved["cursor_hash"],
                         limit=len(page["events"]) or 1)
        return page
    except (ValueError, KeyError, TypeError) as exc:
        raise BlotterError("pending turn page is invalid; cursor unchanged") from exc


def read_turn_page(client: RemoteBlotter, state_dir: Path, *, session: str,
                   limit: int = 20, max_bytes: int = MAX_PAGE_BYTES) -> dict:
    """Deliver one bounded page without advancing the acknowledged cursor.

    Repeated calls return the exact pending response, retaining its original
    session even if the caller restarted into a new turn. A response that does
    not fit is retried at a smaller page size, never below two records. Each
    check itself adds a marker, so a one-record page could chase its own markers
    forever. An oversized minimum page fails without advancing the cursor.
    """
    actor, session = _valid_id(client.actor, "actor"), _valid_id(session, "session")
    if type(limit) is not int or not 2 <= limit <= 100:
        raise BlotterError("page limit must be 2..100")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PAGE_BYTES:
        raise BlotterError("page byte budget is invalid")
    folder = Path(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    with _exclusive_cursor(folder / (_cursor_key(actor) + ".lock")):
        saved = _load_cursor_state(client, folder)
        if "pending_page" in saved:
            page = _validate_pending(saved, actor)
            if _encoded_size(page) > max_bytes:
                raise BlotterError("pending turn page exceeds byte budget; cursor unchanged")
            return page
        while True:
            result = client.check(session=session, after_seq=saved["cursor"], limit=limit,
                                  expected_after_hash=saved["cursor_hash"])
            _validate_result(result, actor=actor, session=session, cursor=saved["cursor"],
                             cursor_hash=saved["cursor_hash"], limit=limit)
            page = {"receipt": secrets.token_hex(32), "actor": actor, "session": session,
                    "previous_cursor": saved["cursor"], "cursor": result["next_seq"],
                    "cursor_hash": result["next_hash"], "has_more": result["has_more"],
                    "events": result["events"], "check_event": result["check_event"],
                    "visible_head_seq": result["visible_head_seq"]}
            if _encoded_size(page) <= max_bytes:
                updated = dict(saved, pending_page=page)
                _write_cursor_state(folder, actor, updated)
                return page
            if len(result["events"]) <= 2:
                raise BlotterError("minimum turn page exceeds byte budget; cursor unchanged")
            limit = max(2, len(result["events"]) // 2)


def acknowledge_turn_page(client: RemoteBlotter, state_dir: Path, *, receipt: str) -> dict:
    """Acknowledge the validated delivered snapshot, not the server's live head.

    Exactly the saved receipt can advance a cursor. Retry of the most recent
    acknowledgment is idempotent and cannot discard a newer pending page.
    The next check verifies the committed hash against the remote ledger.
    """
    actor = _valid_id(client.actor, "actor")
    if not _cursor_hash_valid(receipt):
        raise BlotterError("turn page receipt is invalid")
    folder = Path(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    with _exclusive_cursor(folder / (_cursor_key(actor) + ".lock")):
        saved = _load_cursor_state(client, folder)
        page = _validate_pending(saved, actor) if "pending_page" in saved else None
        last = saved.get("last_ack")
        if isinstance(last, dict) and last.get("receipt") == receipt:
            if (last.get("actor") != actor or type(last.get("cursor")) is not int
                    or last["cursor"] != saved["cursor"]
                    or last.get("cursor_hash") != saved["cursor_hash"]
                    or type(last.get("has_more")) is not bool
                    or last.get("acknowledged") is not True):
                raise BlotterError("saved acknowledgment does not match cursor")
            return last
        if page is None or page["receipt"] != receipt:
            raise BlotterError("turn page receipt is unknown or stale; cursor unchanged")
        result = {"receipt": receipt, "actor": actor, "session": page["session"],
                  "cursor": page["cursor"], "cursor_hash": page["cursor_hash"],
                  "has_more": page["has_more"], "acknowledged": True}
        updated = dict(saved, cursor=page["cursor"], cursor_hash=page["cursor_hash"],
                       last_ack=result)
        updated.pop("pending_page")
        _write_cursor_state(folder, actor, updated)
        return result
