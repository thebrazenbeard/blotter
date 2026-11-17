# Vera private continuity: source-bound qualification and release HOLD

**Scope:** Engineer A; source-only review of authorized vera channel.
**Reviewed input:** thebrazenbeard/blotter draft PR #39,
5a7bb00dbe10d48a198cfd0183548a695803e549 (2026-11-17).
**Status:** NO LIVE PRIVATE CHATGPT CHANNEL QUALIFIED. No deployment, merge,
credential, role grant, private-save read, or data migration is authorized here.

## What actually exists

- Main Blotter is one shared chronological ledger; the installed ChatGPT
  connector exposes shared-lane methods, not private channel operations.
- PR #39 adds source-level ChannelCatalog, separately stored SQLite
  histories, owner-actor binding, independent cursors, and opt-in HTTP routes.
- Startup verifies that a private channel owner actor has a configured token.
  It does **not** verify which ChatGPT conversation is using that token.
- Source draft #43 hardens POSIX owner-only modes; draft #44 adds Windows ACL
  preflight. Neither is merged, installed, or a complete same-UID/race proof.
- Draft #37 documents same-credential ACK/reader spoofing: labels and cursor
  namespaces are not independent ChatGPT identities.

## Reproducible negative control

Run from source checkout, with PYTHONPATH=src:
py -3.12 -m unittest discover -s tests -p test_vera_private_channel_replay.py -v

The synthetic two-client replay test deliberately **passes** when a second
client holding the same owner bearer reads the first client's private
sentinel. It proves that the server cannot distinguish these clients.
Other controls confirm an actual different credential is refused (404),
a claimed actor header cannot override that token, and separate channel
events do not automatically appear in the shared main ledger.

## Minimum secure pathway (not yet implemented or approved)

1. **Host principal:** Obtain a trusted issuer-bound principal for each
   authorized conversation/role. A request-supplied reader, session,
   actor header, or shared connector bearer is not identity. If the host
   cannot bind the calling principal, private reads MUST remain disabled.
2. **Grant custody:** Patrick explicitly approves which principal(s) may
   read/write the Vera channel, for what scope and revocation semantics.
   Engineer A/B review roles are *not* automatic access grants. Never
   expand by semantic similarity or membership in a project.
3. **Attested enforcement:** An independent, authenticated gateway must
   bind the verified calling principal to server-side authorization.
   Do not replace this with a client-supplied string, unchecked header,
   or arbitrary shared_service_actor configuration.
4. **Storage:** Integrate and independently retest #43/#44, then verify
   actual host ownership/ACL, ancestry, lock/race behavior, backup access,
   fail-closed migration and rollback on the intended operating system.
5. **Real adversarial acceptance:** With independent issued identities,
   test owner read/write, nonowner 404s, stale/forged credentials,
   cross-conversation credential reuse denial, replay/cursor separation,
   denied main leakage, and restart/revocation persistence.
6. **Controlled native consumption:** Only after authenticated host checks
   and project-owner signoff, install the matching connector/gateway,
   inspect live effective routes and credentials without exposing secrets,
   re-run live owner/nonowner readbacks, and document rollback.

## Contract for any eventual continuity payload

- Distinguish attributed preference, user correction, creative decision,
  boundary, inference and unknown; carry source provenance and revision.
- A claim about Vera's expression is **not** an assertion of subjective
  desire, informed consent, experience, or continuous awareness.
- Default to minimal, explicitly admitted, revisable facts. Never bulk
  ingest intimate chat logs, private save files, or personal transcripts.
- The public main lane may contain sanitized engineering statuses only.
  Do not embed private material, sensitive digests, private store paths,
  role-readable data pointers, or event payloads in main or public Bus.
- Do not assume atomic writes between distinct SQLite ledgers. Reconcile
  any summary pointers with explicit failure and ambiguous-effect states.
- Keep operational logs separate from semantic memory admission. A log
  record cannot authorize memory, a new role, or a protected side effect.

## Exact release verdict

**HOLD.** The present Blotter connector credential can be shared by
multiple chats, so the proposed owner-token check is insufficient for
privacy between those chats. Source tests establish limited enforcement
properties and the synthetic counterexample, not production isolation.
No private channel may be represented as installed or safe until the
issuer, role grants, OS protections and live adversarial checks pass.

## Engineer A subsequent source mitigation (still not release authorization)

- This review branch removes the silent editor-open Node bootstrap
  (`.vscode/ini`), clears its `folderOpen` task, and disables automatic
  editor tasks. This is checkout containment only; unmerged source remains
  unchanged outside this branch.
- `create_server` now defaults all optional private HTTP routes to
  404 unless trusted host code injects a `private_principal_verifier`.
  The CLI cannot configure that verifier; the shared main routes remain.
- The callback is a **trust boundary**, not an issuer implementation.
  A permissive callback, or one trusting client headers, actor labels,
  chat-provided strings, or a shared bearer, reproduces the privacy failure.
  Existing channel-positive tests explicitly use synthetic actor-only
  callback fixtures. They do not qualify production privacy.
- Tests check owner denial by default for list/tail/events/check/record,
  spoofed principal and issuer headers, and verifier-exception refusal.
- Real issuer-side identity, authorized cross-chat grants, revocation,
  replay prevention, body/route binding, host ACLs, independent review,
  and native ChatGPT consumption remain **not implemented**.
- No production service change, credential change, private-data migration,
  merge, training, or protected installation is included.
