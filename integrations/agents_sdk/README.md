# Optional OpenAI Agents SDK Blotter hooks

For **application-owned** OpenAI Agents SDK runners, `BlotterRunHooks` maps
observed `on_agent_start`, `on_agent_end`, `on_tool_start`, and `on_tool_end`
callbacks to the existing central Blotter API. It is optional and adds no
mandatory dependency to `agent-blotter`. The SDK must be separately installed
in the runner, and this integration must be attached explicitly. It does **not**
inject hooks into native ChatGPT web, Codex, or other unrelated runtimes.

```python
from pathlib import Path
from agents import Agent, Runner
from agent_blotter.remote import RemoteBlotter
from agent_blotter.agents_sdk_hooks import BlotterRunHooks

# All configuration is supplied by the trusted runner, not by model/tool input.
client = RemoteBlotter(operator_url, actor=credential_actor,
                       token=private_token, direct_only=True)
hooks = BlotterRunHooks(client, Path(private_cursor_dir),
                        session=unique_run_session, fail_closed=False)
result = await Runner.run(Agent(name="worker", instructions="..."),
                          "...", hooks=hooks)
if hooks.failures:
    # Surface this run as incompletely observed; reconcile each event_id
    # before retrying an ambiguous network write.
    report_logging_gap(hooks.failures)
```

`on_agent_start` performs a **single** durable cursor catch-up per hook
instance. Handoffs do not silently reset cursors or impersonate a different
identity; one runner hook has one server-bound actor and token. The resulting
`hooks.last_check` is available for deliberate context construction, but is
**not automatically shown to the model**. A full Blotter agent-turn contract
requires the host to expose and actually consume the checked records.

Recorded data is deliberately limited to event kind, constant message,
generated event ID, and status. Even seemingly valid tool or agent names can
contain secrets, so **no dynamic names** are recorded. Prompt text,
arguments, responses, tool results, exception strings and credentials are
never copied into the shared ledger. An error leaves a sanitized record
in `hooks.failures` with the stable event ID; the adapter never retries an
ambiguous append. This failure list is **in-memory only** and disappears if
the runner crashes; it is not a durable outbox. The host must persist safe
failure receipts if crash-surviving reconciliation is required.

This module is packaged as `agent_blotter.agents_sdk_hooks` by normal
`pip install .`; there is no extra dependency on the Agents SDK. The example
above needs the SDK installed separately in the host application.

The SDK lifecycle contract is documented at
https://openai.github.io/openai-agents-python/ref/lifecycle/ . This repository's
tests exercise the callbacks and real authenticated local Blotter HTTP API;
they do not qualify an installed Agents SDK runner or remote NAS transport.
