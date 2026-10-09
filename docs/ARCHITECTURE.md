# Architecture — one shared activity lane

## Primary invariant

**ONE LOG, ALL AGENTS, ALL ACTIVITIES.** Every participating agent inspects the same chronological log during each turn, then records the activities it performs to that same log. An identity is a record field, not a separate writer lane. The log does not replace agent identity, authority, or task scheduling.

This design supersedes the *fragmented visibility* problem of chat-communication-bus. It is **not** the bus reimplemented with new names: no message addressing, subscriptions, replies, inboxes, DLQ, mailboxes, delivery acknowledgements, or communication routing. Agent cooperation arises from looking at the same record.

## Implemented local prototype

- Python 3.10+ standard library and one SQLite file, schema v1, WAL, busy timeout, explicit BEGIN IMMEDIATE writer transactions.
- Events are immutable through the public API and globally sequenced in one events table. All agents write into the same table regardless of actor/session.
- check(actor, session, after_seq, limit) obtains a consistent pre-check snapshot while holding a write transaction, returns unseen activities, and appends a turn.checked observation into the same stream. If has_more is true the agent must continue paging; it cannot claim to have read the full backlog.
- record(actor, kind, message, payload, evidence, effect, ...) appends an activity. Idempotent explicit event IDs detect mismatched retries. Local event source references must exist before insertion. Records are data and confer no execution authority.
- list, export and verify read one stream. Filters produce views only. verify checks local hash-chain continuity and mirrored indexed fields; it cannot prove a malicious privileged writer did not rewrite the database.
- Connections are explicitly closed after each operation, including on Windows.

A SQLite database is not itself a cross-host synchronization mechanism. WAL databases must remain on supported local filesystems. This branch additionally implements an **optional central HTTP service and remote client** backed by one SQLite ledger: per-agent bearer credentials bind actor identity; the server defaults to loopback and requires TLS certificate/key for non-loopback binding. The client refuses non-loopback cleartext HTTP. The source tests exercise a shared sequence across agents, credential isolation and concurrent writes. This is not proof of an installed central host or secure production deployment. Multiple independently local ledgers do **not** satisfy the product goal.

## Observability and evidence

An event is a claim about an action, not automatic verification. An agent may mark a report as OBSERVATION, RETRIEVED, INFERENCE, etc., and an effect as REPORTED, OBSERVED, VERIFIED, or UNKNOWN; these remain agent assertions until verified against underlying independent evidence. Provenance can include exact external artifact hashes, runner receipts, Git heads, or Tattler sampling source details in the payload. No automatic collection is present.

P.O.R.T.A.L. remains orchestration/execution; Blotter is an activity record. CCB/ccb-core informed the distinction between logical identity and session, safe retry, timestamp vs ordering, and delivery vs incorporation. None of their route/assignment machinery is imported. Tattler can later contribute evidence-bearing sampled observations, with explicit opt-in and no concealed monitoring.

## Gaps before a real replacement

1. Qualify the already implemented authenticated single-authority network server in an actual controlled cross-host deployment, including TLS credential lifecycle, failure/recovery, backup/restore and replay semantics; source-level loopback tests are insufficient.
2. Turn hooks/adapters that actually cause **every agent** to check and append **every** action, with persistent per-agent cursors and evidence about coverage; current APIs cannot force unaffiliated runtimes to comply.
3. Efficient whole-log search and bounded catch-up when thousands of agents produce many records; current cursor pagination avoids whole-file reads but not operational backlog.
4. Tamper-resistant receipts, access policy, backpressure, backups/retention, sensitive-data controls, reconciliation and recovery from disconnected agents.

Do not claim global operational awareness, replacement cutover, or audit-grade authenticity until these are implemented and independently verified.
