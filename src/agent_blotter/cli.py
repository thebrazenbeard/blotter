"""Command line interface for the local Blotter ledger."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .store import Blotter, BlotterError, IntegrityError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blotter", description="Local agent activity and knowledge ledger")
    parser.add_argument("--db", default=os.environ.get("BLOTTER_DB", ".blotter/blotter.sqlite3"),
                        help="local SQLite path; default .blotter/blotter.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the local store")
    record = commands.add_parser("record", help="append an agent activity")
    record.add_argument("--actor", required=True)
    record.add_argument("--kind", required=True)
    record.add_argument("--message", required=True)
    record.add_argument("--session", default="default")
    record.add_argument("--level", default="INFO")
    record.add_argument("--evidence", default="UNKNOWN")
    record.add_argument("--effect", default="NONE")
    record.add_argument("--payload", default="{}", help="JSON object (never pass secrets)")
    record.add_argument("--event-id")
    record.add_argument("--correlation-id")
    record.add_argument("--occurred-at")
    record.add_argument("--source-id", action="append", default=[])
    promote = commands.add_parser("promote", help="promote observed events to knowledge")
    promote.add_argument("--actor", required=True)
    promote.add_argument("--message", required=True)
    promote.add_argument("--session", default="default")
    promote.add_argument("--source-id", action="append", required=True)
    promote.add_argument("--payload", default="{}")
    promote.add_argument("--event-id")
    listing = commands.add_parser("list", help="list records in ascending sequence order")
    listing.add_argument("--actor")
    listing.add_argument("--kind")
    listing.add_argument("--session")
    listing.add_argument("--after-seq", type=int, default=0)
    listing.add_argument("--limit", type=int, default=100)
    knowledge = commands.add_parser("knowledge", help="list explicitly promoted knowledge")
    knowledge.add_argument("--after-seq", type=int, default=0)
    knowledge.add_argument("--limit", type=int, default=100)
    exported = commands.add_parser("export", help="emit canonical event bodies as JSONL")
    exported.add_argument("--after-seq", type=int, default=0)
    commands.add_parser("verify", help="check sequence and hash-chain integrity")
    args = parser.parse_args(argv)
    try:
        ledger = Blotter(Path(args.db))
        if args.command == "init":
            result = {"ok": True, "path": str(ledger.path)}
        elif args.command == "record":
            result = ledger.record(actor=args.actor, kind=args.kind, message=args.message,
                level=args.level, evidence=args.evidence, session=args.session,
                effect=args.effect, payload=json.loads(args.payload), event_id=args.event_id,
                correlation_id=args.correlation_id, occurred_at=args.occurred_at,
                source_ids=args.source_id)
        elif args.command == "promote":
            result = ledger.promote(actor=args.actor, message=args.message,
                source_ids=args.source_id, session=args.session,
                payload=json.loads(args.payload), event_id=args.event_id)
        elif args.command == "list":
            result = ledger.list(actor=args.actor, kind=args.kind, session=args.session,
                after_seq=args.after_seq, limit=args.limit)
        elif args.command == "knowledge":
            result = ledger.knowledge(after_seq=args.after_seq, limit=args.limit)
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
