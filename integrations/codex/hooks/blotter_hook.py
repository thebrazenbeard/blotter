"""Optional, host-trusted Codex desktop lifecycle-to-Blotter bridge.

Never forward prompts, tool arguments, responses, transcripts, or ledger entries.
This is deliberately not a general ChatGPT web hook or full cursor catch-up.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


MAX_HOOK_BYTES = 2_097_152
SAFE_TOOL = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
ALLOWED_EVENTS = frozenset({"UserPromptSubmit", "PostToolUse", "Stop"})


class HookInputError(ValueError):
    """An untrusted hook event lacks the identity required to log truthfully."""


def _event_field(event: dict, name: str) -> str:
    value = event.get(name)
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise HookInputError("missing or invalid Codex hook identity")
    return value


def _event_id(actor: str, session_id: str, turn_id: str,
              name: str, discriminator: str) -> str:
    # Hash the identity tuple so arbitrary event IDs never appear in the
    # shared ledger, while retries retain the same stable event ID.
    encoded = json.dumps([actor, session_id, turn_id, name, discriminator],
                         ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return "codex." + hashlib.sha256(encoded).hexdigest()[:48]


def _tool_category(raw_tool: str) -> str:
    """Avoid persisting arbitrary tool names, even if their syntax looks safe."""
    if raw_tool in ("Bash", "apply_patch", "Edit", "Write"):
        return raw_tool
    if raw_tool.startswith("mcp__"):
        return "MCP"
    return "other"


def handle_event(event: dict, client: Any, *, actor: str) -> dict:
    """Observe a supported lifecycle signal and append metadata only.

    The optional latest20 read is deliberately not a cursor advancement or
    proof that the model consumed the contents. It is not ``turn.checked``.
    """
    if not isinstance(event, dict) or event.get("hook_event_name") not in ALLOWED_EVENTS:
        raise HookInputError("unsupported Codex hook event")
    if not isinstance(actor, str) or not SAFE_TOOL.fullmatch(actor):
        raise HookInputError("invalid host-provisioned actor")
    name = event["hook_event_name"]
    session_id = _event_field(event, "session_id")
    turn_id = _event_field(event, "turn_id")
    discriminator = name
    recent_count = None
    payload = {"event": name}
    if name == "PostToolUse":
        raw_tool = _event_field(event, "tool_name")
        if raw_tool.startswith("mcp__blotter__") or raw_tool.startswith("blotter"):
            return {"skipped": True}
        discriminator = _event_field(event, "tool_use_id")
        tool = _tool_category(raw_tool)
        payload["tool"] = tool
        kind, message = "codex.tool.observed", "Codex tool returned: " + tool
    elif name == "UserPromptSubmit":
        # The raw record texts remain in this private hook process. They are
        # not promoted to developer instructions or copied into a new record.
        recent_count = len(client.tail(limit=20))
        kind, message = "codex.turn.started", "Codex turn started"
    else:
        kind, message = "codex.turn.stopped", "Codex turn stopped"
    eid = _event_id(actor, session_id, turn_id, name, discriminator)
    client.record(session="codex." + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:40],
                  kind=kind, message=message, payload=payload,
                  evidence="OBSERVATION", effect="OBSERVED", event_id=eid)
    result = {"event_id": eid}
    if recent_count is not None:
        result.update({"recent_count": recent_count, "complete_catchup": False})
    return result


def _client_from_private_host_config():
    # All three settings are supplied by the trusted *host*, never from
    # the model-controlled hook JSON. Tokens must not appear on commands.
    from agent_blotter.mcp_stdio import _load_token_file, _validate_config_url
    from agent_blotter.remote import RemoteBlotter

    actor = os.environ.get("BLOTTER_HOOK_ACTOR", "")
    pin_path = os.environ.get("BLOTTER_HOOK_PIN_FILE", "")
    url = _validate_config_url(os.environ.get("BLOTTER_URL", ""),
                               pin_file=Path(pin_path) if pin_path else None)
    token_path = os.environ.get("BLOTTER_HOOK_TOKEN_FILE", "")
    if not token_path:
        raise ValueError("Blotter hook token file is not configured")
    token = _load_token_file(Path(token_path))
    return actor, RemoteBlotter(url, actor=actor, token=token,
                               timeout=2.0, direct_only=True)


def _warning() -> None:
    # Codex interprets systemMessage as a diagnostic. Deliberately omit
    # exceptions: service bodies and host paths can include private data.
    print(json.dumps({"systemMessage":
          "Blotter Codex host hook did not verify this event; inspect private diagnostics."}))


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
        if len(raw) > MAX_HOOK_BYTES:
            raise HookInputError("hook input too large")
        event = json.loads(raw.decode("utf-8"))
        actor, client = _client_from_private_host_config()
        handle_event(event, client, actor=actor)
    except (HookInputError, ValueError, OSError, UnicodeError, ImportError,
            TimeoutError, ConnectionError):
        _warning()
    except Exception:
        # Network and JSON protocol libraries can raise implementation-
        # specific errors. Never expose their text or claim the write failed;
        # a timed-out write may have committed and needs event-ID recovery.
        _warning()
    return 0  # An unavailable logging service must not block Codex actions.


if __name__ == "__main__":
    raise SystemExit(main())
