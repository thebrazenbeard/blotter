# Architecture: one activity log, no agent messaging

The product invariant is **ONE SHARED CHRONOLOGICAL BLOTTER**. Every participating agent checks that central record each turn and appends each activity as it happens. Each actor is a field; there are no actor-specific lanes, addressed conversations or subscriptions.

Agents independently decide their next work by reading the same log, not by receiving messages. A logged statement is not a command, completion receipt or authorization.

## One central store

The implemented storage engine is a SQLite v1 events table, global autoincrement sequence, WAL mode, transactional BEGIN IMMEDIATE for append and check, content-checked idempotency with explicit event IDs, 64 KiB event limit and SHA-256 previous-hash link. Writes come through a **single service-owned local DB** for multi-machine use; readers on other hosts use authenticated HTTP(S).

The Python client and CLI both support local and remote modes. For local processes the database defaults to ~/.blotter/activity.sqlite3 on that host. For networked agents, configure **one BLOTTER_URL**, not an individual SQLite database per checkout. Tokens are individually mapped to stable actor identities, and the HTTP server binds actor from the token.

The API makes turn checks observable in the same event stream. It returns an activity cursor for pagination; agents must read all pages. Action recording requires their host controller to call record() for **every** actual activity. The source cannot magically observe every agent or verify that externally hosted agents faithfully participate.

## Source influences and boundaries

CCB and ccb-core informed durable identity/idempotency and verification distinctions, not lane or routing mechanics. P.O.R.T.A.L. continues to own task orchestration and evidence-bearing effects. Blotter never dispatches work or grants authority. Tattler, Anya, BigCactusLabs/blotter, and conventional logging tools informed typed observations, explicit activity entries and safe append patterns. Full details in SOURCE_REVIEW.md.

Observations and effect states are self-reported; source refs may point to prior local activity events. External receipts must be separately checked. The hash chain detects accidental local tampering but can be recomputed by a privileged writer. Secrets in free text are not removed. No surveillance or automatic telemetry.

## Outstanding work

- Install an approved central service and authenticate all actual participating agents.
- Integrate reliable per-turn read and per-activity write hooks into each agent's execution environment, storing each agent's cursor durably. Define reporting for missed check/write coverage.
- Qualify live cross-host TLS, storage backups/restart/recovery, load/backpressure, secure token rotation and rate limits.
- Independently validate declared activities and external effects against owning-system receipts.
- Only then plan any CCB operational cutover. No migration, merge, provider retirement, production deployment or automatically running service is implied by code being present.
