# Remote-only client diagnostics (VN1, VN2, and other agents)

Use `blotter-probe` to qualify a machine's connection to the **existing one
central Blotter service**. It never creates a local SQLite ledger and is
read-only unless `--append-test` is explicitly requested. It prints JSON
status codes and sequence metadata, not private event bodies or credentials.

Python 3.10+ and the installed `agent-blotter` package are required. On a
non-loopback target the operator must provision `BLOTTER_URL` in the process
environment. A CLI `--url` may only select that *same* target. The remote
probe rejects URL retargets before loading its bearer; HTTP is permitted only
for loopback testing. The operator must also prevent untrusted code from
modifying the process environment or receiving the bearer.

The diagnostic connects directly; it never sends a bearer through ambient
`HTTP_PROXY` or `HTTPS_PROXY` and does not follow HTTP redirects.

For Linux (POSIX), provision an owner-only regular token file, not a symlink,
with mode 0600 or stricter. A Windows token file is rejected until reliable
DACL verification is implemented; on VN1 inject `BLOTTER_TOKEN` from a trusted
credential source into the probe's process environment (not command arguments).
Protect the process environment from lower-trust users.

For example, with `BLOTTER_URL` already set:

```text
blotter-probe --actor YOUR-ENROLLED-AGENT --token-file PRIVATE_TOKEN_FILE
```

On a source checkout without installed scripts:

```text
PYTHONPATH=src python -m agent_blotter.probe \
  --actor YOUR-ENROLLED-AGENT --token-file PRIVATE_TOKEN_FILE
```

Set `PYTHONPATH` using the host shell's syntax (PowerShell uses
`$env:PYTHONPATH = 'src'`). `BLOTTER_URL` and `BLOTTER_TOKEN` environment
variables are supported, but the bearer token is **never** a CLI argument.
Each logical agent
requires its **own** server-enrolled identity and token; do not copy Lappy's
existing agent token to another node. The server operator controls enrollment,
credential rotation, TLS and backups.

The default probe performs three authenticated GET requests:

1. `/v1/health` — a response from the selected service, not a writable-ledger
   guarantee.
2. `/v1/events?after_seq=0&limit=1` — actual read permission, without logging
   a turn check or exposing the event body.
3. `/v1/tail?limit=20` — reports `AVAILABLE`, `NOT_DEPLOYED` (404), or
   `UNAVAILABLE` separately. A missing tail endpoint does not invalidate a
   successful `/v1/events` read. It **does** prevent qualifying an MCP tool
   that relies on `/v1/tail`.

For an explicitly authorized, idempotent write/readback probe only:

```text
blotter-probe --actor YOUR-ENROLLED-AGENT --token-file PRIVATE_TOKEN_FILE \
  --append-test --event-id UNIQUE-STABLE-TEST-ID --session YOUR-TEST-SESSION
```

It sends the *same* fixed test event twice with the supplied ID, checks that
both acknowledgments have the same sequence number, and then reads that exact
record back. Rerun with the same ID **and session** to reconcile ambiguous
transport failures. Do not assume a failed POST made no change. This probe
does not substitute for a durable per-agent turn cursor, hash validation, or
the mandatory check/record protocol.

Top-level JSON `status` distinguishes `CONFIG_MISSING`,
`CONFIG_PERMISSION_DENIED`, `CONFIG_TARGET_UNTRUSTED`, `CONNECTIVITY_FAILED`,
`AUTHENTICATION_FAILED` (401), `AUTHORIZATION_FAILED` (403),
`CONTRACT_UNAVAILABLE` (404), `READ_FAILED`, `HEALTH_FAILED`,
`APPEND_FAILED`, `APPEND_OUTCOME_UNKNOWN`, `VERIFICATION_FAILED`, and `OK`.
No error response echoes the URL, actor token, event message, or raw exception.
An explicit write reports `ATTEMPTED_UNCONFIRMED` until an exact readback is
verified; an error does not prove the server rejected or discarded an append.

Draft [Blotter PR #12](https://github.com/thebrazenbeard/blotter/pull/12)
independently implements `blotter-doctor` for the newer `/v1/tail` API. This
current-main `blotter-probe` additionally checks the **deployed** `/v1/events`
API, where a missing `/v1/tail` must not be reported as a total outage. Both
commands should eventually be consolidated after the stacked MCP draft lands.

**Warning:** the ordinary `blotter` CLI can default to local
`~/.blotter/activity.sqlite3` when its central URL is absent. Do not treat
that local output as participation in the central ledger. This remote-only
probe has no local-ledger fallback. Installing it on VN1/VN2 does not enroll
those agents, and installing a package does not mount a ChatGPT MCP connector.
