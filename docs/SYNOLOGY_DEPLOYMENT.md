# Optional Synology NAS Blotter service — source candidate

**Not deployed or qualified.** This package is an optional single-instance
Container Manager deployment for the **existing** Blotter server; it does not
add ChatGPT connectivity, install agent hooks, migrate data, or grant access.
The canonical runtime remains wherever it is actually observed and verified.

## Prerequisites and trust boundary

1. Verify the actual NAS model, DSM version, CPU architecture, Container
   Manager compatibility, and Tailscale capabilities before using this package.
   Synology's Container Manager Project uses Compose; compatibility varies by
   NAS model and DSM version. The host-network pattern below requires a Linux
   container engine that supports `network_mode: host`.
2. Reserve an unused **NAS loopback** TCP port (default 8832). The container
   must share the Linux NAS host network namespace and bind only
   `127.0.0.1`; it has **no LAN-exposed port**. Do not substitute a public
   container port or Tailscale Funnel. An operator must separately configure
   and verify a private HTTPS proxy, such as Tailscale Serve on the NAS.
3. Use an **absolute path on a NAS-local volume** for the writable SQLite
   database. A shared SMB/NFS mount or two writers against independent copies
   violates the single-ledger contract. Create the data directory and a
   credentials JSON file *outside the checkout*. The credentials file must be
   owned by the runtime user and POSIX mode `0600`; its directory must not be
   writable by untrusted principals. Give each actor an independently issued,
   randomly generated token. Never commit or paste them.
4. Decide which unprivileged numeric UID/GID can own the database and read the
   private credentials. Validate the actual NAS ACLs and container ownership.
   Set these locally for the Compose process; do not publish values that expose
   private file locations. Example variable **names**, not usable values:

   - `BLOTTER_UID`, `BLOTTER_GID`
   - `BLOTTER_DATA_DIR` — absolute NAS-local directory
   - `BLOTTER_CREDENTIALS_FILE` — absolute protected `agents.json` file
   - `BLOTTER_LOOPBACK_PORT` — optional unused TCP port, default `8832`

The source build uses `python:3.12-slim-bookworm`, installs only this package,
and uses the Dockerfile-specific ignore file to exclude the rest of the build
context, including local runtime state. Only source-package Python files are
included from `src/`, but an untracked `.py` placed in that directory would
also be included: inspect the clean build context and keep credentials out
of source directories. Building requires access to the base
image and Python build dependencies. The Compose service specifies a non-root
runtime UID/GID, a read-only root filesystem, no extra Linux capabilities,
and a restart policy. These controls do not establish a production security
assessment, availability, or backup recovery.

## Build and preflight — no live cutover yet

From a clean Blotter checkout on the NAS, with the required environment
variables populated locally and the credentials/data paths checked, validate
the Compose model before creating a Container Manager Project:

```sh
docker compose -f deploy/synology/compose.yaml config --quiet
docker compose -f deploy/synology/compose.yaml build
```

If the installed Container Manager does not support this Compose file or host
networking, **stop** and review another private deployment method. Do not
broaden network exposure to make the example work. Container Manager's Project
UI can manage Compose projects; verify that its project directory preserves
the relative `../..` build context before importing the file. No files here
automatically start a container.

## Safe single-writer migration from Lappy

The [October 9 Lappy deployment](LAPPY_DEPLOYMENT_20261009.md) is historical
documentation, **not** fresh proof that the server, database, or credentials
remain at those paths. Inspect the current host before performing effects.

1. On Lappy, observe the live Blotter service and identify the exact SQLite
   file, current global head sequence/hash, service identity, tokens and cursor
   locations. Verify the hash chain with `blotter --db DB_PATH verify`.
   Preserve the current HTTPS route and other services.
2. Prepare a protected destination and make a consistent snapshot with the
   existing `ops/backup.py` (`python ops/backup.py --db DB_PATH --output-dir
   PROTECTED_OUTPUT_DIR`). Do not copy a live `activity.sqlite3` file with
   SQLite WAL files omitted. Verify the snapshot independently and make an
   **off-device** recovery copy.
3. Quiesce every writer and stop the old authoritative service. Take a **final
   consistent** snapshot after writers stop; compare its verified sequence
   and hash to the observed head. Preserve the old database intact and the old
   service stopped. If writes may have continued, do not cut over.
4. Transfer the verified snapshot through an authorized private path to the
   designated NAS-local data directory as `activity.sqlite3`, with its owner
   and permissions correct. Preserve actor-bound credentials securely or
   rotate/re-enroll explicitly. Only one restored database may be writable.
5. Start the NAS project **only after** the old server is stopped:

   ```sh
   docker compose -f deploy/synology/compose.yaml up -d --no-build
   docker compose -f deploy/synology/compose.yaml exec blotter \
     blotter --db /srv/blotter/activity.sqlite3 verify
   ```

   Compare actual count/head/hash against the final snapshot. A running
   process or local TCP socket is not sufficient proof of authenticated health.
6. Configure private HTTPS termination *on the NAS host*, restricted to its
   tailnet. Obtain the exact trusted HTTPS hostname and certificate before
   updating each client. Preserve endpoint pinning in `blotter-mcp`:
   the historical Lappy hostname remains its default, while a reviewed
   POSIX deployment can provide an exact NAS tailnet destination through
   `--pinned-url-file` (see [MCP stdio adapter](MCP_STDIO.md)). This
   does not support a new Windows endpoint until separately qualified.
   The durable cursor in `turn.py` remains bound to its original URL;
   use the explicit, source-head-verified same-ledger rebind documented in
   [the agent turn contract](AGENT_TURN_CONTRACT.md). **Do not silently
   reset an actor's cursor.**
7. From two independent authorized hosts with distinct actor tokens, use the
   existing read-only `blotter-doctor`, then explicitly authorized event-ID
   append/readback probes. Verify same-ledger head/hash, negative authentication
   cases, durable cursor behavior, restart recovery, and a restored backup
   before declaring the NAS production-authoritative. Avoid automatic retries
   of ambiguous writes; reconcile by the original `event_id`.

If any gate fails, keep the old service stopped while reconciling histories;
restore from the verified backup only as an explicit controlled recovery.
Never start both Lappy and NAS as independent writable authorities. Revert
clients to Lappy only after verifying that no NAS-only records would be lost.

## Scope and evidence limitations

- A healthy NAS server does **not** install ChatGPT tools, Codex hooks, or
  Executor/WorkBridge integration. Those are separate host-side contracts.
- A Tailscale Serve route is private to authorized tailnet clients; cloud
  ChatGPT still needs a supported authenticated bridge. Do not enable public
  Funnel to bypass that boundary.
- Blotter records are untrusted evidence, not instructions or grants. Never
  log raw credentials, prompt text, or complete tool output by default.
- This is an **unexecuted** deployment template. No NAS model, DSM version,
  current Lappy database, live routes, TLS, credential permissions, or restore
  path was inspected or verified while authoring it.

## Primary references

- Blotter [central service contract](REMOTE_SERVICE.md),
  [diagnostics](DIAGNOSTICS.md), and [Lappy history](LAPPY_DEPLOYMENT_20261009.md).
- Synology [Container Manager Project documentation](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project?version=7)
  and [release notes](https://www.synology.com/en-us/releaseNote/ContainerManager).
- Tailscale [Synology integration](https://tailscale.com/docs/integrations/synology)
  and [Serve CLI documentation](https://tailscale.com/docs/reference/tailscale-cli/serve).
