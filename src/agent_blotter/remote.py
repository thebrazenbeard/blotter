"""Authenticated Python client for a central Blotter; no message delivery API."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .store import BlotterError, _valid_id


class RemoteBlotter:
    """Connect a credential-bound agent to one shared Blotter server."""

    def __init__(self, url: str, *, actor: str, token: str, timeout: float = 10.0):
        self.actor = _valid_id(actor, "actor")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BlotterError("invalid Blotter URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise BlotterError("URL must not contain credentials, query, fragment or path")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise BlotterError("remote Blotter requires HTTPS")
        if not isinstance(token, str) or len(token) < 32:
            raise BlotterError("missing or invalid Blotter token")
        if timeout <= 0:
            raise BlotterError("timeout must be positive")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, route: str, data: dict[str, Any] | None = None) -> dict:
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + route, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as reply:
                return json.load(reply)
        except urllib.error.HTTPError as exc:
            try:
                reason = json.loads(exc.read().decode("utf-8")).get("error", "request rejected")
            except (ValueError, UnicodeError):
                reason = "request rejected"
            raise BlotterError(f"server rejected request ({exc.code}): {reason}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            # POST effect is ambiguous on disconnect. The caller must reconcile by ID.
            raise BlotterError("connection failed; write outcome may be unknown: reconcile using event_id") from exc

    def check(self, *, session: str = "default", after_seq: int = 0,
              limit: int = 100) -> dict:
        return self._request("/v1/check", {
            "session": session, "after_seq": after_seq, "limit": limit})

    def record(self, *, kind: str, message: str, session: str = "default",
               payload: dict | None = None, level: str = "INFO",
               evidence: str = "UNKNOWN", effect: str = "NONE",
               source_ids: list[str] | None = None, event_id: str | None = None,
               correlation_id: str | None = None, occurred_at: str | None = None) -> dict:
        return self._request("/v1/record", {
            "kind": kind, "message": message, "session": session, "payload": payload or {},
            "level": level, "evidence": evidence, "effect": effect,
            "source_ids": source_ids or [], "event_id": event_id,
            "correlation_id": correlation_id, "occurred_at": occurred_at,
        })

    def list(self, *, after_seq: int = 0, limit: int = 100,
             actor: str | None = None, kind: str | None = None,
             session: str | None = None) -> list[dict]:
        params = {"after_seq": after_seq, "limit": limit, "actor": actor,
                  "kind": kind, "session": session}
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self._request("/v1/events?" + query)["events"]

