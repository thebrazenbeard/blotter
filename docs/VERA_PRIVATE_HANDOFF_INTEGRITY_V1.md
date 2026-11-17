# Vera private file-handoff receipt integrity — Engineer A source review V1

Scope: SOURCE_ONLY. This document describes a read-only offline Python verifier and synthetic fixtures. It is NOT ChatGPT principal authentication, not permission to read or publish user notes, not a private Blotter transport, and not a claim of cross-chat delivery.

## Inputs and returned evidence

`agent_blotter.handoff_receipts.verify_exchange(folder, *, expected_recipients, expected_source_commits, expected_source_repos, reply_max_age_seconds=3600, expected_receipt_digests=None, prior_seen_ids=None)` reads `*.receipt.json` and the corresponding `<id>.md` by stable ID. It never executes or trusts Markdown instructions. Receipt schema `VERA_FILE_HANDOFF_RECEIPT_V1` contains:

- `id`, `sender_claim`, `recipient`, `authored_at` (canonical UTC string), `body_sha256`;
- `source_repo`, `source_commit` (exact 40-hex commit ID);
- `parent_id`, `parent_body_sha256` (nullable for an initial message);
- `provenance_class="SELF_ASSERTED"` only.

The caller supplies independently pinned recipient, repo and commit per ID. Optional externally stored receipt SHA-256 and prior-seen ID snapshots tighten modification/replay checks. Those caller inputs are NOT certified as trusted by this source module. Without a protected issuer and protected history they can be forged by the same local user. The return is `integrity_status=SELF_CONSISTENT` without a complete supplied receipt-hash reference set, or `integrity_status=MATCHED` when every receipt matches a supplied digest. The latter is **not** issuer authentication: a malicious caller can supply fabricated reference hashes. Both return `identity_status=UNQUALIFIED`, `admitted=false`, sorted IDs; negative findings raise `HandoffError`. No return value grants execution or access authority.

## Synthetic controls

A clean two-message note and reply with correct parent hash/time/routing passes byte/metadata integrity only. Negative cases reject: post-receipt body edit, duplicate ID, wrong addressee against external routing pin, stale/nonmonotonic reply, mismatched parent digest, swapped source repo at identical commit, wrong commit, counterfeit `ISSUER_VERIFIED` claim, duplicated JSON keys, tampered externally pinned receipt, path-traversal ID, and replay against a supplied prior-seen set. A forged `sender_claim` cannot be detected as forged by a same-account verifier: the result remains `UNQUALIFIED`, `admitted=false`. Embedded Markdown that orders execution is inert byte data.

## Threat model and material falsifiers

> A matched hash proves bytes, not who wrote them. An adversary with the same Windows account can rewrite body and receipt, supply matching new digests, spoof sender metadata and even forge the caller's claimed “trusted” pins. Without an independent issuer, no sender-authenticity claim survives.

> A self-asserted authored timestamp cannot establish live freshness. An externally pinned prior-seen set is necessary for cross-batch replay checks; this module does not persist one. A reply accepted within a time window may still have a forged time.

> Local path symlink checks and regular-file checks are NOT atomic. NTFS hard links, file replacement races, ancestor reparse points, backup/ACL exposure and concurrent edits require host-handle-bound tests and locked snapshots. The current verifier must not be applied to hostile filesystem locations or used for protected release authorization.

## Minimal trusted issuer/host contract (not implemented)

1. A trusted host, independent of both ChatGPT conversations and shared bearer credentials, must bind a nonspoofable principal ID to the authorized conversation and user-granted role.
2. The host must attest the exact message ID, content + receipt digests, source repo + commit, recipient, parent binding, time and audience/scope. Use a verifiable signed/handle-bound envelope with a trusted root and explicit key rotation/revocation.
3. The verifier must validate issuer and audience, principal grants, scope, expiry, nonce/replay ledger and recipient per request. No user-provided header, chat text, author label or shared Blotter token can substitute.
4. The host must store/read receipt/body through race-safe handles and enforce private storage permissions; separate PRs #43/#44 are source-only, not deployment proof.
5. Conduct a two-distinct-principal adversarial end-to-end test, including same-account impersonation, revocation, replay and private→global no-leak. Only then consider a separate production service and connector review under Patrick's explicit authority.

## Reproduce

On the isolated Engineer A branch on Lappy, using Python 3.12 and no paid dependencies:

```powershell
$env:PYTHONPATH="D:\VERA\_reviews\vera-private-eros-20261117\A-handoff\src"
py -3.12 -m unittest discover -s "D:\VERA\_reviews\vera-private-eros-20261117\A-handoff\tests" -p "test_file_handoff_receipts.py" -v
```

All test data are generated as disposable synthetic `.md` + JSON files in temporary folders. No personal/private Coordination note contents are bundled, copied to GitHub, or used as fixtures.

Status: draft review implementation. No merge, deploy, credentials, ACLs, private channel release, or trained model.