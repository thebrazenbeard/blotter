"""Read-only Blotter connectivity diagnostics with an explicit opt-in write probe."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from .mcp_stdio import _load_token_file, _validate_config_url
from .remote import RemoteBlotter
from .store import BlotterError, _valid_id


DEFAULT_URL = "https://lappy.tail86ea75.ts.net/blotter"


def _failure_code(exc: Exception, phase: str) -> str:
    """Return a stable, secret-free failure code for a diagnostic phase."""
    message = str(exc).lower()
    if "server rejected request (401)" in message:
        return "AUTHENTICATION_FAILED"
    if "server rejected request (403)" in message:
        if "invalid credentials" in message:
            return "AUTHENTICATION_FAILED"
        return "IDENTITY_OR_PERMISSION_REJECTED"
    if "server rejected request (404)" in message:
        return "TAIL_ENDPOINT_MISSING" if phase == "latest20_read" else "API_ENDPOINT_MISSING"
    if "server rejected request (422)" in message:
        return "CURSOR_CONFLICT" if "history mismatch" in message or "cursor" in message else "REQUEST_REJECTED"
    if "server rejected request (503)" in message:
        return "SERVICE_UNAVAILABLE"
    if "hostname resolution failed" in message or "dns resolution failed" in message:
        return "NETWORK_DNS_FAILURE"
    if "connection failed" in message or "outcome may be unknown" in message:
        return "CONNECTION_FAILED"
    if isinstance(exc, BlotterError):
        return "REQUEST_FAILED"
    return "INVALID_RESPONSE"


def diagnose(client: RemoteBlotter, *, test_append: bool = False) -> dict:
    """Probe authenticated health and recent-read endpoints without mutation.

    A record is written only when test_append=True. The event ID is included in
    the report and is used for exact read-after-write verification. A write
    without a receipt is never retried automatically.
    """
    report = {
        "ok": False,
        "checks": {
            "authenticated_health": {"status": "not_run"},
            "latest20_read": {"status": "not_run"},
            "append_test": {"status": "not_requested" if not test_append else "not_run"},
        },
        "writes_attempted": 0,
        "writes_confirmed": 0,
    }

    try:
        health = client.health()
    except Exception as exc:
        report["checks"]["authenticated_health"] = {
            "status": "failed", "code": _failure_code(exc, "authenticated_health")}
        report["diagnosis"] = "The authenticated health request did not succeed."
        if report["checks"]["authenticated_health"]["code"] == "NETWORK_DNS_FAILURE":
            report["diagnosis"] = (
                "The Blotter hostname did not resolve. Check that this host is connected to "
                "the authorized private network and that its DNS client is active; no write was attempted.")
        return report

    if not isinstance(health, dict) or health.get("ok") is not True:
        report["checks"]["authenticated_health"] = {
            "status": "failed", "code": "INVALID_HEALTH_RESPONSE"}
        report["diagnosis"] = "The server response did not match the expected health schema."
        return report

    report["checks"]["authenticated_health"] = {
        "status": "passed", "schema_version": health.get("schema_version")}

    try:
        events = client.tail(limit=1)
        if (not isinstance(events, list) or len(events) > 1 or
                any(not isinstance(event, dict) or type(event.get("seq")) is not int
                    or event["seq"] < 1 for event in events)):
            raise ValueError("invalid recent-history response")
        report["checks"]["latest20_read"] = {
            "status": "passed",
            "visible_events": len(events),
            "head_seq": events[-1]["seq"] if events else 0,
        }
    except Exception as exc:
        report["checks"]["latest20_read"] = {
            "status": "failed", "code": _failure_code(exc, "latest20_read")}
        report["diagnosis"] = (
            "Authentication/health passed, but the bounded recent-history endpoint did not. "
            "No unbounded history scan was attempted.")

    if test_append:
        event_id = "doctor-" + uuid4().hex
        report["writes_attempted"] = 1
        try:
            written = client.record(
                kind="diagnostic.probe",
                message="Explicit Blotter diagnostic append/read-back probe",
                session="blotter-doctor",
                evidence="OBSERVATION",
                effect="NONE",
                payload={"diagnostic_probe": True},
                event_id=event_id,
            )
        except Exception as exc:
            code = _failure_code(exc, "append_test")
            status = "outcome_unknown" if code in {
                "CONNECTION_FAILED", "SERVICE_UNAVAILABLE"} else "failed"
            report["checks"]["append_test"] = {
                "status": status, "code": code, "event_id": event_id}
            report["diagnosis"] = (
                "The append did not return a verified receipt. Do not retry with a new event ID; "
                "reconcile this event ID against the ledger.")
        else:
            seq = written.get("seq") if isinstance(written, dict) else None
            if type(seq) is not int or seq < 1 or written.get("id") != event_id:
                report["checks"]["append_test"] = {
                    "status": "verification_failed", "code": "INVALID_APPEND_RECEIPT",
                    "event_id": event_id}
                report["diagnosis"] = "The append response lacked the expected event ID and sequence receipt."
            else:
                try:
                    events = client.list(after_seq=seq - 1, limit=100)
                    found = any(
                        isinstance(event, dict)
                        and event.get("id") == event_id
                        and event.get("seq") == seq
                        for event in events
                    )
                except Exception:
                    found = False
                if found:
                    report["writes_confirmed"] = 1
                    report["checks"]["append_test"] = {
                        "status": "passed", "event_id": event_id, "seq": seq}
                else:
                    report["checks"]["append_test"] = {
                        "status": "verification_failed", "code": "READ_AFTER_WRITE_FAILED",
                        "event_id": event_id, "seq": seq}
                    report["diagnosis"] = (
                        "The service returned an append receipt, but read-after-write did not "
                        "confirm the exact event. Preserve the event ID for reconciliation.")

    append_ok = (not test_append or
                 report["checks"]["append_test"].get("status") == "passed")
    read_ok = report["checks"]["latest20_read"].get("status") == "passed"
    report["ok"] = read_ok and append_ok
    if report["ok"]:
        report["diagnosis"] = (
            "Authenticated health and recent-history reads passed."
            + (" Explicit append/read-back passed." if test_append else
               " No write was attempted."))
    elif "diagnosis" not in report:
        report["diagnosis"] = "One or more diagnostic checks failed; see the per-check codes."
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blotter-doctor",
        description="Diagnose Blotter configuration and read access; writes require --test-append",
    )
    parser.add_argument("--actor", default=os.environ.get("BLOTTER_ACTOR"),
                        help="credential-bound agent identity (or BLOTTER_ACTOR)")
    parser.add_argument("--url", default=os.environ.get("BLOTTER_URL", DEFAULT_URL),
                        help="Blotter service base URL; only the pinned endpoint is allowed")
    parser.add_argument("--token-file", default=os.environ.get("BLOTTER_TOKEN_FILE"),
                        help="private token file path (or BLOTTER_TOKEN_FILE)")
    parser.add_argument("--test-append", action="store_true",
                        help="explicitly append one diagnostic event and verify it by read-after-write")
    args = parser.parse_args(argv)

    if not args.actor or not args.token_file:
        print(json.dumps({
            "ok": False,
            "checks": {"configuration": {"status": "failed", "code": "CONFIGURATION_MISSING"}},
            "diagnosis": "Set the actor and private token-file path; no network request or write was attempted.",
            "writes_attempted": 0,
        }, ensure_ascii=True, sort_keys=True))
        return 2

    try:
        url = _validate_config_url(args.url)
        actor = _valid_id(args.actor, "actor")
        token = _load_token_file(Path(args.token_file))
        # This pinned tailnet destination is private: never inherit HTTPS_PROXY.
        client = RemoteBlotter(url, actor=actor, token=token, direct_only=True)
    except (BlotterError, OSError, ValueError):
        print(json.dumps({
            "ok": False,
            "checks": {"configuration": {"status": "failed", "code": "CONFIGURATION_INVALID"}},
            "diagnosis": "Private configuration could not be validated; credentials and paths were not displayed.",
            "writes_attempted": 0,
        }, ensure_ascii=True, sort_keys=True))
        return 2

    result = diagnose(client, test_append=args.test_append)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
