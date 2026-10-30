# Optional Codex desktop observation hooks — source candidate

This directory is a **manually installed, host-trusted Codex desktop plugin
candidate**, not a ChatGPT web integration. It has **not** been installed or
runtime-qualified on Lappy, VN1, VN2, the NAS, or a ChatGPT session.

Official schemas: [Codex lifecycle hooks](https://learn.chatgpt.com/docs/hooks)
and [plugin packaging](https://developers.openai.com/plugins/build/plugins).
Hooks only run after local review/trust of the exact definitions; installing a
web plugin does not deploy their scripts. Do not bypass hook trust.

## What is implemented

The plugin's `hooks/hooks.json` calls `hooks/blotter_hook.py` for:

| Codex event | Observed behavior | Blotter record |
| --- | --- | --- |
| `UserPromptSubmit` | Read bounded latest 20 with `GET /v1/tail` | `codex.turn.started` |
| `PostToolUse` | The host reported a supported tool completed | `codex.tool.observed` |
| `Stop` | The host reported a turn stopped | `codex.turn.stopped` |

The hook **never copies the prompt, assistant message, transcript, tool input,
tool response, ledger event text, or server error body** into Blotter or into
Codex context. Tool names are mapped to a small fixed set of categories; even
syntactically valid but arbitrary tool names are omitted.
Session/turn/tool-call identities are hashed into stable event IDs so a retry
has the same identity. The hook skips Blotter tool calls to avoid recursive
logging. A `PostToolUse` record establishes only that the hook saw the tool
return; it does not verify that an external side effect occurred.

`UserPromptSubmit` currently **does not perform a durable cursor catch-up or
deliver every unseen event to the model**. Its `latest20` call is a bounded
host-only read and is not a `turn.checked` event. Thus this is a partial
instrumentation candidate, **not** satisfaction of Blotter's complete agent
turn contract. Keeping potentially hostile ledger content out of privileged
hook-added context is deliberate.

## Before local installation

1. Review the exact plugin files and the target Codex host's hook support.
2. Install `agent-blotter` into the Python interpreter referenced by the
   `python3` (Unix) or `python` (Windows) hook command; this plugin does not
   bundle or install Python dependencies.
3. Provision **one distinct actor/token per trusted Codex execution identity**.
   One installed hook process configuration binds one actor: subagents that
   inherit it will be attributed to that same host identity, not independently
   authenticated subagent identities. Isolate host configuration if separate
   identities are required; never trust an `agent_id` from hook JSON as actor.
   The host operator must provide `BLOTTER_HOOK_ACTOR`, an approved
   `BLOTTER_URL`, and `BLOTTER_HOOK_TOKEN_FILE`. The token file must be private.
   For a reviewed NAS endpoint on POSIX, also supply
   `BLOTTER_HOOK_PIN_FILE` pointing at the operator-owned restricted file
   containing that exact private HTTPS tailnet endpoint. This uses the same
   `mcp_stdio` pin verification; external pin files on Windows are unsupported
   and fail closed until a handle-bound ACL reader is qualified.
   Do not store tokens in plugin files, repo settings, tool arguments, or
   ChatGPT text. The hook uses Blotter's existing private-token loader,
   strict endpoint allowlist, and TLS-validated direct HTTPS client.
4. Ensure the hook process, but not unrelated model-controlled tools, can
   access the bearer credential. Simply exporting a bearer token or a
   same-user-readable token path into a generic Codex shell environment is
   **not** an isolation boundary. If host-specific credential confinement
   cannot be demonstrated, **do not enable the hook**.
5. Review and trust the exact plugin hook definition in the Codex host. No
   automatic trust/deployment is provided. On Windows, qualify token-file
   ACLs and parent-directory rename/delete permissions before deployment;
   source checks alone do not close the documented Windows TOCTOU caveat.
6. Run a two-actor, independently read-back integration test against one
   central ledger, including a deliberately unavailable-server case. The
   default MCP endpoint remains Lappy's HTTPS URL; an approved NAS endpoint
   requires the separate POSIX operator-owned exact-URL pin configuration.

## Failure and coverage limits

The script returns an explicit generic `systemMessage` warning when parsing,
configuration, authentication, network, or write verification fails. It does
not dump exception text and does not block the Codex tool operation.
An interrupted request can have an ambiguous write outcome. Preserve the
stable event ID and independently reconcile it before any retry. No queue,
retry daemon, reconnect, or arbitrary historical replay is installed.

Hooks may not observe every tool path or event, and `Stop` is not a guarantee
of success. A trusted execution host is needed for comprehensive automatic
capture; this plugin cannot instrument ordinary ChatGPT web conversations,
other agents' private runtimes, or undisclosed model processing.

Run source checks with:

```bash
python -m unittest discover -s tests -p 'test_codex_hooks.py' -v
```
