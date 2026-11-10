"""Qualify real HTTP clients across an isolated server process cold restart.

This command has no production URL, credential, database, or restart option.
Every run creates a fresh synthetic database below the supplied evidence folder.
Synthetic bearer values exist only in parent/child process memory/environment.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_blotter import Blotter  # noqa: E402
from agent_blotter.remote import RemoteBlotter  # noqa: E402
from agent_blotter.server import create_server  # noqa: E402
from agent_blotter.store import BlotterError  # noqa: E402


ACTOR = "qualification.agent"
SESSION = "isolated-durability"
PINNED_FILES = (
    "src/agent_blotter/__init__.py", "src/agent_blotter/store.py",
    "src/agent_blotter/server.py", "src/agent_blotter/remote.py",
    "src/agent_blotter/probe.py", "src/agent_blotter/private_credentials.py",
    "src/agent_blotter/mcp_stdio.py", "src/agent_blotter/turn.py",
    "src/agent_blotter/turn_pages.py", "src/agent_blotter/codex_hooks.py",
    "src/agent_blotter/cli.py", "src/agent_blotter/doctor.py",
    "pyproject.toml", "ops/verify_operational_client.py",
)


class QualificationFailure(RuntimeError):
    """A deliberately credential-free qualification failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in PINNED_FILES}


def _hidden_process_options() -> dict:
    # Popen does not create a visible console on Windows for this helper.
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _environment(token: str, *, url: str = "") -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("BLOTTER_")}
    env.update(BLOTTER_TOKEN=token, BLOTTER_URL=url,
               PYTHONPATH=str(ROOT / "src"), PYTHONNOUSERSITE="1")
    return env


def _serve(folder: Path) -> int:
    """Private child entry point; use the real store and HTTP server API."""
    token = os.environ.pop("BLOTTER_TOKEN", "")
    server = create_server(Blotter(folder / "synthetic.sqlite3"),
                           {token: ACTOR}, host="127.0.0.1", port=0,
                           request_timeout=2.0)
    ready = {"pid": os.getpid(), "port": server.server_port,
             "source_sha256": _hashes(), "python_version": sys.version}
    (folder / "ready.json").write_text(json.dumps(ready), encoding="utf-8")

    def await_stop():
        while not (folder / "stop").exists():
            time.sleep(0.03)
        server.shutdown()

    threading.Thread(target=await_stop, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.03)
    finally:
        server.server_close()
    return 0


class ServerProcess:
    def __init__(self, folder: Path, token: str, pins: dict):
        self.folder = folder
        (folder / "ready.json").unlink(missing_ok=True)
        (folder / "stop").unlink(missing_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--_serve", str(folder)],
            cwd=ROOT, env=_environment(token), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            **_hidden_process_options())
        self.output = ""
        self.exit_code = None
        self.url = ""
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise QualificationFailure("isolated child exited before readiness")
                try:
                    ready = json.loads((folder / "ready.json").read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.03)
                    continue
                if ready.get("source_sha256") != pins or ready.get("python_version") != sys.version:
                    raise QualificationFailure("source or interpreter changed during qualification")
                self.url = f"http://127.0.0.1:{ready['port']}"
                health = RemoteBlotter(self.url, actor=ACTOR, token=token,
                                        timeout=2, direct_only=True).health()
                if health.get("ok") is not True:
                    raise QualificationFailure("isolated HTTP health was not verified")
                self.ready = ready
                break
            else:
                raise QualificationFailure("isolated child readiness deadline expired")
        except BaseException:
            self.stop()
            raise

    def stop(self) -> dict:
        if self.exit_code is not None:
            return {"pid": self.process.pid, "exit_code": self.exit_code,
                    "controlled_stop": self.exit_code == 0}
        (self.folder / "stop").touch()
        forced = False
        try:
            stdout, stderr = self.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # Only terminate the helper created and owned by this object.
            forced = True
            self.process.terminate()
            stdout, stderr = self.process.communicate(timeout=5)
        self.output = stdout + stderr
        self.exit_code = self.process.returncode
        return {"pid": self.process.pid, "exit_code": self.exit_code,
                "controlled_stop": not forced and self.exit_code == 0}


