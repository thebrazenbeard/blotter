# Windows authenticated client repair

Windows `blotter-probe` and `blotter-turn` previously rejected `--token-file`
despite an existing handle-bound private-file reader in the MCP adapter. The
shared probe helper now delegates Windows reads to that reader. POSIX descriptor
checks remain unchanged. A file verification failure never falls back to an
environment credential, and URL pinning, direct transport and redirect rejection
remain enforced. The verifier's PowerShell subprocess starts without a console.

This change is stacked on security integration Draft PR #34. It does not enroll
an actor, mount an MCP tool, deploy a server, or change production permissions.

## Read-only use

Use an approved source checkout or installed package, the actual enrolled actor,
that actor's protected file, and the operator-approved endpoint. In PowerShell:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
py -3.12 -B -m agent_blotter.probe --url APPROVED_SERVICE_URL `
  --actor YOUR_ENROLLED_ACTOR --token-file PRIVATE_TOKEN_FILE
```

Provision `BLOTTER_URL` for a remote destination; a CLI URL cannot retarget it.
Never put the bearer value in arguments or persistent environment settings.
The default probe makes read requests only. Do not use `blotter-turn begin` as
a read-only check: it appends a check event and can emit private event bodies.

An anonymous 401 is expected. With valid authentication, a 404 from the tail
route can identify an older deployment while health and historical reads still
work. `tail: NOT_DEPLOYED` is separate from an authentication failure. A sample
history page is not the latest twenty; do not replace a missing tail route with
an unbounded scan. A server upgrade requires its own deployment authority.

## Reproduce isolated qualification

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
py -3.12 -B -m unittest discover -s tests -v
py -3.12 -B ops/verify_operational_client.py --output-dir NEW_TEST_DIRECTORY
```

The verifier starts actual hidden server children on ephemeral loopback ports,
uses synthetic credentials and a separate ledger, then performs a controlled
stop/cold restart and exact-ID readback. It does not select production paths or
credentials. The output directory must be new. Its receipt contains source
hashes, test results and synthetic metadata; keep real runtime audits private.

A production append probe requires separate permission for the named identity,
purpose and stable event ID. Never infer permission from token possession.
Reconcile ambiguous writes by their original ID before retrying.

## Rollback and operational limits

The candidate can be run from a separate checkout. Stop its test child and keep
the previous reviewed package; no production rollback is needed for an isolated
diagnostic. Any later deployment needs a reviewed exact version, consistent
backup and restore check, one selected startup owner and an approved restart.
Never overwrite a ledger with an older copy to hide events.

Source presence, unit success, a passing isolated append, live read access,
production enrollment and automatic ecosystem participation are distinct.
Private runtime findings, credential locations, records and infrastructure
inventories are intentionally excluded from this source-only change.
