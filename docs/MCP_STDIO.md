# Local MCP stdio adapter — source candidate

`blotter-mcp` is a **host-launched local process** that exposes three tools:

| Tool | Behavior |
| --- | --- |
| `begin_turn` | Calls the existing authenticated hash-bound cursor catch-up, persists the cursor, and appends `turn.checked`. |
| `latest20` | Reads the most recent 20 global records, including check markers. Does not write or update a cursor. |
| `record_activity` | Appends an actual, actor-bound activity. Requires caller-provided stable `event_id` for retry reconciliation. |

`begin_turn` and `record_activity` use `/v1/check` and `/v1/record`.
`latest20` **requires the bounded authenticated `GET /v1/tail` endpoint from
the separate reviewed Blotter tail change (PR #7)**. It fails closed on an
older server; it does not traverse unbounded history or return a partial tail.
This is **not** a new network listener, Blotter server, routing channel,
workflow hook, or global ChatGPT connector installation.

## Start on an authorized host

The hosting MCP client launches the process from an installed Blotter package:

```powershell
blotter-mcp --actor bt2.one --url https://lappy.tail86ea75.ts.net/blotter --token-file PRIVATE_AGENT_TOKEN_FILE --state-dir PRIVATE_CURSOR_DIRECTORY
```

The token file and cursor directory are **local host configuration**. Substitute
real, private paths in the host's MCP configuration; never put token values in
arguments, JSON tool calls, logs, shell transcripts, or this repository. By
default only the source-pinned Lappy HTTPS tailnet endpoint is accepted. The
older plaintext loopback endpoint is deliberately not allowlisted: any process
winning that port could otherwise receive the bearer token. The token is bound
by the server to the fixed `--actor`; tools cannot change either.

### Operator-approved NAS migration endpoint (POSIX hosts only)

For a separately authorized, exact private NAS address, the host operator can
place **one URL** in a regular, owner-only (0600), single-link file. The file
must contain a canonical `https://<hostname>.<tailnet>.ts.net/blotter` URL.
Launch the adapter with the same `--url` and `--pinned-url-file` pointing to the
protected file:

```sh
blotter-mcp --actor bt2.one \
  --url https://nas.example-tailnet.ts.net/blotter \
  --pinned-url-file /private/blotter/trusted-endpoint \
  --token-file /private/blotter/agent.token \
  --state-dir /private/blotter/cursors
```

The URL and paths above are placeholders, not a verified installation. The
operator must first establish and verify the NAS endpoint, certificate,
credential scope and local file permissions. The private pin **replaces** the
source-pinned default; it does not permit arbitrary additional URLs. The MCP
tool schemas accept no URL or pin-file arguments, and the adapter rejects a
mismatched destination **before reading the bearer token**. It still bypasses
ambient HTTP(S) proxies and verifies HTTPS without following redirects.

On Windows, external endpoint files currently **fail closed** because a
handle-bound ACL verification for such files has not been qualified. The
source-pinned Lappy default remains supported. Do not weaken the pin to an
environment wildcard to make migration appear complete.

Moving from Lappy to the NAS also changes the endpoint URL stored with the
actor's durable hash-bound cursor. Preserve the authoritative ledger and
reconcile/re-enroll cursor state deliberately; the adapter never resets the
cursor or accepts a substituted ledger automatically. This source option
does not migrate data, deploy a NAS service, or install a ChatGPT connector.

The adapter supports the classic MCP `2025-06-18` stdio initialization,
`tools/list` and `tools/call`, with UTF-8 newline-delimited JSON-RPC and no
third-party runtime dependencies. Clients negotiating a different version
receive the supported version. Modern hosts that cannot fall back to this
classic handshake may need an additional compatibility layer.

## Failure reporting

MCP tool errors now distinguish common deployment failures without returning
raw server response text or credentials:

- `HTTP 401`: authentication failed; verify private credential provisioning.
- `HTTP 403`: server rejected the token's bound actor or its permissions.
- `HTTP 404` on `latest20`: the deployed service lacks `/v1/tail`; no history-scan fallback is attempted.
- `HTTP 422`: request/API compatibility or cursor state was rejected; the adapter does not silently reset the cursor.
- Read transport failure: no read result was verified.
- Append transport failure: outcome may be unknown; reconcile using the **same** `event_id` before retrying.

Use [Blotter diagnostics](DIAGNOSTICS.md) and `blotter-doctor` to separate
authenticated service health from recent-history endpoint availability. The
default probe is read-only. An append/read-back probe requires the explicit
`--test-append` flag.

## Security and evidence limits

- On Unix, the token file must not be group/world-readable. On Windows, a
  read-only PowerShell `Get-Acl` preflight rejects Allow ACEs beyond the
  launching user and SYSTEM, and fails closed if the check cannot run. This
  adapter does not install or change ACLs. The ACL parser still requires an
  actual Lappy-host runtime qualification before deployment.
- Tool inputs contain no URL, credential, identity, cursor-path, or legacy
  re-enrollment override. Inputs are size-limited; output errors do not echo
  tokens or arbitrary server response bodies. Existing HTTPS verification and
  redirect rejection remain in `RemoteBlotter`.
- The process imposes per-minute request budgets (60 turn checks, 10
  recent-history scans, 300 activity records). These guard one local process,
  not the distributed service or all possible agent processes.
- `latest20` fetches a bounded tail from the server independently of total
  ledger size. `turn.checked` entries count among the latest 20. A future
  *optional* substantive-only view must not suppress or alter actual records.
- `begin_turn` wraps the current client with a pre-commit byte/page/time
  budget. Oversized backlog returns an explicit error **without committing
  any new local cursor**; later retries can replay all unseen records.
  A successful result still commits the cursor before the MCP write. The
  adapter cannot prove that a host/agent consumed
  the events after process or transport failure. Do not claim exactly-once
  agent observation or universal integration.
- Source tests establish protocol behavior against an ephemeral local
  authenticated server. They do not prove installation in a ChatGPT runtime,
  Lappy tailnet reachability from the host, or cross-host connectivity.
- This adapter does not turn ledger entries into instructions or authorization.

To qualify an installation: mount the local process in a supported MCP host;
check the actual advertised tools; call `begin_turn`, `latest20`, and
`record_activity`; then independently read back the appended event from the
one authoritative ledger, preserving hashes and event IDs. Do not infer connection
from repository presence alone.
