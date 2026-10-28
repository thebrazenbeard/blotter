"""Authenticated Python client for a central Blotter; no message delivery API."""
from __future__ import annotations

import json
import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .store import BlotterError, MAX_EVENT_BYTES, _valid_id


# A page contains at most 100 independently bounded 64 KiB events. Leave an
# additional 64 KiB for metadata and JSON framing, and use much smaller limits
# for non-page endpoints. Never trust Content-Length as a complete bound.
_PAGE_OVERHEAD_BYTES = 65536
_HEALTH_RESPONSE_BYTES = 16384
_ERROR_RESPONSE_BYTES = 8192


def _response_budget(route: str, data: dict[str, Any] | None) -> int:
    path, _, query = route.partition("?")
    if path == "/v1/health":
        return _HEALTH_RESPONSE_BYTES
    if path == "/v1/record":
        return MAX_EVENT_BYTES + _PAGE_OVERHEAD_BYTES
    if path in ("/v1/tail", "/v1/events", "/v1/check"):
        if path == "/v1/check":
            raw_limit = (data or {}).get("limit", 100)
        else:
            params = urllib.parse.parse_qs(query)
            raw_limit = params.get("limit", [20 if path == "/v1/tail" else 100])[0]
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 100
        # Invalid/unbounded requests are rejected by the server; the read
        # budget must still be bounded even if the server is compromised.
        return min(100, max(1, limit)) * MAX_EVENT_BYTES + _PAGE_OVERHEAD_BYTES
    return _HEALTH_RESPONSE_BYTES


def _read_bounded(reply: Any, maximum: int) -> bytes:
    """Read no more than maximum+1 bytes before decoding untrusted JSON."""
    content = reply.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError("response exceeds safe size")
    return content


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward authenticated requests to a redirected URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())
_DIRECT_NO_REDIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect())


