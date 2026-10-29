"""Durable per-agent cursor client for ONE central activity ledger."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from .remote import RemoteBlotter
from .probe import _read_private_token, _target_trusted
from .store import BlotterError, GENESIS, _valid_id

@contextlib.contextmanager
def _exclusive_cursor(lock_path: Path):
    """An OS process lock, released on process death."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)

def begin_turn(client: RemoteBlotter, state_dir: Path, *, session: str, limit: int = 100,
               re_enroll_legacy: bool = False) -> dict:
    """Read all unseen global events and persist an actor-specific cursor."""
    if not 2 <= limit <= 100: raise BlotterError("page limit must be 2..100")
    actor = _valid_id(client.actor, "actor")
    folder = Path(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    # Actor identifiers can contain '/' and ':'. Never use them as paths.
    cursor_key = "actor-" + hashlib.sha256(actor.encode("utf-8")).hexdigest()
    path = folder / (cursor_key + ".json")
    # Legacy cursors used raw actor filenames, including safe nested paths.
    # Inspect only literal, non-traversing path components. Never resolve
    # actor-provided "." or ".." segments or use a legacy path for writing.
    legacy_parts = actor.split("/")
    legacy_path_safe = all(part not in ("", ".", "..") and
                           (os.name != "nt" or ":" not in part)
                           for part in legacy_parts)
    if not path.exists() and not re_enroll_legacy:
        if not legacy_path_safe:
            raise BlotterError("ambiguous legacy actor path; deliberate re-enrollment required")
        old_path = folder.joinpath(*legacy_parts[:-1], legacy_parts[-1] + ".json")
        if old_path.exists():
            raise BlotterError("legacy cursor detected; archive and deliberately re-enroll after reviewing history")
    with _exclusive_cursor(folder / (cursor_key + ".lock")):
        cursor = 0
        cursor_hash = GENESIS
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(saved, dict) or saved.get("url") != client.url
                    or type(saved.get("cursor")) is not int or saved["cursor"] < 0
                    or not isinstance(saved.get("cursor_hash"), str)
                    or len(saved["cursor_hash"]) != 64):
                raise BlotterError("cursor belongs to another ledger or is invalid")
            cursor = saved["cursor"]
            cursor_hash = saved["cursor_hash"]
        start = cursor
        seen = []
        pages = 0
        marker = None
        while True:
            result = client.check(session=session, after_seq=cursor, limit=limit,
                                  expected_after_hash=cursor_hash)
            pages += 1
            if pages > 10000: raise BlotterError("catch-up exceeded safe page limit")
            events = result["events"]
            next_seq = result["next_seq"]
            next_hash = result.get("next_hash")
            if type(next_seq) is not int or next_seq <= cursor:
                raise BlotterError("server did not advance the cursor")
            if any(type(e.get("seq")) is not int or e["seq"] <= cursor for e in events):
                raise BlotterError("server returned invalid event sequence")
            if (not isinstance(next_hash, str) or len(next_hash) != 64 or
                    any(ch not in "0123456789abcdef" for ch in next_hash)):
                raise BlotterError("server omitted valid cursor checkpoint hash")
            seen.extend(events)
            cursor = next_seq
            cursor_hash = next_hash
            marker = result["check_event"]
            if not result["has_more"]: break
        fd, temp = tempfile.mkstemp(dir=str(folder), prefix=cursor_key+".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"url":client.url,"cursor":cursor,
                           "cursor_hash":cursor_hash},out,separators=(",",":"))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp): os.unlink(temp)
        return {"actor":actor,"session":session,"previous_cursor":start,
                "cursor":cursor,"cursor_hash":cursor_hash,
                "pages":pages,"events":seen,"check_event":marker}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blotter-turn")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--url", default=os.environ.get("BLOTTER_URL"))
    parser.add_argument("--token-file", help="private bearer token file")
    parser.add_argument("--state-dir", default=str(Path.home()/".blotter"/"cursors"))
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("begin")
    begin.add_argument("--session", required=True)
    begin.add_argument("--limit", type=int, default=100)
    begin.add_argument("--re-enroll-legacy", action="store_true",
                       help="explicitly reset a legacy actor cursor and reread full history")
    record = sub.add_parser("record")
    record.add_argument("--session", required=True)
    record.add_argument("--kind", required=True)
    record.add_argument("--message", required=True)
    record.add_argument("--evidence", default="UNKNOWN")
    record.add_argument("--effect", default="NONE")
    record.add_argument("--payload", default="{}")
    record.add_argument("--event-id")
    args = parser.parse_args(argv)
    try:
        if not args.url: raise BlotterError("BLOTTER_URL or --url required")
        # Before accessing credentials, reject command-line destination
        # substitution against the operator-owned BLOTTER_URL setting.
        if not os.environ.get("BLOTTER_URL") or not _target_trusted(args.url):
            raise BlotterError("Blotter URL does not match operator configuration")
        try:
            token = (_read_private_token(Path(args.token_file)) if args.token_file
                     else os.environ.get("BLOTTER_TOKEN", ""))
        except (OSError, UnicodeError) as exc:
            raise BlotterError("private token file unavailable or unsafe") from exc
        if (not isinstance(token, str) or len(token) > 512 or
                any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise BlotterError("invalid private token configuration")
        # The private tailnet service must not inherit HTTP(S)_PROXY.
        client = RemoteBlotter(args.url, actor=args.actor, token=token,
                              direct_only=True)
        if args.command == "begin":
            value = begin_turn(client,Path(args.state_dir),session=args.session,
                               limit=args.limit,re_enroll_legacy=args.re_enroll_legacy)
        else:
            value = client.record(session=args.session,kind=args.kind,
                                  message=args.message,evidence=args.evidence,
                                  effect=args.effect,payload=json.loads(args.payload),
                                  event_id=args.event_id)
        # Windows consoles can use CP1252: JSON escapes preserve Unicode data
        # while preventing an encoding failure after the cursor was persisted.
        print(json.dumps(value,ensure_ascii=True,sort_keys=True))
        return 0
    except (BlotterError,OSError,ValueError,KeyError,TypeError) as exc:
        print("blotter-turn: "+str(exc),file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
