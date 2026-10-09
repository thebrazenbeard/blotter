"""One authenticated HTTP front door onto ONE shared Blotter database.

The API transfers ledger records; it is not a message router, inbox, or agent command plane.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import ssl
import stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store import Blotter, BlotterError, SCHEMA_VERSION, _valid_id

MAX_HTTP_BODY = 70000
MAX_HTTP_PAGE = 100
WRITE_FIELDS = frozenset(("kind", "message", "session", "level", "evidence",
                          "effect", "payload", "source_ids", "event_id",
                          "correlation_id", "occurred_at"))
READ_FIELDS = frozenset(("after_seq", "limit", "actor", "kind", "session"))


def load_credentials(path: str | Path) -> dict[str, str]:
    """Return token -> actor mapping from an owner-protected JSON file.

    Input format: {"agent.one": "<random >=32 char token>", ...}
    """
    path = Path(path)
    if not path.is_file():
        raise BlotterError("credentials file not found")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BlotterError("credentials file must not be accessible by group or others")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
) -> ThreadingHTTPServer:
    """Create a bound service. Non-loopback binds require server TLS."""
    if not credentials:
        raise BlotterError("at least one authenticated agent is required")
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

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AgentBlotter/0.2"

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
            self.end_headers()
            self.wfile.write(payload)

        def _actor(self) -> str | None:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                self._send(401, {"error": "authentication required"})
                return None
            supplied = header[7:]
            for token, actor in credentials.items():
                if hmac.compare_digest(supplied, token):
                    return actor
            self._send(403, {"error": "invalid credentials"})
            return None

        def _body(self) -> dict:
            rawlen = self.headers.get("Content-Length")
            if rawlen is None or not rawlen.isdigit():
                raise BlotterError("valid Content-Length required")
            length = int(rawlen)
            if not 1 <= length <= MAX_HTTP_BODY:
                raise BlotterError("request body out of bounds")
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                raise BlotterError("Content-Type must be application/json")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise BlotterError("request body must be a JSON object")
            return value

        def do_POST(self) -> None:
            actor = self._actor()
            if actor is None:
                return
            route = urlsplit(self.path).path
            if route not in ("/v1/check", "/v1/record"):
                self._send(404, {"error": "route not found"})
                return
            try:
                obj = self._body()
                if route == "/v1/check":
                    if set(obj) - {"session", "after_seq", "limit"}:
                        raise BlotterError("unknown check fields")
                    if type(obj.get("after_seq", 0)) is not int or type(obj.get("limit", 100)) is not int:
                        raise BlotterError("cursor and page size must be integers")
                    result = ledger.check(actor=actor, session=obj.get("session", "default"),
                                          after_seq=obj.get("after_seq", 0),
                                          limit=min(obj.get("limit", 100), MAX_HTTP_PAGE))
                else:
                    if set(obj) - WRITE_FIELDS:
                        raise BlotterError("unknown activity fields, actor is credential-bound")
                    if "kind" not in obj or "message" not in obj:
                        raise BlotterError("kind and message required")
                    result = ledger.record(actor=actor, **obj)
                self._send(200, result)
            except (BlotterError, ValueError, TypeError, UnicodeError) as exc:
                self._send(422, {"error": str(exc)})
            except OSError:
                self._send(503, {"error": "ledger temporarily unavailable"})

        def do_GET(self) -> None:
            actor = self._actor()
            if actor is None:
                return
            parts = urlsplit(self.path)
            if parts.path == "/v1/health":
                self._send(200, {"ok": True, "schema_version": SCHEMA_VERSION})
                return
            if parts.path != "/v1/events":
                self._send(404, {"error": "route not found"})
                return
            try:
                args = parse_qs(parts.query, strict_parsing=True)
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

    result = ThreadingHTTPServer((host, port), Handler)
    result.daemon_threads = True
    if certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(certfile=certfile, keyfile=keyfile)
            result.socket = context.wrap_socket(result.socket, server_side=True)
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
    args = parser.parse_args(argv)
    try:
        ledger = Blotter(args.db)
        credentials = load_credentials(args.credentials)
        server = create_server(ledger, credentials, host=args.host, port=args.port,
                               certfile=args.tls_cert, keyfile=args.tls_key)
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
