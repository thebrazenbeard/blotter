"""Blotter CLI: one ledger, turn checks and per-activity records."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .remote import RemoteBlotter
from .store import Blotter, BlotterError, IntegrityError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blotter", description="One shared chronological agent activity blotter")
    parser.add_argument("--db", default=os.environ.get(
        "BLOTTER_DB", str(Path.home() / ".blotter" / "activity.sqlite3")),
        help="single local SQLite file")
    parser.add_argument("--url", default=os.environ.get("BLOTTER_URL"),
                        help="central Blotter HTTPS service URL (use BLOTTER_TOKEN environment variable)")
    parser.add_argument("--identity", help="agent credential identity for remote read/export")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize a local activity ledger")
    check = commands.add_parser("check", help="read unseen shared activity and log this turn check")
    check.add_argument("--actor", required=True)
    check.add_argument("--session", default="default")
    check.add_argument("--after-seq", type=int, default=0)
    check.add_argument("--limit", type=int, default=100)
    record = commands.add_parser("record", help="append one actual activity to the shared lane")
    record.add_argument("--actor", required=True)
    record.add_argument("--kind", required=True)
    record.add_argument("--message", required=True)
    record.add_argument("--session", default="default")
    record.add_argument("--level", default="INFO")
    record.add_argument("--evidence", default="UNKNOWN")
    record.add_argument("--effect", default="NONE")
    record.add_argument("--payload", default="{}")
    record.add_argument("--event-id")
    record.add_argument("--correlation-id")
    record.add_argument("--occurred-at")
    record.add_argument("--source-id", action="append", default=[])
    listing = commands.add_parser("list", help="read the global chronological log")
    listing.add_argument("--actor")
    listing.add_argument("--kind")
    listing.add_argument("--session")
    listing.add_argument("--after-seq", type=int, default=0)
    listing.add_argument("--limit", type=int, default=100)
    exported = commands.add_parser("export", help="export global history as JSONL")
    exported.add_argument("--after-seq", type=int, default=0)
    commands.add_parser("verify", help="locally verify sequence and hash integrity")
    args = parser.parse_args(argv)
    try:
        if args.url:
            if args.command in ("init", "verify"):
                raise BlotterError("init and verify are local administrator operations")
            bound_actor = args.identity or getattr(args, "actor", None)
            if not bound_actor:
                raise BlotterError("--identity is required for remote list and export")
            if args.command in ("check", "record") and args.identity and args.identity != args.actor:
                raise BlotterError("--identity must equal --actor on remote writes/checks")
            ledger = RemoteBlotter(args.url, actor=bound_actor,
                                  token=os.environ.get("BLOTTER_TOKEN", ""))
        else:
            ledger = Blotter(Path(args.db))
        if args.command == "init":
            result = {"ok": True, "path": str(ledger.path)}
        elif args.command == "check":
            if args.url:
                result = ledger.check(session=args.session, after_seq=args.after_seq, limit=args.limit)
            else:
                result = ledger.check(actor=args.actor, session=args.session,
                                      after_seq=args.after_seq, limit=args.limit)
        elif args.command == "record":
            kwargs = {
                "kind": args.kind, "message": args.message,
                "level": args.level, "evidence": args.evidence,
                "session": args.session, "effect": args.effect,
                "payload": json.loads(args.payload), "event_id": args.event_id,
                "correlation_id": args.correlation_id, "occurred_at": args.occurred_at,
                "source_ids": args.source_id,
            }
            if not args.url:
                kwargs["actor"] = args.actor
            result = ledger.record(**kwargs)
        elif args.command == "list":
            result = ledger.list(actor=args.actor, kind=args.kind, session=args.session,
                                 after_seq=args.after_seq, limit=args.limit)
        elif args.command == "verify":
            result = ledger.verify()
        elif args.command == "export":
            if not args.url:
                for line in ledger.export(after_seq=args.after_seq):
                    print(line)
            else:
                cursor = args.after_seq
                while True:
                    batch = ledger.list(after_seq=cursor, limit=100)
                    for item in batch:
                        print(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                    if not batch:
                        break
                    new_cursor = batch[-1]["seq"]
                    if new_cursor <= cursor:
                        raise BlotterError("remote export sequence did not advance")
                    cursor = new_cursor
                    if len(batch) < 100:
                        break
            return 0
        else:
            parser.error("unknown command")
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (BlotterError, IntegrityError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"blotter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
