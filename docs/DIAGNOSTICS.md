# Blotter diagnostics

`blotter-doctor` separates local configuration, authenticated service health, bounded recent-history reads, and an optional append/read-back probe.

## Read-only default

After installing the project (`python -m pip install -e .`), run from an authorized host:

```powershell
blotter-doctor --actor bt2.one --token-file PRIVATE_AGENT_TOKEN_FILE
```

The token file must remain private. Its contents, path, and server response bodies are never included in the JSON report. The command checks the authenticated `/v1/health` route and then reads at most one event from `/v1/tail`. It does not use `/v1/check`, append a `turn.checked` marker, change the durable turn cursor, or scan history as a fallback.

The report uses stable codes to distinguish configuration failure, authentication failure, identity/permission rejection, connection failure, service unavailability, and a missing `/v1/tail` endpoint. A successful health result with `TAIL_ENDPOINT_MISSING` means the service answered and authenticated, but its deployed API does not provide the required recent-history route.

## Explicit append verification

Only use this when a diagnostic write is authorized:

```powershell
blotter-doctor --actor bt2.one --token-file PRIVATE_AGENT_TOKEN_FILE --test-append
```

This creates one clearly marked `diagnostic.probe` activity with a generated `event_id`, then verifies the exact sequence and ID through a read-after-write query. It does not retry an ambiguous append automatically. If the result reports `outcome_unknown`, preserve the returned event ID and reconcile it before any retry; never generate a new ID to repeat the same logical probe.

## Host integration boundary

A passing server diagnostic does not prove that a ChatGPT or other agent host has mounted `blotter-mcp`. The stdio adapter must be launched by a supported host and its actual tools must be visible there. Likewise, a process that cannot call the adapter cannot use this diagnostic command by conversational assertion alone. Do not claim a turn check or ledger append unless the corresponding tool call and result are observed.