class RemoteBlotter:
    """Connect a credential-bound agent to one shared Blotter server."""

    def __init__(self, url: str, *, actor: str, token: str, timeout: float = 10.0,
                 direct_only: bool = False):
        self.actor = _valid_id(actor, "actor")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BlotterError("invalid Blotter URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BlotterError("URL must not contain credentials, query or fragment")
        # An authenticated HTTPS proxy can mount this API at a safe URL prefix.
        import re
        if (not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*/?", parsed.path)
                or any(x in (".", "..") for x in parsed.path.split("/"))):
            raise BlotterError("unsafe Blotter URL path")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise BlotterError("remote Blotter requires HTTPS")
        if not isinstance(token, str) or len(token) < 32:
            raise BlotterError("missing or invalid Blotter token")
        if timeout <= 0:
            raise BlotterError("timeout must be positive")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Diagnostics for private services must not inherit HTTPS_PROXY.
        # Existing general clients retain their prior explicit-proxy behavior.
        self._opener = (_DIRECT_NO_REDIRECT_OPENER if direct_only
                        else _NO_REDIRECT_OPENER)

    def _request(self, route: str, data: dict[str, Any] | None = None) -> dict:
        headers = {"Authorization": "Bearer " + self.token,
                   "X-Blotter-Actor": self.actor, "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + route, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with self._opener.open(req, timeout=self.timeout) as reply:
                try:
                    content = _read_bounded(reply, _response_budget(route, data))
                    result = json.loads(content.decode("utf-8"))
                    if not isinstance(result, dict):
                        raise ValueError("response is not a JSON object")
                    return result
                except (ValueError, UnicodeError, RecursionError) as exc:
                    # The server may have committed the POST before the
                    # malformed/oversized response arrived. Never relabel it
                    # as a definite rejection and risk a duplicate write.
                    if body is not None:
                        raise BlotterError(
                            "invalid or oversized response; write outcome may be unknown: "
                            "reconcile using event_id") from exc
                    raise BlotterError(
                        "invalid or oversized response; no read result verified: "
                        + str(exc)) from exc
        except urllib.error.HTTPError as exc:
            try:
                try:
                    details = json.loads(_read_bounded(
                        exc, _ERROR_RESPONSE_BYTES).decode("utf-8"))
                    reason = details.get("error", "request rejected") if isinstance(
                        details, dict) else "request rejected"
                    if not isinstance(reason, str):
                        reason = "request rejected"
                    reason = reason[:256]
                except (ValueError, UnicodeError, RecursionError, OSError,
                        http.client.HTTPException):
                    reason = "request rejected"
            finally:
                exc.close()
            # A reverse proxy can emit 5xx after the upstream has committed
            # a POST. Treat it as unknown, never a definite rejection.
            if body is not None and exc.code >= 500:
                raise BlotterError(
                    f"server/upstream error ({exc.code}); write outcome may be unknown: "
                    "reconcile using event_id") from exc
            raise BlotterError(f"server rejected request ({exc.code}): {reason}") from exc
        except urllib.error.URLError as exc:
            # DNS failure proves the request never reached the server, so even a
            # POST cannot have committed. Do not report a false ambiguous write.
            reason = exc.reason
            reason_text = str(reason).lower()
            dns_failure = (
                isinstance(reason, socket.gaierror)
                or (isinstance(reason, OSError) and
                    getattr(reason, "winerror", None) in (11001, 11002, 11003))
                or "getaddrinfo failed" in reason_text
                or "name or service not known" in reason_text
                or "no such host is known" in reason_text
            )
            if dns_failure:
                raise BlotterError(
                    "Blotter hostname resolution failed; verify private-network DNS/Tailscale connectivity"
                ) from exc
            if body is not None:
                raise BlotterError(
                    "connection failed; write outcome may be unknown: reconcile using event_id"
                ) from exc
            raise BlotterError(
                "connection failed before a response; no read result was verified"
            ) from exc
        except (TimeoutError, ConnectionError, OSError, http.client.HTTPException) as exc:
            if body is not None:
                raise BlotterError(
                    "connection failed; write outcome may be unknown: reconcile using event_id"
                ) from exc
            raise BlotterError(
                "connection failed before a response; no read result was verified"
            ) from exc

    def health(self) -> dict:
        """Authenticated, read-only health check of the configured service."""
        return self._request("/v1/health")

    def check(self, *, session: str = "default", after_seq: int = 0,
              limit: int = 100, expected_after_hash: str | None = None) -> dict:
        return self._request("/v1/check", {
            "session": session, "after_seq": after_seq, "limit": limit,
            "expected_after_hash": expected_after_hash})

    def record(self, *, kind: str, message: str, session: str = "default",
               payload: dict | None = None, level: str = "INFO",
               evidence: str = "UNKNOWN", effect: str = "NONE",
               source_ids: list[str] | None = None, event_id: str | None = None,
               correlation_id: str | None = None, occurred_at: str | None = None) -> dict:
        if source_ids is not None and not isinstance(source_ids, list):
            raise BlotterError("source_ids must be a list")
        return self._request("/v1/record", {
            "kind": kind, "message": message, "session": session,
            "payload": {} if payload is None else payload,
            "level": level, "evidence": evidence, "effect": effect,
            "source_ids": [] if source_ids is None else source_ids, "event_id": event_id,
            "correlation_id": correlation_id, "occurred_at": occurred_at,
        })

    def list(self, *, after_seq: int = 0, limit: int = 100,
             actor: str | None = None, kind: str | None = None,
             session: str | None = None) -> list[dict]:
        params = {"after_seq": after_seq, "limit": limit, "actor": actor,
                  "kind": kind, "session": session}
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self._request("/v1/events?" + query)["events"]

    def tail(self, *, limit: int = 20) -> list[dict]:
        """Authenticated recent global history view (not a turn check)."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BlotterError("tail limit must be 1-100")
        return self._request("/v1/tail?" + urllib.parse.urlencode({"limit": limit}))["events"]
