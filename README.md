# Blotter

**A shared, chronological activity ledger for cooperating software agents and automation workers.**

Blotter records what participating actors actually attempted, observed, tested, and decided in one ordered event history. It supports local SQLite use and a separately configured authenticated HTTP(S) service, with Python, command-line, durable turn-cursor, diagnostics, and local MCP stdio interfaces.

Blotter is **not** a message bus, task scheduler, command runner, permission issuer, or automatic agent instrumentation system. It does not create inboxes or per-agent event lanes. A record is evidence of a report, not independent proof of the underlying event.

## Product capabilities

| Surface | What it provides | Important boundary |
| --- | --- | --- |
| One event ledger | Global sequence, actor/session attribution, event metadata, JSON payload, and previous-record SHA-256 links | Internal consistency, not authentication of a privileged database writer |
| Local mode | One SQLite WAL database shared by processes on the **same host** | Separate database files are separate ledgers; do not place a WAL database on a shared network drive |
| Central mode | One server-owned database behind identity-bound bearer tokens and HTTP(S) endpoints | Requires operator deployment, credential management, and TLS for non-loopback binding |
| Catch-up and currentness | Paged `check`, bounded `tail`, cursor sequence/hash, and the `blotter-turn` client | Reading the latest 20 does not replace full catch-up or persist a turn cursor |
| Observability and integrations | `blotter` CLI, Python `Blotter` / `RemoteBlotter`, `blotter-probe`, `blotter-doctor`, and host-launched `blotter-mcp` | No automatic installation into an AI client, no unsolicited telemetry, and no external-effect authority |

The package targets **Python 3.10+** and declares no third-party runtime dependencies in [`pyproject.toml`](pyproject.toml).

## Local quick start

From an authorized source checkout:

```bash
python -m pip install -e .
blotter --db ./state/activity.sqlite3 init
blotter --db ./state/activity.sqlite3 check --actor agent.alpha --session example-001 --after-seq 0
blotter --db ./state/activity.sqlite3 record --actor agent.alpha --session example-001 --kind test.executed --message "Unit tests completed" --evidence OBSERVATION --payload '{"exit_code":0}'
blotter --db ./state/activity.sqlite3 tail --limit 20
blotter --db ./state/activity.sqlite3 verify
```

These commands operate on **one local database**, not an external service. `init` initializes it; `check` reads unseen events and records a check marker; `record` appends an explicitly supplied activity; `tail` displays the most recent events without marking a turn as checked; `verify` checks local ledger sequence/hash consistency. The example's test result is sample data, not an executed test or authenticated effect receipt.

For checkout-only development without installing, use `PYTHONPATH=src python -m agent_blotter` in place of `blotter`. The actual commands and flags are implemented in [`src/agent_blotter/cli.py`](src/agent_blotter/cli.py).

## Shared central deployment

For workers on different hosts, deploy **one** server with one protected database. Each logical actor receives its own randomly generated bearer token; all participating clients use the same service URL, not independent local ledgers. See the [central service guide](docs/REMOTE_SERVICE.md) for credentials, TLS, startup, and operating controls.

The `blotter` CLI uses `BLOTTER_URL` and `BLOTTER_TOKEN` when configured. Once an operator has provisioned a compatible endpoint and credentials, example authenticated reads are:

```bash
blotter --identity agent.alpha tail --limit 20
blotter --identity agent.alpha list
blotter check --actor agent.alpha --session example-002 --after-seq 0
```

These examples assume the environment variables and matching actor identity are already configured. Do not paste tokens into command arguments, log payloads, examples, or issue comments. Remote requests bypass ambient HTTP(S) proxies by default to avoid exposing bearer credentials; `--allow-environment-proxy` is an explicit opt-in for an operator-approved trusted gateway. `init` and `verify` remain local administrator operations.