def _http_status(url: str, *, token: str | None = None,
                 actor: str | None = None, body: dict | None = None) -> int:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if actor is not None:
        headers["X-Blotter-Actor"] = actor
    raw = None if body is None else json.dumps(body).encode("utf-8")
    if raw is not None:
        headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=raw, headers=headers,
                                     method="GET" if raw is None else "POST")
    try:
        with opener.open(request, timeout=2) as reply:
            reply.read(8193)
            return reply.status
    except urllib.error.HTTPError as exc:
        try:
            exc.read(8193)
            return exc.code
        finally:
            exc.close()


def _probe(url: str, token: str, event_id: str | None = None) -> tuple[dict, str]:
    command = [sys.executable, "-m", "agent_blotter.probe", "--url", url,
               "--actor", ACTOR, "--session", SESSION, "--timeout", "2"]
    if event_id is not None:
        command += ["--append-test", "--event-id", event_id]
    reply = subprocess.run(command, cwd=ROOT, env=_environment(token, url=url),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=15, **_hidden_process_options())
    output = reply.stdout + reply.stderr
    if token in output:
        raise QualificationFailure("probe emitted a synthetic bearer")
    try:
        result = json.loads(reply.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise QualificationFailure("probe did not emit one JSON diagnostic") from exc
    if reply.returncode != 0 or result.get("status") != "OK":
        raise QualificationFailure("probe did not verify its HTTP operation")
    return result, output


def _record_once(client: RemoteBlotter, fields: dict) -> tuple[dict, str]:
    """On an ambiguous POST, read/reconcile by ID; never blindly retry."""
    try:
        return client.record(**fields), "ACKNOWLEDGED"
    except BlotterError as exc:
        if "write outcome may be unknown" not in str(exc):
            raise
        matches = [event for event in client.list(limit=100)
                   if event.get("id") == fields["event_id"]]
        if len(matches) != 1 or any(matches[0].get(key) != value for key, value in
                {**fields, "id": fields["event_id"], "actor": ACTOR}.items()
                if key != "event_id"):
            raise QualificationFailure("ambiguous write remains unresolved; no retry attempted") from exc
        return matches[0], "RECONCILED_BY_EVENT_ID_WITHOUT_RETRY"


def _integrity(database: Path) -> dict:
    # This path can only be the fresh synthetic database made by this run.
    result = Blotter(database).verify()
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
        result["sqlite_quick_check"] = connection.execute("PRAGMA quick_check").fetchone()[0]
    return result


def qualify(output_directory: Path) -> dict:
    output_directory = Path(output_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    folder = output_directory / ("synthetic-blotter-" + uuid.uuid4().hex)
    folder.mkdir(exist_ok=False)
    token, bad_token = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
    pins = _hashes()
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True, **_hidden_process_options())
    report = {"status": "RUNNING", "started_at": _now(), "scope": "ISOLATED_SYNTHETIC_ONLY",
              "source_commit": git.stdout.strip() if git.returncode == 0 else None,
              "source_sha256": pins, "python_version": sys.version,
              "python_executable": sys.executable, "os_name": os.name,
              "evidence_directory": str(folder), "checks": []}
    servers = []

    def check(name, passed, **details):
        report["checks"].append({"name": name, "passed": bool(passed), **details})
        if not passed:
            raise QualificationFailure(name)

    try:
        first_server = ServerProcess(folder, token, pins)
        servers.append(first_server)
        client = RemoteBlotter(first_server.url, actor=ACTOR, token=token,
                                timeout=2, direct_only=True)
        check("fresh_empty_store", client.list(limit=100) == [])
        for route in ("/v1/health", "/v1/events"):
            status = _http_status(first_server.url + route)
            check("unauthenticated_get_" + route.rsplit("/", 1)[1], status == 401,
                  http_status=status)
        denied = {"kind": "test.executed", "message": "Rejected synthetic request",
                  "event_id": "denied-" + uuid.uuid4().hex}
        status = _http_status(first_server.url + "/v1/record", token=bad_token,
                              actor=ACTOR, body=denied)
        check("bad_bearer_post_rejected", status == 403, http_status=status)
        status = _http_status(first_server.url + "/v1/record", token=token,
                              actor="wrong.actor", body=denied)
        check("wrong_actor_post_rejected", status == 403, http_status=status)
        check("rejected_requests_do_not_append", client.list(limit=100) == [])
        readonly, _ = _probe(first_server.url, token)
        check("existing_probe_readonly", readonly.get("append") == "NOT_ATTEMPTED"
              and readonly.get("tail") == "AVAILABLE" and client.list(limit=100) == [],
              diagnostic=readonly)
        fields = {"kind": "test.executed", "message": "Isolated HTTP durability qualification",
                  "session": SESSION, "payload": {"synthetic": True},
                  "evidence": "OBSERVATION", "effect": "ATTEMPTED",
                  "event_id": "sdk-" + uuid.uuid4().hex}
        first, disposition = _record_once(client, fields)
        check("existing_sdk_append_readback", first == client.list(limit=100)[0]
              and first.get("actor") == ACTOR and first.get("id") == fields["event_id"],
              event_id=fields["event_id"], seq=first.get("seq"), disposition=disposition)
        probe_id = "probe-" + uuid.uuid4().hex
        appended, _ = _probe(first_server.url, token, probe_id)
        before = client.list(limit=100)
        check("existing_probe_append_readback", appended.get("append") == "VERIFIED"
              and len(before) == 2 and before[1].get("id") == probe_id,
              event_id=probe_id, diagnostic=appended, readback_count=len(before))
        stop = first_server.stop()
        check("first_process_controlled_stop", stop["controlled_stop"], **stop)
        first_integrity = _integrity(folder / "synthetic.sqlite3")
        check("pre_restart_hash_chain", first_integrity.get("ok") is True
              and first_integrity.get("count") == 2
              and first_integrity.get("sqlite_quick_check") == "ok", **first_integrity)

        restarted = ServerProcess(folder, token, pins)
        servers.append(restarted)
        client = RemoteBlotter(restarted.url, actor=ACTOR, token=token,
                                timeout=2, direct_only=True)
        after = client.list(limit=100)
        check("cold_restart_exact_http_readback", after == before, readback_count=len(after),
              first_pid=first_server.process.pid, restarted_pid=restarted.process.pid)
        retried, disposition = _record_once(client, fields)
        check("sdk_same_id_retry_after_restart", retried == first
              and client.list(limit=100) == before, seq=retried.get("seq"),
              readback_count=len(client.list(limit=100)), disposition=disposition)
        repeated, _ = _probe(restarted.url, token, probe_id)
        check("probe_same_id_retry_after_restart", repeated.get("append_seq") == before[1]["seq"]
              and client.list(limit=100) == before, diagnostic=repeated, readback_count=len(before))
        try:
            client.record(**{**fields, "message": "Different body under existing synthetic ID"})
        except BlotterError as exc:
            status = getattr(exc.__cause__, "code", None)
            collision = "event_id collision with different content" in str(exc)
        else:
            status, collision = 200, False
        check("changed_body_same_id_rejected", status == 422 and collision,
              http_status=status, collision_reported=collision)
        check("all_retries_and_conflicts_leave_two_events", client.list(limit=100) == before,
              readback_count=len(client.list(limit=100)))
        stop = restarted.stop()
        check("restarted_process_controlled_stop", stop["controlled_stop"], **stop)
        final_integrity = _integrity(folder / "synthetic.sqlite3")
        check("post_restart_hash_chain_unchanged", final_integrity == first_integrity,
              **final_integrity)
        check("source_pins_unchanged", _hashes() == pins)
        check("child_diagnostics_contain_no_bearer", all(
            token not in server.output and bad_token not in server.output for server in servers))
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        # Do not include arbitrary exception text or request/response bodies.
        report["failure"] = {"type": type(exc).__name__, "reason": (
            str(exc) if isinstance(exc, QualificationFailure) else "operation failed; inspect named check")}
    finally:
        for server in servers:
            server.stop()
        report["finished_at"] = _now()
    serialized = json.dumps(report, indent=2, sort_keys=True)
    if token in serialized or bad_token in serialized:
        report = {"status": "FAIL", "failure": {"reason": "synthetic bearer output blocked"}}
        serialized = json.dumps(report, indent=2, sort_keys=True)
    (folder / "receipt.json").write_text(serialized + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--_serve", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._serve is not None:
        return _serve(args._serve)
    if args.output_dir is None:
        parser.error("--output-dir is required")
    result = qualify(args.output_dir)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
