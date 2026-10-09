# Agent Blotter: shared turn activity

This directory contains one single-lane activity ledger library, not a messaging bus. For the intended turn contract, read AGENTS.md, and do not introduce per-agent lanes or message routing.

The Python library is source-only unless a host actually calls it. Agent identities are record labels, not authenticated credentials. No external integrations, live services, or destructive migrations are authorized by this file.
