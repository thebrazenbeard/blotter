"""Optional Agents SDK host-hook integration, without model-controlled identities."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_blotter import Blotter
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server
from agent_blotter.agents_sdk_hooks import BlotterRunHooks


class AgentsSDKHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Blotter(Path(self.temp.name) / "activity.sqlite3")
        self.token = "T" * 40
        self.server = create_server(self.store, {self.token: "sdk.runner"}, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.client = RemoteBlotter(f"http://127.0.0.1:{self.server.server_port}",
                                    actor="sdk.runner", token=self.token,
                                    direct_only=True)
        self.hooks = BlotterRunHooks(self.client, Path(self.temp.name) / "cursors",
                                     session="run-001")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        self.temp.cleanup()

    async def test_actual_remote_records_and_no_sensitive_arguments(self):
        ctx = SimpleNamespace(tool_arguments="token=secret-value")
        agent = SimpleNamespace(name="planner")
        tool = SimpleNamespace(name="lookup")
        await self.hooks.on_agent_start(ctx, agent)
        await self.hooks.on_tool_start(ctx, agent, tool)
        await self.hooks.on_tool_end(ctx, agent, tool, "API_SECRET_DO_NOT_LOG")
        await self.hooks.on_agent_end(ctx, agent, "TOP_SECRET_OUTPUT")

        events = self.store.list(limit=100)
        self.assertEqual([e["kind"] for e in events],
                         ["turn.checked", "agent.started", "tool.started",
                          "tool.finished", "agent.finished"])
        self.assertTrue(all(e["actor"] == "sdk.runner" for e in events))
        self.assertTrue(all(e["payload"] == {} for e in events[1:]))
        self.assertNotIn("API_SECRET_DO_NOT_LOG", str(events))
        self.assertNotIn("TOP_SECRET_OUTPUT", str(events))
        self.assertNotIn("secret-value", str(events))
        self.assertEqual(len({e["id"] for e in events}), len(events))
        self.assertEqual(self.store.verify()["count"], len(events))
        self.assertEqual(self.hooks.failures, [])

    async def test_handoffs_do_not_repeat_turn_check(self):
        ctx = SimpleNamespace()
        await self.hooks.on_agent_start(ctx, SimpleNamespace(name="one"))
        await self.hooks.on_agent_start(ctx, SimpleNamespace(name="two"))
        self.assertEqual([e["kind"] for e in self.store.list(limit=100)].count("turn.checked"), 1)

    async def test_unknown_initial_sync_is_visible_and_not_blindly_retried(self):
        with mock.patch("agent_blotter.agents_sdk_hooks.begin_turn",
                        side_effect=TimeoutError("private token")) as begin:
            await self.hooks.on_agent_start(SimpleNamespace(), SimpleNamespace(name="one"))
            await self.hooks.on_agent_start(SimpleNamespace(), SimpleNamespace(name="two"))
        self.assertEqual(begin.call_count, 1)
        self.assertEqual(self.hooks.last_check, None)
        self.assertEqual(self.hooks.failures,
                         [{"kind": "turn.checked", "event_id": None, "effect": "UNKNOWN"}])
        self.assertEqual([e["kind"] for e in self.store.list(limit=100)],
                         ["agent.started", "agent.started"])
        self.assertNotIn("private token", str(self.hooks.failures))

    async def test_failure_does_not_retry_ambiguous_append(self):
        client = mock.Mock(actor="sdk.runner", url=self.client.url)
        client.record.side_effect = RuntimeError("credential secret do not copy")
        hooks = BlotterRunHooks(client, Path(self.temp.name) / "other-cursors",
                               session="run-002", fail_closed=False)
        ctx = SimpleNamespace()
        agent = SimpleNamespace(name="a")
        with mock.patch("agent_blotter.agents_sdk_hooks.begin_turn",
                        return_value={"events": [], "cursor": 0}):
            await hooks.on_agent_start(ctx, agent)
        self.assertEqual(client.record.call_count, 1)
        first_id = client.record.call_args.kwargs["event_id"]
        self.assertTrue(first_id)
        self.assertEqual(hooks.failures[0]["event_id"], first_id)
        self.assertNotIn("secret", str(hooks.failures))

    async def test_fail_closed_option_and_no_dynamic_name_leak(self):
        client = mock.Mock(actor="sdk.runner", url=self.client.url)
        client.record.side_effect = RuntimeError("private password")
        hooks = BlotterRunHooks(client, Path(self.temp.name) / "strict-cursors",
                               session="run-003", fail_closed=True)
        with self.assertRaises(RuntimeError):
            await hooks.on_tool_start(SimpleNamespace(), SimpleNamespace(),
                                      SimpleNamespace(name="sk-abc123"))
        self.assertEqual(client.record.call_args.kwargs["payload"], {})
        self.assertNotIn("sk-abc123", str(client.record.call_args))


if __name__ == "__main__":
    unittest.main()
