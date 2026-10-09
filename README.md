# Blotter

Blotter is one shared, chronological activity log for every participating agent. It is a **blotter**, not a communication bus: no inboxes, addresses, delivery routes, replies, or per-agent lanes.

Its contract is simple:

1. **At the start of each turn:** an agent checks the one shared ledger, reading every unseen entry since its previous cursor. If the response says more pages exist, it continues checking until the backlog has been read.
2. **During the turn:** it records each actual activity (tool call, result, edit, decision, observation, test, error, or other action), including who acted and what evidence the record rests on.
3. **At the next turn:** it checks again, including activity from every other agent since the last read.

A single globally ordered stream replaces the operational fragmentation of the old chat-communication-bus model. Blotter does not itself send messages or run agent actions.

## Current scope

This source implements a **local, single-host Python/SQLite prototype**, not a live cross-host service. All processes using the same local database file see the same monotonically sequenced log. For multiple workstations, a shared centralized service with authenticated read/write adapters remains to be built. Merely cloning this repository on separate machines does not make their logs shared.

Requires Python 3.10+, no third-party runtime packages. The default is **one ledger per host** at ~/.blotter/activity.sqlite3 regardless of the working repository. Every agent on the host must use this same path (or the same BLOTTER_DB override), never a separate file per agent or repo. This does **not** synchronize different hosts. From the repository root:

    python -m pip install -e .
    blotter init
    blotter check --actor agent.one --session turn-01 --after-seq 0
    blotter record --actor agent.one --session turn-01 --kind tool.executed --message "Ran unit tests" --evidence OBSERVATION --payload '{"exit_code":0}'
    blotter check --actor agent.two --session turn-02 --after-seq 0
    blotter list
    blotter verify

For an uninstalled checkout, set PYTHONPATH=src and replace the blotter command with python -m agent_blotter. On PowerShell: $env:PYTHONPATH='src'.

**Cursor handling:** a check returns events, check_event, next_seq, has_more, and visible_head_seq. Persist next_seq **per agent** between turns. Read all events returned. If has_more is true, check again using next_seq before doing other work. A cursor that points beyond this ledger's current head is rejected rather than silently skipping history. The act of checking is itself appended to the same stream as a turn.checked event. Filtering list by agent, session, or kind creates a view and **never** another lane.

## Python use

    from agent_blotter import Blotter
    from pathlib import Path
    ledger = Blotter(Path.home() / ".blotter" / "activity.sqlite3")
    view = ledger.check(actor="agent.one", session="turn-01", after_seq=0)
    while view["has_more"]:
        # consume view["events"] before obtaining the next page
        view = ledger.check(actor="agent.one", session="turn-01", after_seq=view["next_seq"])
    cursor = view["next_seq"]  # save for this agent's next turn
    ledger.record(actor="agent.one", session="turn-01", kind="test.executed",
                  message="Ran test suite", evidence="OBSERVATION", effect="REPORTED",
                  payload={"exit_code": 0, "command": "python -m unittest"})

Every entry includes its actor, turn/session, activity kind, message, evidence classification, claimed effect state, optional structured details, UTC occurred/recorded times, a unique event ID and a global sequence. A SHA-256 chain supports internal integrity checks, not authentication or independent proof that the claimed action occurred.

The system has no secret scanner for messages or arbitrary values. It redacts only some sensitive-looking JSON keys. Never log credentials or sensitive personal information. No auto-capture, surveillance, external telemetry, execution privileges or network listener.

The implementation is reviewed as a source candidate; agent integrations, central storage, per-turn automatic hooks and continuous operation are **not installed or verified** by this repository alone.

See docs/ARCHITECTURE.md and docs/SOURCE_REVIEW.md. No third-party code has been copied. No repository-wide redistribution license is assumed.

## Tests

    python -m unittest discover -s tests -v
