"""Blotter: read the same lane each turn, write every activity into it."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .store import Blotter, BlotterError, IntegrityError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blotter", description="One shared chronological agent activity blotter")
    parser.add_argument("--db", default=os.environ.get("BLOTTER_DB", str(Path.home() / ".blotter" / "activity.sqlite3")),
                        help="shared local SQLite file (default ~/.blotter/activity.sqlite3; override BLOTTER_DB)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the shared activity ledger")
    check = commands.add_parser("check", help="read activity since last turn and log the check")
    check.add_argument("--actor", required=True)
    check.add_argument("--session", default="default")
    check.add_argument("--after-seq", type=int, default=0)
    check.add_argument("--limit", type=int, default=100)
    record = commands.add_parser("record", help="append an activity to the one shared lane")
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
    listing = commands.add_parser("list", help="read the shared activity history")
    listing.add_argument("--actor")
    listing.add_argument("--kind")
    listing.add_argument("--session")
    listing.add_argument("--after-seq", type=int, default=0)
    listing.add_argument("--limit", type=int, default=100)
    exported = commands.add_parser("export", help="export shared history as JSONL")
    exported.add_argument("--after-seq", type=int, default=0)
    commands.add_parser("verify", help="verify local sequence and hash integrity")
    args = parser.parse_args(argv)
    try:
        ledger = Blotter(Path(args.db))
        if args.command == "init":
            result = {"ok": True, "path": str(ledger.path)}
        elif args.command == "check":
            result = ledger.check(actor=args.actor, session=args.session,
                                  after_seq=args.after_seq, limit=args.limit)
        elif args.command == "record":
            result = ledger.record(actor=args.actor, kind=args.kind, message=args.message,
                level=args.level, evidence=args.evidence, session=args.session,
                effect=args.effect, payload=json.loads(args.payload), event_id=args.event_id,
                correlation_id=args.correlation_id, occurred_at=args.occurred_at,
                source_ids=args.source_id)
        elif args.command == "list":
            result = ledger.list(actor=args.actor, kind=args.kind, session=args.session,
                after_seq=args.after_seq, limit=args.limit)
        elif args.command == "verify":
            result = ledger.verify()
        elif args.command == "export":
            for line in ledger.export(after_seq=args.after_seq):
                print(line)
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
