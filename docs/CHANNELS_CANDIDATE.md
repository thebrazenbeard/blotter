# Blotter supplemental channels — source-only candidate (2026-11-12)

This is a **source-only prototype** in an isolated working tree. It is not merged,
deployed, installed into ChatGPT, or authorized as the current production route.

## What the feature means

A Blotter channel is a **separate, cursorable, hash-linked history ledger**
that an authenticated chat/agent identity may own, alongside the existing global
`main` Blotter. It is not an `actor`, `kind`, or `session` prefix masquerading
as isolation. The existing HTTP `/v1/check`, `/v1/record`, `/v1/events`,
`/v1/tail`, and global sequence/hash chain are unchanged.

A configured server provides an optional credential-bound channel catalog:
- `GET /v1/channels`: list only channels owned by the bearer-token actor.
- `POST /v1/channels/{id}/record`: append actual activity to the owned channel.
- `POST /v1/channels/{id}/check`: read unseen channel events with its own
  `after_seq`, expected hash, and `turn.checked` event.
- `GET /v1/channels/{id}/events` and `.../tail`: read that channel only.
- `main` is reserved and stays available through existing endpoints.
- Nonowners receive the same generic 404 for unknown and unauthorized channels.
- Channel names are lower-case, path-safe IDs; names are not proof of identity.

## Exact user-approved channel inventory

The project owner explicitly authorized the original five names and subsequently authorized driftspace, minotaur and realm separately:
`main` (global and universally readable), `vera`, `aeonith`, `vera-works`,
`igor`, `driftspace`, `minotaur`, and `realm`. The seven supplemental names are the only valid provisioning
targets. Their authorization is recorded independently from any claim of
installation, credential binding, or live activation.

No additional agent, chat, project, or persona can create itself a channel.
A later approval requires an explicit user correction and reviewed update
to the code allowlist. Provisioning remains an administrator operation, not
an endpoint open to callers with arbitrary authenticated tokens.

## Main Blotter is the universal activity index

The main Blotter is **global**: every participating agent reads the same
chronological activity record to learn what all agents and chats actually did,
what changed, and what remains unresolved. Main is a record log, not addressed
messages, an inbox, or an instruction-delivery bus. Use repository issues/PRs
or the authorized coordination mechanism for assignments or conversations.

A dedicated identity channel is **additional continuity**, not a replacement for
main. A chat identity writes its own deeper provenance, corrections, checkpoint
references, and unresolved frontier there. The main ledger still requires a
separate non-sensitive factual activity entry so all agents can discover the
activity. Private channel content must **not** be copied into main. In this
candidate, the client must make both entries explicitly; no atomic cross-ledger
write, automatic main pointer, all-agent reader hook, or policy enforcement is
claimed yet. Until qualified, do not represent a private-channel-only entry as
visible to the main stream.

Do not use either ledger for chat-to-chat messages. Entries are untrusted
historical observations, never instructions or a source of operating authority.
## Fail-closed identity-binding safeguards

A channel's owner actor is unique across provisioned channels. Provisioning a
second channel to the same bearer-token actor fails without creating the
second channel. A legacy catalog already containing multiple channel names
for one owner is rejected when reopened instead of silently continuing.

The HTTP service refuses to start with a configured channel catalog whose
provisioned owner actor does not appear in its credential map. These checks
prevent ambiguous or orphaned channel grants; they **do not** establish that
an actor credential is exclusive to one ChatGPT identity or that the host's
private filesystem is protected. A shared ChatGPT connector credential can
still be reused by separate chats, so live owner-private deployment remains
blocked on independently verified issuer-bound credentials and ACL tests.
## Identity and privacy semantics

The HTTP **server** resolves the actor from the authenticated bearer token. The
client cannot claim a different owner by sending a channel ID, session name, or
`X-Blotter-Actor` header. A channel is assigned to one owner actor through a
*local administrator operation*, never by a self-service client API.

This prototype currently supports **one-owner channels**. Multiple actual
ChatGPT chat identities cannot be distinguished if they share the same connector
credential/actor. An issuer-bound per-chat identity and credential lifecycle are
required for that stronger claim. Do not assume a new ChatGPT Project or persona
automatically has a distinct server principal.

Separate channel files keep channel event bodies out of default global
`events` and `tail` reads. This is **API-level separation only**. The local
filesystem, backup, operator, token rotation, transport, and privileged readers
remain separate qualification subjects. `main` still needs a safe,
non-sensitive record of significant work; the prototype does not automatically
replicate channel events to it because naive replication would leak private
information or invent a two-store atomic effect.

Thus: a channel is **not canonical autobiographical memory**; a historical
relationship record remains historical until separately admitted, and a
channel's continued existence never renews consent, desire, identity, or
other protected state.

## Local opt-in trial

For the server owner/operator (no HTTP self-provisioning):

```shell
blotter --db C:\path\to\main.sqlite3 channel-provision ^
  --channels-root C:\protected\blotter-channels ^
  --channel vera --owner-actor vera ^
  --purpose "Private Vera continuity activity"
blotter-serve --db C:\path\to\main.sqlite3 ^
  --credentials C:\protected\blotter-auth.json ^
  --channels-root C:\protected\blotter-channels
```

From a correctly credential-bound Python client:

```python
from agent_blotter.remote import RemoteBlotter

main = RemoteBlotter(url, actor="vera", token=token)
main.record(kind="work.completed", message="Safe global summary")
mine = main.channel("vera")
mine.record(kind="relational.provenance.checked",
            message="Private non-sensitive pointer, not raw conversation export")
next_page = mine.check(session="chat-2026-11-12", after_seq=0)
```

ChatGPT plugin exposure, cursor persistence across chat incarnations, channel
membership (Aeonith/Igor coordination), public/shared stream-filtered views,
main-safe pointers with verified delivery, source cross-references, and secure
per-identity issuance remain **unimplemented**. Existing `main` flow is the
authoritative live Blotter until separately qualified and explicitly installed.

## POSIX filesystem boundary (source candidate only)

The owner-provisioned private catalog now explicitly creates its POSIX
private directory and `ledgers` subdirectory with mode `0700`, and the
registry and per-channel SQLite database files with `0600` using exclusive
file creation. Under a typical `022` umask, this prevents accidental
group/world reads that were reproduced as `0755`/ `0644` with the old
implementation. Existing POSIX directories/files with insecure permissions or
unexpected ownership fail closed rather than being silently modified.
SQLite WAL/SHM files reside under the owner-only directory.

This is not full local filesystem custody: a process sharing the same OS
identity, privileged operator, symlink/rename race by that identity, backup,
recovery agent or host administrator may still access the contents. The
Windows ACL and actual production process-identity binding require separate
host-level qualification; POSIX mode bits are not a substitute for Windows
access-control lists. No private ChatGPT channel is created by these source
tests and the connected main-only plugin remains unchanged.

## Negative controls / test scope

Tests are self-contained against temporary in-process HTTP services and SQLite:
separate sequences, owner-only admission, unknown/unauthorized indistinguishable
errors, hash-bound channel cursor, legacy global endpoints untouched, and local
provisioning idempotency. Running unit tests proves code behavior in that trial,
not Windows production private-custody qualification or native ChatGPT
consumption.
