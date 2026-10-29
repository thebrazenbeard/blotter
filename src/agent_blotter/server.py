"""One authenticated HTTP front door onto ONE shared Blotter database.

The API transfers ledger records; it is not a message router, inbox, or agent command plane.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import math
import os
import ssl
import stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store import Blotter, BlotterError, SCHEMA_VERSION, _valid_id

MAX_HTTP_BODY = 70000
MAX_HTTP_PAGE = 100
DEFAULT_REQUEST_TIMEOUT = 15.0
WRITE_FIELDS = frozenset(("kind", "message", "session", "level", "evidence",
                          "effect", "payload", "source_ids", "event_id",
                          "correlation_id", "occurred_at"))
READ_FIELDS = frozenset(("after_seq", "limit", "actor", "kind", "session"))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    """Reject ambiguous JSON keys at every nesting level before admission.

    A duplicate key must not silently replace an earlier value, especially in
    credential-bound audit records. Do not echo a potentially sensitive key.
    """
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise BlotterError("duplicate JSON object key")
        obj[key] = value
    return obj


class _BlotterHTTPServer(ThreadingHTTPServer):
    """Perform optional TLS handshakes inside client workers, not accept().

    Wrapping a listening socket makes SSLSocket.accept() negotiate TLS before
    ThreadingMixIn starts a worker. One silent peer could then block every
    subsequent connection for the handshake timeout interval.
    """

    tls_context: ssl.SSLContext | None = None
    tls_handshake_timeout: float = DEFAULT_REQUEST_TIMEOUT

    def finish_request(self, request, client_address) -> None:
        if self.tls_context is None:
            super().finish_request(request, client_address)
            return
        request.settimeout(self.tls_handshake_timeout)
        try:
            tls_request = self.tls_context.wrap_socket(request, server_side=True)
        except OSError:
            # Failed handshakes are unauthenticated connection failures.
            return
        try:
            super().finish_request(tls_request, client_address)
        finally:
            tls_request.close()


def _check_credentials_file_permissions(path: Path, *, platform: str | None = None) -> None:
    """Reject credential files whose permissions cannot be proven private.

    MCP actor tokens and this server's actor-to-token JSON must obey the same
    fail-closed Windows SID rule. A successful POSIX chmod is not a Windows ACL.
    """
    if path.is_symlink():
        raise BlotterError("credentials file permissions are unsafe")
    if platform is None:
        platform = os.name
    if platform == "nt":
        # Reuse the established SID-based check, not localized icacls text.
        from .mcp_stdio import _check_windows_token_acl
        try:
            _check_windows_token_acl(path)
        except (ValueError, OSError) as exc:
            raise BlotterError("credentials file permissions could not be verified") from exc
    elif stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BlotterError("credentials file must not be accessible by group or others")


def load_credentials(path: str | Path) -> dict[str, str]:
    """Return token -> actor mapping from an owner-protected JSON file.

    Input format: {"agent.one": "<random >=32 char token>", ...}
    """
    path = Path(path)
    if not path.is_file():
        raise BlotterError("credentials file not found")
    _check_credentials_file_permissions(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"),
                          object_pairs_hook=_unique_json_object)
    except (ValueError, UnicodeError) as exc:
        raise BlotterError("invalid credentials JSON") from exc
    if not isinstance(data, dict) or not data:
        raise BlotterError("credentials must be a nonempty actor-to-token object")
    tokens = {}
    for actor, token in data.items():
        _valid_id(actor, "actor")
        if not isinstance(token, str) or not 32 <= len(token) <= 4096:
            raise BlotterError("each token must contain at least 32 characters")
        if token in tokens:
            raise BlotterError("duplicate credential token")
        tokens[token] = actor
    return tokens


def create_server(
    ledger: Blotter, credentials: dict[str, str], host: str = "127.0.0.1",
    port: int = 8832, *, certfile: str | None = None, keyfile: str | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> ThreadingHTTPServer:
    """Create a bound service. Non-loopback binds require server TLS."""
    if not isinstance(credentials, dict) or not credentials:
        raise BlotterError("at least one authenticated agent is required")
    seen_actors = set()
    for token, actor in credentials.items():
        if not isinstance(token, str) or not 32 <= len(token) <= 4096:
            raise BlotterError("each server credential must contain 32-4096 characters")
        _valid_id(actor, "actor")
        if actor in seen_actors:
            raise BlotterError("duplicate actor identity in credentials")
        seen_actors.add(actor)
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if bool(certfile) != bool(keyfile):
        raise BlotterError("TLS certificate and key must be configured together")
    if not loopback and not certfile:
        raise BlotterError("non-loopback HTTP without TLS is forbidden")
    if type(port) is not int or not 0 <= port <= 65535:
        raise BlotterError("invalid server port")
    if (type(request_timeout) not in (int, float) or
            not math.isfinite(request_timeout) or not 0 < request_timeout <= 120):
        raise BlotterError("request timeout must be finite and 0-120 seconds")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AgentBlotter/0.2"

        def setup(self) -> None:
            super().setup()
            # Bound time spent waiting for headers/body on an idle connection.
            self.connection.settimeout(request_timeout)

        def log_message(self, fmt, *args):
            # Do not print tokens or raw client data from HTTP requests.
            return

        def _send(self, status: int, obj: object) -> None:
            payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def _actor(self) -> str | None:
            authorization = self.headers.get_all("Authorization", [])
            if len(authorization) != 1:
                self.close_connection = True
                self._send(401, {"error": "exactly one Authorization header required"})
                return None
            declared_actors = self.headers.get_all("X-Blotter-Actor", [])
            if len(declared_actors) > 1:
                self.close_connection = True
                self._send(403, {"error": "ambiguous credential identity"})
                return None
            header = authorization[0]
            if not header.startswith("Bearer "):
                self.close_connection = True
                self._send(401, {"error": "authentication required"})
                return None
            supplied = header[7:]
            for token, actor in credentials.items():
                if hmac.compare_digest(supplied, token):
                    expected = declared_actors[0] if declared_actors else None
                    if expected is not None and expected != actor:
                        self.close_connection = True
                        self._send(403, {"error": "credential identity mismatch"})
                        return None
                    return actor
            self.close_connection = True
            self._send(403, {"error": "invalid credentials"})
            return None

        def _body(self) -> dict:
            if self.headers.get("Transfer-Encoding") is not None:
                raise BlotterError("Transfer-Encoding is unsupported")
            if len(self.headers.get_all("Content-Length", [])) != 1:
                raise BlotterError("exactly one Content-Length is required")
            rawlen = self.headers.get("Content-Length")
            if rawlen is None or not rawlen.isdigit():
                raise BlotterError("valid Content-Length required")
            length = int(rawlen)
            if not 1 <= length <= MAX_HTTP_BODY:
                raise BlotterError("request body out of bounds")
            content_types = self.headers.get_all("Content-Type", [])
            if (len(content_types) != 1 or
                    content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
                raise BlotterError("Content-Type must be application/json")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise BlotterError("incomplete request body")
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
            if not isinstance(value, dict):
                raise BlotterError("request body must be a JSON object")
            return value

        def do_POST(self) -> None:
            actor = self._actor()
            if actor is None:
                return
            route = urlsplit(self.path).path
            if route not in ("/v1/check", "/v1/record"):
                # The body has not been consumed; never parse it as a new request.
                self.close_connection = True
                self._send(404, {"error": "route not found"})
                return
            try:
                obj = self._body()
                if route == "/v1/check":
                    if set(obj) - {"session", "after_seq", "limit", "expected_after_hash"}:
                        raise BlotterError("unknown check fields")
                    if type(obj.get("after_seq", 0)) is not int or type(obj.get("limit", 100)) is not int:
                        raise BlotterError("cursor and page size must be integers")
                    result = ledger.check(actor=actor, session=obj.get("session", "default"),
                                          after_seq=obj.get("after_seq", 0),
                                          limit=min(obj.get("limit", 100), MAX_HTTP_PAGE),
                                          expected_after_hash=obj.get("expected_after_hash"))
                else:
                    if set(obj) - WRITE_FIELDS:
                        raise BlotterError("unknown activity fields, actor is credential-bound")
                    if "kind" not in obj or "message" not in obj:
                        raise BlotterError("kind and message required")
                    if "source_ids" in obj and not isinstance(obj["source_ids"], list):
                        raise BlotterError("source_ids must be a JSON array")
                    result = ledger.record(actor=actor, **obj)
                self._send(200, result)
            except (BlotterError, ValueError, TypeError, UnicodeError) as exc:
                self.close_connection = True
                self._send(422, {"error": str(exc)})
            except OSError:
                self.close_connection = True
                self._send(503, {"error": "ledger temporarily unavailable"})

        def do_GET(self) -> None:
            actor = self._actor()
            if actor is None:
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") is not None or
                    len(lengths) > 1 or (lengths and lengths[0] != "0")):
                # GET does not read bodies; reject and close rather than allowing
                # unread bytes to become the next HTTP request line.
                self.close_connection = True
                self._send(422, {"error": "GET request bodies are unsupported"})
                return
            parts = urlsplit(self.path)
            if parts.path == "/v1/health":
                self._send(200, {"ok": True, "schema_version": SCHEMA_VERSION})
                return
            if parts.path not in ("/v1/events", "/v1/tail"):
                self._send(404, {"error": "route not found"})
                return
            try:
                # Preserve empty values so `limit=` cannot silently become a
                # missing parameter and `limit=20&limit=` cannot bypass the
                # repeated-field rejection below.
                args = parse_qs(parts.query, strict_parsing=True,
                                keep_blank_values=True)
                if parts.path == "/v1/tail":
                    if set(args) - {"limit"} or any(len(values) != 1 for values in args.values()):
                        raise BlotterError("unknown or repeated tail query fields")
                    limit = int(args.get("limit", ["20"])[0])
                    self._send(200, {"events": ledger.tail(limit=limit)})
                    return
                if set(args) - READ_FIELDS or any(len(values) != 1 for values in args.values()):
                    raise BlotterError("unknown or repeated query fields")
                after_seq = int(args.get("after_seq", ["0"])[0])
                limit = min(int(args.get("limit", ["100"])[0]), MAX_HTTP_PAGE)
                result = ledger.list(after_seq=after_seq, limit=limit,
                                     actor=args.get("actor", [None])[0],
                                     kind=args.get("kind", [None])[0],
                                     session=args.get("session", [None])[0])
                self._send(200, {"events": result})
            except (BlotterError, ValueError, TypeError) as exc:
                self._send(422, {"error": str(exc)})
            except OSError:
                self._send(503, {"error": "ledger temporarily unavailable"})

    result = _BlotterHTTPServer((host, port), Handler)
    result.daemon_threads = True
    if certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(certfile=certfile, keyfile=keyfile)
            result.tls_context = context
            result.tls_handshake_timeout = request_timeout
        except BaseException:
            result.server_close()
            raise
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blotter-serve", description="Serve one shared agent activity ledger")
    parser.add_argument("--db", default=os.environ.get(
        "BLOTTER_DB", str(Path.home() / ".blotter" / "activity.sqlite3")))
    parser.add_argument("--credentials", required=True,
                        help="private JSON mapping authenticated agent IDs to random tokens")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8832)
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
                        help="maximum idle seconds while reading each HTTP request (default 15)")
    args = parser.parse_args(argv)
    try:
        ledger = Blotter(args.db)
        credentials = load_credentials(args.credentials)
        server = create_server(ledger, credentials, host=args.host, port=args.port,
                               certfile=args.tls_cert, keyfile=args.tls_key,
                               request_timeout=args.request_timeout)
        print(f"Blotter ledger listening on {args.host}:{args.port}", flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (BlotterError, OSError, ssl.SSLError) as exc:
        parser.exit(2, f"blotter-serve: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
