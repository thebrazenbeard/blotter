"""Read-only by default diagnostic of ONE remote Blotter ledger.

This command never opens a local SQLite database. It reports only capability
and sequence metadata, never bearer tokens or private ledger event bodies.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
from pathlib import Path
import stat
import urllib.error
import urllib.parse

from .remote import RemoteBlotter
from .store import BlotterError, _valid_id


def _emit(status: str, *, stage: str, **fields: object) -> int:
    print(json.dumps({"status": status, "stage": stage, **fields},
                     sort_keys=True, ensure_ascii=True, separators=(",", ":")))
    return 0 if status == "OK" else 2


def _error_code(exc: BaseException, *, stage: str) -> str:
    """Classify a result without propagating exception text or credentials."""
    cause = exc.__cause__
    if isinstance(cause, urllib.error.HTTPError):
        if cause.code == 401:
            return "AUTHENTICATION_FAILED"
        if cause.code == 403:
            return "AUTHORIZATION_FAILED"
        if cause.code == 404:
            return "CONTRACT_UNAVAILABLE"
        return "READ_FAILED" if stage != "append" else "APPEND_FAILED"
    if isinstance(cause, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return "CONNECTIVITY_FAILED" if stage != "append" else "APPEND_OUTCOME_UNKNOWN"
    return "READ_FAILED" if stage != "append" else "APPEND_FAILED"


def _read_private_token(path: Path) -> str:
    """Use an owner-only, single-link regular file; fail closed on Windows.

    The stdlib has no reliable Windows DACL inspection. Windows operators use
    a process-scoped BLOTTER_TOKEN supplied by their credential manager until
    an audited Windows file loader is available. POSIX O_NOFOLLOW and fstat
    bind the permission check to the file descriptor actually being read.
    """
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise PermissionError("token-file ACL cannot be verified on this OS")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PermissionError("token-file cannot be a symlink") from exc
        raise
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise PermissionError("token-file is not private")
        with os.fdopen(fd, "r", encoding="utf-8") as opened:
            fd = -1
            return opened.read(4097).strip()
    finally:
        if fd >= 0:
            os.close(fd)


def _target_trusted(url: str) -> bool:
    """Require an operator provisioned BLOTTER_URL for non-loopback hosts.

    --url by itself cannot retarget a remote bearer to an arbitrary host.
    This is a configuration guard, not protection if the caller can alter the
    process environment or its credential supply.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    expected = os.environ.get("BLOTTER_URL", "").rstrip("/")
    if expected:
        return url.rstrip("/") == expected
    return parsed.hostname in ("127.0.0.1", "::1", "localhost")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blotter-probe",
        description="Fail-closed remote-only Blotter reachability and read/write diagnostic")
    parser.add_argument("--url", default=os.environ.get("BLOTTER_URL"))
    parser.add_argument("--actor", required=True)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--append-test", action="store_true",
                        help="explicitly append and read back one idempotent test event")
    parser.add_argument("--event-id", help="required unique idempotency key for --append-test")
    parser.add_argument("--session", default="remote-probe")
    args = parser.parse_args(argv)

    if not args.url or not math.isfinite(args.timeout) or args.timeout <= 0:
        return _emit("CONFIG_MISSING", stage="configuration")
    if not _target_trusted(args.url):
        return _emit("CONFIG_TARGET_UNTRUSTED", stage="configuration")
    if (args.append_test and not args.event_id) or (args.event_id and not args.append_test):
        return _emit("CONFIG_MISSING", stage="configuration")
    try:
        _valid_id(args.actor, "actor")
        _valid_id(args.session, "session")
        if args.event_id:
            _valid_id(args.event_id, "event_id")
    except BlotterError:
        return _emit("CONFIG_MISSING", stage="configuration")

    if args.token_file:
        try:
            token = _read_private_token(args.token_file)
        except PermissionError:
            return _emit("CONFIG_PERMISSION_DENIED", stage="configuration")
        except (OSError, UnicodeError):
            return _emit("CONFIG_MISSING", stage="configuration")
    else:
        token = os.environ.get("BLOTTER_TOKEN", "")
    if (not isinstance(token, str) or len(token) > 512 or
            any(ord(char) < 33 or ord(char) > 126 for char in token)):
        return _emit("CONFIG_MISSING", stage="configuration")
    try:
        client = RemoteBlotter(args.url, actor=args.actor, token=token,
                              timeout=args.timeout, direct_only=True)
    except (BlotterError, ValueError, TypeError):
        return _emit("CONFIG_MISSING", stage="configuration")
    finally:
        token = ""  # No bearer is included in diagnostic output.

    try:
        health = client._request("/v1/health")
        if not isinstance(health, dict) or health.get("ok") is not True:
            return _emit("HEALTH_FAILED", stage="health")
    except (BlotterError, ValueError, KeyError, TypeError) as exc:
        return _emit(_error_code(exc, stage="health"), stage="health")

    try:
        sample = client.list(after_seq=0, limit=1)
        if (not isinstance(sample, list) or len(sample) > 1 or
                any(not isinstance(event, dict) or
                    type(event.get("seq")) is not int or event["seq"] < 1
                    for event in sample)):
            return _emit("READ_FAILED", stage="read")
    except (BlotterError, ValueError, KeyError, TypeError) as exc:
        return _emit(_error_code(exc, stage="read"), stage="read")

    tail_status = "UNKNOWN"
    try:
        tail = client._request("/v1/tail?limit=20")
        recent = tail.get("events") if isinstance(tail, dict) else None
        tail_status = ("AVAILABLE" if isinstance(recent, list) and
                       len(recent) <= 20 and all(
                           isinstance(event, dict) and
                           type(event.get("seq")) is int and event["seq"] > 0
                           for event in recent) else "INVALID_RESPONSE")
    except (BlotterError, ValueError, KeyError, TypeError) as exc:
        tail_status = ("NOT_DEPLOYED" if _error_code(exc, stage="tail") ==
                       "CONTRACT_UNAVAILABLE" else "UNAVAILABLE")

    fields: dict[str, object] = {"health": "VERIFIED", "read": "VERIFIED",
                                  "tail": tail_status, "append": "NOT_ATTEMPTED"}
    if not args.append_test:
        return _emit("OK", stage="read", **fields)

    message = "Explicit remote Blotter probe (idempotent, readback required)"
    fields["append"] = "ATTEMPTED_UNCONFIRMED"
    try:
        first = client.record(kind="test.executed", message=message,
                              session=args.session, event_id=args.event_id,
                              evidence="OBSERVATION", effect="ATTEMPTED")
        repeated = client.record(kind="test.executed", message=message,
                                 session=args.session, event_id=args.event_id,
                                 evidence="OBSERVATION", effect="ATTEMPTED")
    except (BlotterError, ValueError, KeyError, TypeError) as exc:
        return _emit(_error_code(exc, stage="append"), stage="append", **fields)

    seq = first.get("seq") if isinstance(first, dict) else None
    if (type(seq) is not int or seq <= 0 or first.get("id") != args.event_id or
            not isinstance(repeated, dict) or repeated.get("seq") != seq or
            repeated.get("id") != args.event_id):
        return _emit("VERIFICATION_FAILED", stage="idempotency", **fields)
    try:
        after = client.list(after_seq=seq - 1, limit=1)
    except (BlotterError, ValueError, KeyError, TypeError):
        return _emit("VERIFICATION_FAILED", stage="readback", **fields)
    if (not isinstance(after, list) or len(after) != 1 or
            not isinstance(after[0], dict) or after[0].get("seq") != seq or
            after[0].get("id") != args.event_id or after[0].get("actor") != args.actor):
        return _emit("VERIFICATION_FAILED", stage="readback", **fields)
    fields["append"] = "VERIFIED"
    fields["append_seq"] = seq
    return _emit("OK", stage="readback", **fields)


if __name__ == "__main__":
    raise SystemExit(main())
