# Central Blotter service (source-only deployment instructions)

The central server exists to put **all participating agents' activity records into one shared lane**. It does not send messages, grant command authority, collect ambient data, or provide inboxes.

## Preconditions

- A dedicated host controls a single local SQLite file with adequate storage/backups. Do not put a WAL file on a shared SMB/NFS drive; other hosts connect through HTTP(S), not by mounting the database.
- Register each stable logical agent with a **different, randomly generated** bearer token. Store a credentials JSON file **on the server only**, with an entry per actor: {"agent.one":"<random-long-token>","agent.two":"<different-random-token>"}. These placeholders are not usable tokens.
- Protect credentials at rest. On Unix, permission must be owner-only (e.g. chmod 600). On Windows restrict access using NTFS ACLs. Never commit credentials or export them in logs or issue discussions.
- Server-side non-loopback binding requires a TLS certificate and private key. Use a trusted certificate and a private/VPN network segment; clients validate HTTPS normally.

## Server

Install the Python package then start only one service instance against the chosen central database:

    blotter-serve --db /srv/blotter/activity.sqlite3 --credentials /etc/blotter/agents.json --host 127.0.0.1 --port 8832

Loopback mode is suitable for local agents or a secure pre-existing tunnel. To bind to a private network interface, the service requires explicit TLS:

    blotter-serve --db /srv/blotter/activity.sqlite3 --credentials /etc/blotter/agents.json --host 0.0.0.0 --port 8832 --tls-cert /etc/blotter/tls.crt --tls-key /etc/blotter/tls.key

Non-loopback plaintext HTTP is rejected by design. Choose network restrictions, credential issuance/rotation, monitoring, backup, service restart policy and an approved trust model before production use. The source does not automatically deploy or install any system service. Tokens load on startup; restart after rotation.

## Client — one service for all agents

Set BLOTTER_URL to the same server address for every agent and BLOTTER_TOKEN to each **agent's own** issued token. Do not store these in the repository or pass them on CLI arguments. Use the stable token-associated actor ID.

    blotter check --actor agent.one --session turn-001 --after-seq 0
    blotter record --actor agent.one --session turn-001 --kind tool.executed --message "Observed successful test" --evidence OBSERVATION --event-id tool-attempt-unique-001
    blotter --identity agent.one list --after-seq 0

RemoteBlotter (Python SDK) provides check(), record(), list(). A check itself appends one turn.checked event to the same database. If has_more is true, continue paging with next_seq until every unseen record has been inspected; only then consider this turn's initial synchronization done.

## HTTP contract

- POST /v1/check: session, after_seq, limit (capped at 100); records the authenticated caller's check and returns events + cursor.
- POST /v1/record: kind, message, session, payload, level, evidence, effect, source_ids, event_id, correlation_id, occurred_at. Server derives actor from the token, rejecting unrecognized fields.
- GET /v1/events?after_seq=N&limit=N: authenticated historical read. Filters by actor, kind, session are **views**, never private lanes.
- GET /v1/health: authenticated lightweight response. There are **no commands to other agents** and no message-delivery endpoints.

The central server trusts credential possession for actor identity; it does not independently attest agent code. If a POST fails by timeout/disconnect, the effect is UNKNOWN and should be reconciled using the explicit event ID; never blindly issue a second event. SQLite transactions serialize concurrency; credentials and records remain untrusted inputs.

## Deployment qualification not yet established

Source presence and loopback tests do not qualify: TLS deployment, authenticated cross-host behavior on a real network, endpoint abuse resistance, load with hundreds of active agents, credential revocation without restart, intrusion containment, full-fidelity secret scrubbing, central service recovery, nor automatic instrumentation of every agent's turn. Those require distinct live tests before replacing CCB operationally.
