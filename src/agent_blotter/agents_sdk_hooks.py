"""Observed lifecycle events from an application-owned OpenAI Agents SDK runner.

The optional Agents SDK is *not* needed to import or test this module. When
installed, BlotterRunHooks subclasses the SDK's RunHooks type. This does not
install lifecycle hooks in native ChatGPT web conversations.

Construct one instance with a fixed, credential-bound RemoteBlotter per SDK
run. In a multi-agent handoff, the actor represents this *runner principal*;
no dynamically supplied agent or tool names enter the payload. Do not share
this one token as multiple purported actor identities.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import uuid
from typing import Any

from agent_blotter.store import _valid_id
from agent_blotter.turn import begin_turn

try:  # Optional integration; agent-blotter itself has zero runtime dependencies.
    from agents import RunHooks as _SDKRunHooks
except ImportError:  # Unit tests and non-SDK Blotter installs use the same methods.
    _SDKRunHooks = object


class BlotterRunHooks(_SDKRunHooks):
    """A bounded host-side observer for SDK RunHooks callbacks.

    Failures are visible to the caller as sanitized ``failures`` entries;
    ``fail_closed`` can instead stop an execution whose Blotter guarantee is
    mandatory. Network failures are never retried automatically because an
    append might already have committed. ``begin_turn`` fetches all unseen
    events and persists a cursor, but it does NOT put those events into an
    agent's model context. The application must do that deliberately.
    """

    def __init__(self, client: Any, state_dir: Path, *, session: str,
                 fail_closed: bool = False):
        self.actor = _valid_id(client.actor, "actor")
        self.session = _valid_id(session, "session")
        self.client = client
        self.state_dir = Path(state_dir)
        self.fail_closed = bool(fail_closed)
        self.failures: list[dict[str, str | None]] = []
        self.last_check: dict | None = None
        self._begin_attempted = False
        self._begin_lock = asyncio.Lock()

    def _failure(self, kind: str, event_id: str | None) -> None:
        # No original exception or server body: both might contain secrets.
        self.failures.append({"kind": kind, "event_id": event_id,
                              "effect": "UNKNOWN"})

    async def _begin_once(self) -> None:
        if self._begin_attempted:
            return
        async with self._begin_lock:
            if self._begin_attempted:
                return
            self._begin_attempted = True  # No blind repeat of an ambiguous POST.
            try:
                self.last_check = await asyncio.to_thread(
                    begin_turn, self.client, self.state_dir, session=self.session)
            except asyncio.CancelledError:
                self._failure("turn.checked", None)
                raise
            except Exception:
                self._failure("turn.checked", None)
                if self.fail_closed:
                    raise RuntimeError("Blotter turn synchronization unavailable") from None

    async def _record(self, kind: str, message: str, payload: dict[str, str],
                      effect: str) -> None:
        event_id = "sdk-" + uuid.uuid4().hex
        try:
            await asyncio.to_thread(
                self.client.record, kind=kind, message=message,
                session=self.session, event_id=event_id,
                evidence="OBSERVATION", effect=effect, payload=payload)
        except asyncio.CancelledError:
            self._failure(kind, event_id)
            raise
        except Exception:
            self._failure(kind, event_id)
            if self.fail_closed:
                raise RuntimeError("Blotter event append unavailable") from None

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        await self._begin_once()
        await self._record("agent.started", "Agents SDK agent started",
                           {}, "OBSERVED")

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        await self._record("agent.finished", "Agents SDK agent finished",
                           {}, "OBSERVED")

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        await self._record("tool.started", "Agents SDK local tool invoked",
                           {}, "ATTEMPTED")

    async def on_tool_end(self, context: Any, agent: Any, tool: Any,
                          result: Any) -> None:
        await self._record("tool.finished", "Agents SDK local tool returned",
                           {}, "OBSERVED")
