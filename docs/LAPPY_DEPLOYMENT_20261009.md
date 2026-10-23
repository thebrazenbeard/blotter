# Lappy private Blotter deployment (2026-10-09)

This is a partial operational deployment, not universal agent participation.

- Reviewed source checkout: D:\VERA\repos\blotter, draft PR #1 development branch.
- Persistent SQLite store: D:\VERA\blotter-runtime\data\activity.sqlite3 (local NTFS).
- Credentials: D:\VERA\blotter-runtime\private, NTFS ACL restricted to LAPPY\patri and SYSTEM.
- Loopback API: http://127.0.0.1:19083.
- Private tailnet HTTPS: https://lappy.tail86ea75.ts.net/blotter.
- Pre-existing HTTPS / route and TCP 17444 are preserved.
- Startup: HKCU Run VERA-Blotter (Task Scheduler could not be registered under current access).
- Lappy must be awake and signed in. This is not an always-on Windows service.
- Never commit tokens, credentials or runtime records into this public repository.

At the start of every agent turn, invoke the cursor client (with an agent's own token file):

    python -m agent_blotter.turn --url https://lappy.tail86ea75.ts.net/blotter --actor AGENT --token-file PRIVATE_TOKEN_FILE --state-dir CURSORS begin --session TURN_ID

After each actual activity, append an event:

    python -m agent_blotter.turn --url https://lappy.tail86ea75.ts.net/blotter --actor AGENT --token-file PRIVATE_TOKEN_FILE record --session TURN_ID --kind tool.executed --message "Actual action" --evidence OBSERVATION

The client pages through all unseen events and persists a per-actor cursor. This does not hook external ChatGPT, Codex or plugin runtimes automatically. Non-tailnet agents have no direct reachability until an authorized secure transport is installed. No public Funnel is enabled.

Recovery needs both source and runtime state. Restore a SQLite-consistent backup via SQLite's backup API, preserve credentials or deliberately rotate and re-enroll clients, restore one loopback server and tailnet route, then verify independent reads and local hash-chain integrity. Automated backups, key rotation, failover and global compliance enforcement are not implemented. CCB remains intact. The blotter is not a messaging system.

An on-demand SQLite-consistent snapshot can be created using ops/backup.py after preparing a protected output directory. One initial local backup was attempted during deployment. Backups on the same disk do not survive disk loss; replication to separate protected storage is not configured.