The HTTP server uses identity-bound bearer tokens and refuses non-loopback binding without TLS. It does not supply a turnkey internet deployment, enrollment authority, secret vault, or host-wide agent hooks. **Always pass an explicit deployment URL** to diagnostic/MCP clients or configure the supported environment setting as appropriate; some source entry points retain a project-specific historical default endpoint. Their existence does not establish live availability or universal portability of a particular installation.

## Turn-oriented client and Python API

Applications that adopt Blotter can implement this **opt-in turn contract**:

1. At turn start, use the actor's persisted cursor to call `check`, continuing while `has_more` is true.
2. Record material actions with accurate evidence/effect labels, retaining uncertainty rather than promoting an observation to verification.
3. Persist `next_seq` and `next_hash` for the next turn. Supply the prior hash on catch-up to detect ledger replacement/rewrite at the same endpoint.
4. Handle rejected, ambiguous, or legacy cursors explicitly; do not silently reset them or assume old history is current authority.

The credential-bound `blotter-turn` command automates bounded catch-up and durable actor-specific cursor persistence for configured remote clients. It refuses silent reuse of incompatible legacy cursors; re-enrollment requires a deliberate full-history replay after reviewing prior state. See the [turn contract](docs/AGENT_TURN_CONTRACT.md).

Local Python API:

```python
from pathlib import Path
from agent_blotter import Blotter

ledger = Blotter(Path("state/activity.sqlite3"))
seen = ledger.check(actor="agent.alpha", session="example-001", after_seq=0)
event = ledger.record(
    actor="agent.alpha", session="example-001", kind="test.executed",
    message="Unit tests completed", evidence="OBSERVATION"
)
```

For a configured remote service, `agent_blotter.RemoteBlotter` exposes `check`, `record`, `tail`, and related authenticated operations. See [`src/agent_blotter/__init__.py`](src/agent_blotter/__init__.py), [`src/agent_blotter/remote.py`](src/agent_blotter/remote.py), and [`src/agent_blotter/store.py`](src/agent_blotter/store.py) for actual interfaces.

## Local MCP adapter and diagnostics

`blotter-mcp` is a **host-launched stdio adapter**, not a network MCP listener or an automatically installed ChatGPT tool. Its exposed methods include `begin_turn`, `latest20`, and `record_activity`, with explicit actor and token-file configuration. An older central service without `GET /v1/tail` cannot serve `latest20`; the adapter fails closed. See [MCP stdio](docs/MCP_STDIO.md).

`blotter-probe` checks remote connectivity without creating a local ledger; `blotter-doctor` performs read-only-by-default diagnosis. The latter only attempts a test append with an explicit `--test-append`. See [remote probe](docs/REMOTE_CLIENT_PROBE.md) and [diagnostics](docs/DIAGNOSTICS.md). Neither proves that a client has been installed on a particular machine.

## Security, evidence, and limits

- Event contents are untrusted input. **A record does not grant execution, merge, provider, identity, consent, or deployment authority.** The hash link detects some local ledger inconsistencies; it is not a digital signature or independent witness.
- Sensitive field-name redaction is best effort. Free-form messages and values are not comprehensively scanned. Record only information you are authorized to retain, and keep secrets and private logs out of public examples.
- `tail` returns recent history only; it does not append a `turn.checked` marker, reconcile all missing pages, or establish that every agent is participating.
- Source tests, CI, or historical deployment notes do not establish that a named host is currently reachable, that every agent is integrated, or that production recovery/security has been qualified.
- No root `LICENSE` file was available in the inspected public source. **Do not infer an open-source license or redistribution/commercial rights from repository visibility.** Clarify usage rights with the owner before incorporating this implementation into another product.

Repository documentation: [architecture](docs/ARCHITECTURE.md) · [central service](docs/REMOTE_SERVICE.md) · [turn contract](docs/AGENT_TURN_CONTRACT.md) · [MCP stdio](docs/MCP_STDIO.md).

To run the repository's unit suite in an authorized checkout:

```bash
python -m unittest discover -s tests -v
```

This README describes the inspected source contract, not an independent live deployment test.
