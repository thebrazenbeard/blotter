# Codex host integration

This package provides two complementary surfaces: acknowledged MCP history
pages and optional host lifecycle hooks. Installing Python source alone enables
neither surface in an existing Codex chat.

The MCP server is bound to one enrolled actor, its protected token file, and the
fixed private HTTPS endpoint. `read_turn_page` returns one unseen page plus a
receipt; `acknowledge_turn_page` advances that reader only after the caller has
read the complete page. An unread or truncated page must not be acknowledged.
An exact pending replay can belong to an older session. Read and acknowledge
that page, then request another page for the current session even when the
replayed page says `has_more: false`. Finish only after acknowledging a page
whose `session` matches the current requested session and whose `has_more` is
false. Existing `begin_turn` remains the older
whole-backlog interface; the hooks request the acknowledged page interface.

## What the hooks do

| Hook | Behavior |
| --- | --- |
| `UserPromptSubmit` | Supplies short instructions and a stable reader scope for full acknowledged catch-up. No network, credential read, or cursor advance. |
| `SubagentStart` | Supplies the same instructions using a separate child reader scope. Does not enroll a new actor. |
| `PostToolUse` | Appends one host-observed tool-return record and checks the exact receipt. |
| `Stop` | Records that the host observed a turn stop. Does not claim task success. |

Reader scopes hash the fixed actor, host session ID, and tagged root/child ID.
They persist across turns of the same chat and prevent one chat or child from
acknowledging another's unseen history. They are delivery tracking within one
authenticated host principal, not proof of separately authenticated subagents.
Use separate trusted launch configurations for distinct authenticated actors.
Role names and model-supplied tool arguments never select a token or identity.

The observer copies only fixed tool categories, hashes, event type, and known
numeric exit-code/boolean-error metadata. It recognizes Codex's canonical
`Bash` name and fixed `exec_command`/`write_stdin` aliases, `apply_patch`, code
mode, and a generic MCP category. It never copies prompts, commands, file paths,
tool response text, transcripts, assistant messages, arbitrary tool names, or
ledger bodies. A zero exit code describes a command's observed return; it does
not verify an external effect. Use `record_activity` for substantive decisions,
errors, outcomes, and evidence that this metadata observer cannot describe.

The observer skips calls to its configured Blotter MCP server, including page
reads, acknowledgements and explicit activity appends, to avoid recursive logs.
Event IDs hash the actor, session, turn, event, tool-call identity and reader.
Repeating the same event keeps its ID; distinct children cannot collide even
when they reuse tool-call IDs.

## Prepare and register the host

1. Install the reviewed package into a dedicated trusted interpreter. Use an
   ordinary installed package, not an editable workspace installation. Protect
   its interpreter, package, parent directories, launch configuration, and
   private token file from unrelated model-controlled writes.
2. Register the package's existing `agent_blotter.mcp_stdio` module as the
   `blotter` MCP server with its enrolled `--actor`, protected `--token-file`,
   and private `--state-dir`. Launch it with the interpreter's absolute path
   and `-I -m agent_blotter.mcp_stdio`. Verify the advertised tools include
   `read_turn_page`, `acknowledge_turn_page`, and `record_activity`.
3. Prepare these host-owned environment settings for hook commands:

   | Setting | Purpose |
   | --- | --- |
   | `BLOTTER_HOOK_ACTOR` | The same actually enrolled actor as the MCP process. |
   | `BLOTTER_URL` | The fixed approved Lappy HTTPS endpoint. No external endpoint pin is supported here. |
   | `BLOTTER_HOOK_TOKEN_FILE` | Absolute path to that actor's protected token file. Never a bearer value. |
   | `BLOTTER_HOOK_STATE_DIR` | Absolute private directory for bounded diagnostic-condition files; separate from cursor state. |
   | `BLOTTER_HOOK_MCP_SERVER` | Optional registered server name; defaults to `blotter`. Use the same name as registration. |

   Use the Windows opened-handle ACL/reparse reader or the existing verified
   POSIX reader. A failed private-file check never falls back to `BLOTTER_TOKEN`.
   Requests use direct transport and the existing fixed HTTPS allowlist.
   Same-user environment variables are configuration, not credential isolation;
   the execution host must enforce the intended credential access boundary.
4. Take the hook definitions from `integrations/codex/hooks/hooks.json` and
   replace **both** interpreter placeholders with the actual reviewed absolute
   paths. The placeholders deliberately do not fall back to PATH or expand an
   inherited interpreter variable. The command is `-I -m
   agent_blotter.codex_hooks`; `PYTHONPATH` and the current working directory
   must not supply the imported package.
5. Register the exact hook definition in the supported host configuration or
   plugin. Review and trust that exact definition through Codex's hook review
   interface. Do not overwrite existing hook definitions or bypass trust.
   Refresh/restart the relevant host session as required and observe actual
   hook invocation; files on disk alone are not installation evidence.

Current host events, command layout and trust behavior are described in the
[official Codex hooks reference](https://learn.chatgpt.com/docs/hooks). This
source was checked against installed Codex CLI 0.153.4; another host or version
needs its own qualification. The templates are not installed automatically.

## Failure behavior and verification

Ordinary work continues when host logging fails. A private status file keeps
the last diagnostic condition and up to 64 unconfirmed event IDs; it is not a
second ledger, a queue, or a cursor. Recovery preserves prior unresolved IDs.
Only a confirmed matching-ID retry removes that particular retained ID. If
retention overflows, a sticky incomplete-retention flag and overflow-attempt
count remain explicit; this count is not a count of unique missing events.
A warning appears when the condition changes, when ID retention first
overflows, and once when a later observation gets a matching receipt. A successful instruction
hook cannot announce logging recovery because it has made no service request.
Provision writable diagnostic storage to suppress warnings across short-lived
hook processes. Storage failure produces a generic warning without error text.

Failed or ambiguous observations are **not retried automatically**. Their
stable IDs support deliberate reconciliation; this version does not queue every
missed observation during an outage or retain unlimited unresolved IDs. An
incomplete-retention flag cannot be cleared by an unrelated successful event.
Do not turn an unconfirmed append into a
success claim or repeat it with a new ID. Raw service errors never reach Codex.

Qualify the installed path using synthetic credentials and a separate ledger:

- Observe hook start context in an actual parent and child context; confirm
  stable reader scopes across turns and separation between contexts.
- Page through a backlog, acknowledge only complete pages, and verify that
  unread pending pages replay and the next turn is incremental.
- Run a successful command, a nonzero command, and a file patch; independently
  read back exact event IDs, safe categories and observed return metadata.
- Repeat one event and prove exactly one stored ID; use two children with the
  same tool-call ID and prove separate observations.
- Exercise invalid credentials, an unavailable service and a malformed receipt;
  check that no secret appears, repeated warnings are suppressed, and a real
  recovery is reported once.
- Verify a Blotter read/ack/record does not recursively append another record.

The source-focused check is:

```text
PYTHONPATH=src python -m unittest discover -s tests -p test_codex_hooks.py -v
```

This test command imports the checkout for development only. Host commands use
the separately installed package with `-I`. Synthetic tests do not prove a
central deployment, host installation, hook trust, or cross-host operation.
Hooks do not observe hosted tools such as web search or guarantee every
specialized tool path. They do not instrument ordinary ChatGPT web sessions or
prove private reasoning, universal capture, or actual model consumption.
