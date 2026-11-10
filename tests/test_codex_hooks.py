"""Host-hook behavior with synthetic ledger state; no production configuration."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_blotter import Blotter, BlotterError
from agent_blotter import codex_hooks as hooks


class LocalActor:
    def __init__(self, ledger, actor="codex.test"):
        self.ledger, self.actor = ledger, actor

    def record(self, **kwargs):
        return self.ledger.record(actor=self.actor, **kwargs)


class CodexHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Blotter(Path(self.temp.name) / "synthetic.sqlite3")
        self.client = LocalActor(self.ledger)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, name, **fields):
        return {"hook_event_name": name, "session_id": "private-chat",
                "turn_id": "turn-1", **fields}

    def test_turn_start_instructs_full_acknowledged_read_without_network_or_cursor(self):
        event = self.event("UserPromptSubmit", prompt="private PASSWORD=do-not-copy")
        result = hooks.handle_event(event, None, actor=self.client.actor)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("read_turn_page", context)
        self.assertIn("acknowledge_turn_page", context)
        self.assertIn("has_more", context)
        self.assertIn("page.session", context)
        self.assertIn("current requested session", context)
        self.assertIn("untrusted", context)
        self.assertNotIn("private-chat", context)
        self.assertNotIn("PASSWORD", context)
        self.assertEqual(self.ledger.verify()["count"], 0)
        self.assertFalse(list(Path(self.temp.name).glob("*.json")))

    def test_reader_persists_across_turns_and_separates_chats_and_children(self):
        first = self.event("UserPromptSubmit")
        reader = hooks.reader_scope(first, actor=self.client.actor)
        self.assertEqual(reader, hooks.reader_scope(dict(first, turn_id="turn-2"),
                                                    actor=self.client.actor))
        for changed in ({"session_id": "different-chat"}, {"agent_id": "child-one"},
                        {"agent_id": "child-two"}):
            self.assertNotEqual(reader, hooks.reader_scope(dict(first, **changed),
                                                          actor=self.client.actor))
        self.assertNotEqual(hooks.reader_scope(dict(first, agent_id="child-one"),
                                               actor=self.client.actor),
                            hooks.reader_scope(dict(first, agent_id="child-two"),
                                               actor=self.client.actor))
        self.assertNotEqual(reader, hooks.reader_scope(dict(first, agent_id="root"),
                                                       actor=self.client.actor))

    def test_subagent_start_delivers_child_scoped_instruction_without_enrollment(self):
        event = self.event("SubagentStart", agent_id="child-one", agent_type="architect")
        result = hooks.handle_event(event, None, actor=self.client.actor)
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SubagentStart")
        self.assertIn(hooks.reader_scope(event, actor=self.client.actor),
                      result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_tool_observation_logs_only_allowlisted_metadata_with_exact_receipt(self):
        event = self.event("PostToolUse", tool_use_id="tool-one", tool_name="Bash",
                           tool_input={"command": "echo SECRET"},
                           tool_response={"exit_code": 7, "output": "Bearer SECRET"})
        result = hooks.handle_event(event, self.client, actor=self.client.actor)
        self.assertEqual(result, {})
        record, = self.ledger.list()
        self.assertEqual(record["kind"], "codex.tool.observed")
        self.assertEqual(record["payload"]["tool"], "Bash")
        self.assertEqual(record["payload"]["exit_code"], 7)
        self.assertEqual(record["payload"]["outcome"], "nonzero_exit")
        self.assertNotIn("SECRET", json.dumps(record))
        self.assertNotIn("private-chat", json.dumps(record))
        self.assertEqual(record["evidence"], "OBSERVATION")
        self.assertEqual(record["effect"], "OBSERVED")

    def test_stable_tool_retry_is_idempotent_and_new_turn_has_distinct_id(self):
        event = self.event("PostToolUse", tool_use_id="tool-one", tool_name="apply_patch")
        hooks.handle_event(event, self.client, actor=self.client.actor)
        hooks.handle_event(event, self.client, actor=self.client.actor)
        self.assertEqual(self.ledger.verify()["count"], 1)
        hooks.handle_event(dict(event, turn_id="turn-2"), self.client,
                           actor=self.client.actor)
        self.assertEqual(self.ledger.verify()["count"], 2)

    def test_identical_tool_ids_from_two_children_do_not_collide(self):
        event = self.event("PostToolUse", tool_use_id="tool-one", tool_name="Bash")
        for child in ("child-one", "child-two"):
            hooks.handle_event(dict(event, agent_id=child), self.client,
                               actor=self.client.actor)
        records = self.ledger.list()
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["id"], records[1]["id"])

    def test_untrusted_names_response_text_and_boolean_exit_code_are_omitted(self):
        event = self.event("PostToolUse", tool_use_id="tool-one",
                           tool_name="mcp__private-secret__tool",
                           tool_response={"exit_code": True, "output": "SECRET"})
        hooks.handle_event(event, self.client, actor=self.client.actor)
        record, = self.ledger.list()
        self.assertEqual(record["payload"]["tool"], "MCP")
        self.assertEqual(record["payload"]["outcome"], "returned")
        self.assertNotIn("exit_code", record["payload"])
        self.assertNotIn("secret", json.dumps(record).lower())

    def test_actual_codex_tool_aliases_map_to_fixed_categories(self):
        aliases = {"exec_command": "Bash", "functions.exec_command": "Bash",
                   "write_stdin": "Bash", "functions.write_stdin": "Bash",
                   "functions.exec": "code_mode", "functions.apply_patch": "apply_patch",
                   "apply_patch": "apply_patch"}
        for index, (tool, category) in enumerate(aliases.items()):
            hooks.handle_event(self.event("PostToolUse", tool_use_id=f"tool-{index}",
                                           tool_name=tool, tool_response={"exit_code": 0}),
                               self.client, actor=self.client.actor)
            record = self.ledger.tail(limit=1)[0]
            self.assertEqual(record["payload"]["tool"], category)
            if category == "Bash":
                self.assertEqual(record["payload"]["outcome"], "zero_exit")
            else:
                self.assertEqual(record["payload"]["outcome"], "returned")
                self.assertNotIn("exit_code", record["payload"])

    def test_all_blotter_adapter_calls_skip_without_network_or_recursive_records(self):
        for name in ("mcp__blotter__record_activity", "mcp__blotter__read_turn_page",
                     "mcp__blotter__acknowledge_turn_page", "mcp__blotter__latest20",
                     "mcp__blotter__begin_turn"):
            self.assertEqual(hooks.handle_event(self.event("PostToolUse", tool_name=name,
                                                           tool_use_id="tool-one"),
                                                None, actor=self.client.actor), {})
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_malformed_identity_cannot_append(self):
        cases = [self.event("PostToolUse", tool_name="Bash"),
                 self.event("SubagentStart"),
                 self.event("PostToolUse", session_id="", tool_name="Bash", tool_use_id="t")]
        for event in cases:
            with self.subTest(event=event), self.assertRaises(hooks.HookInputError):
                hooks.handle_event(event, self.client, actor=self.client.actor)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_stop_reports_only_host_stoppage_not_success_or_private_assistant_text(self):
        event = self.event("Stop", last_assistant_message="SECRET", stop_hook_active=False)
        hooks.handle_event(event, self.client, actor=self.client.actor)
        record, = self.ledger.list()
        self.assertEqual(record["kind"], "codex.turn.stopped")
        self.assertNotIn("SECRET", json.dumps(record))
        self.assertNotIn("success", json.dumps(record).lower())

    def test_malformed_or_wrong_actor_append_receipt_is_not_accepted(self):
        event = self.event("PostToolUse", tool_name="Bash", tool_use_id="tool-one")
        for receipt in ({"ok": True}, {"id": "wrong", "seq": 1},
                        {"id": "wrong", "seq": True}):
            client = mock.Mock(actor=self.client.actor)
            client.record.return_value = receipt
            with self.subTest(receipt=receipt), self.assertRaises(hooks.HookReceiptError):
                hooks.handle_event(event, client, actor=self.client.actor)
        def different_actor(**kwargs):
            return {**self.client.record(**kwargs), "actor": "other"}
        client = mock.Mock(actor=self.client.actor)
        client.record.side_effect = different_actor
        with self.assertRaises(hooks.HookReceiptError):
            hooks.handle_event(event, client, actor=self.client.actor)

    def test_receipt_payload_cannot_replace_integer_exit_code_with_boolean(self):
        event = self.event("PostToolUse", tool_name="Bash", tool_use_id="tool-one",
                           tool_response={"exit_code": 0})
        def wrong_type(**kwargs):
            receipt = self.client.record(**kwargs)
            return {**receipt, "payload": {**receipt["payload"], "exit_code": False}}
        client = mock.Mock(actor=self.client.actor)
        client.record.side_effect = wrong_type
        with self.assertRaises(hooks.HookReceiptError):
            hooks.handle_event(event, client, actor=self.client.actor)

    def test_warning_only_on_changed_condition_and_once_on_recovery(self):
        folder = Path(self.temp.name) / "status"
        for code, expected in (("AUTHENTICATION_FAILED", True),
                               ("AUTHENTICATION_FAILED", False),
                               ("CONNECTIVITY_FAILED", True),
                               ("OK", True), ("OK", False)):
            warning = hooks.report_status(folder, actor=self.client.actor, code=code)
            self.assertEqual(bool(warning), expected)
            self.assertNotIn("private-chat", json.dumps(warning))

    def test_recovery_preserves_other_unconfirmed_ids_and_only_reconciles_matching_id(self):
        folder = Path(self.temp.name) / "status"
        one, two, three = ["codex." + char * 48 for char in "abc"]
        hooks.report_status(folder, actor=self.client.actor, code="APPEND_UNCONFIRMED", event_id=one)
        hooks.report_status(folder, actor=self.client.actor, code="APPEND_UNCONFIRMED", event_id=two)
        recovery = hooks.report_status(folder, actor=self.client.actor, code="OK", event_id=three)
        self.assertIn("unconfirmed", recovery["systemMessage"])
        path, = folder.glob("*.json")
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["unconfirmed_event_ids"], [one, two])
        hooks.report_status(folder, actor=self.client.actor, code="OK", event_id=one)
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["unconfirmed_event_ids"], [two])

    def test_unconfirmed_id_retention_is_bounded_and_overflow_stays_explicit(self):
        folder = Path(self.temp.name) / "status"
        identifiers = ["codex." + char * 48 for char in "abc"]
        with mock.patch.object(hooks, "MAX_UNCONFIRMED_IDS", 2):
            for identifier in identifiers:
                hooks.report_status(folder, actor=self.client.actor,
                                    code="APPEND_UNCONFIRMED", event_id=identifier)
        hooks.report_status(folder, actor=self.client.actor, code="OK", event_id=identifiers[-1])
        path, = folder.glob("*.json")
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["unconfirmed_event_ids"], identifiers[:2])
        self.assertEqual(state["unconfirmed_overflow_attempts"], 1)
        self.assertTrue(state["unconfirmed_ids_incomplete"])

    def test_host_client_is_fixed_endpoint_direct_and_private_file_only(self):
        configuration = {"BLOTTER_HOOK_ACTOR": self.client.actor,
                         "BLOTTER_URL": "https://lappy.tail86ea75.ts.net/blotter",
                         "BLOTTER_HOOK_TOKEN_FILE": str(Path(self.temp.name) / "synthetic.token"),
                         "BLOTTER_TOKEN": "ENVIRONMENT_FALLBACK_FORBIDDEN"}
        with mock.patch.dict(os.environ, configuration, clear=True), \
             mock.patch("agent_blotter.codex_hooks._load_token_file", return_value="T" * 40), \
             mock.patch("agent_blotter.codex_hooks.RemoteBlotter") as factory:
            hooks.client_from_host_config(actor=self.client.actor)
        factory.assert_called_once_with(configuration["BLOTTER_URL"],
                                        actor=self.client.actor, token="T" * 40,
                                        timeout=2.0, direct_only=True)
        configuration["BLOTTER_URL"] = "https://attacker.example/blotter"
        with mock.patch.dict(os.environ, configuration, clear=True), \
             mock.patch("agent_blotter.codex_hooks._load_token_file") as read:
            with self.assertRaises(ValueError):
                hooks.client_from_host_config(actor=self.client.actor)
        read.assert_not_called()

    def test_hook_manifest_uses_isolated_installed_module_and_all_start_paths(self):
        root = Path(__file__).resolve().parents[1] / "integrations" / "codex"
        definitions = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(set(definitions["hooks"]),
                         {"UserPromptSubmit", "SubagentStart", "PostToolUse", "Stop"})
        for groups in definitions["hooks"].values():
            command = groups[0]["hooks"][0]
            self.assertIn(" -I -m agent_blotter.codex_hooks", command["command"])
            self.assertIn(" -I -m agent_blotter.codex_hooks", command["commandWindows"])
            self.assertNotIn("PYTHONPATH", command["command"])
            self.assertNotIn("${", command["command"])
            self.assertNotIn("%", command["commandWindows"])


class CodexHookProcessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Blotter(Path(self.temp.name) / "synthetic.sqlite3")
        self.client = LocalActor(self.ledger)
        self.environment = {"BLOTTER_HOOK_ACTOR": self.client.actor,
                            "BLOTTER_URL": "https://lappy.tail86ea75.ts.net/blotter",
                            "BLOTTER_HOOK_STATE_DIR": str(Path(self.temp.name) / "status")}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, event=None, *, raw=None, environment=None, client=None):
        stdout = io.StringIO()
        data = raw if raw is not None else json.dumps(event).encode("utf-8")
        with mock.patch.dict(os.environ, {**self.environment, **(environment or {})}, clear=True), \
             mock.patch("sys.stdin", mock.Mock(buffer=io.BytesIO(data))), \
             contextlib.redirect_stdout(stdout), \
             mock.patch("agent_blotter.codex_hooks.client_from_host_config",
                        return_value=client or self.client) as factory:
            status = hooks.main()
        self.assertEqual(status, 0)
        text = stdout.getvalue()
        return json.loads(text) if text else {}, factory

    def test_start_has_no_credential_access_and_only_emits_context(self):
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t"}
        with mock.patch("agent_blotter.codex_hooks._load_token_file") as read:
            result, factory = self.invoke(event)
        read.assert_not_called()
        factory.assert_not_called()
        self.assertIn("hookSpecificOutput", result)
        self.assertEqual(self.ledger.verify()["count"], 0)

    def test_network_failures_do_not_repeat_or_leak_and_recovery_is_observed(self):
        event = {"hook_event_name": "PostToolUse", "session_id": "s", "turn_id": "t",
                 "tool_name": "Bash", "tool_use_id": "tool-one"}
        broken = mock.Mock(actor=self.client.actor)
        broken.record.side_effect = BlotterError("Authorization: Bearer SECRET")
        first, _ = self.invoke(event, client=broken)
        second, _ = self.invoke(dict(event, tool_use_id="tool-two"), client=broken)
        recovered, _ = self.invoke(dict(event, tool_use_id="tool-three"))
        fourth, _ = self.invoke(dict(event, tool_use_id="tool-four"))
        self.assertIn("APPEND_UNCONFIRMED", first["systemMessage"])
        self.assertEqual(second, {})
        self.assertIn("resumed", recovered["systemMessage"])
        self.assertEqual(fourth, {})
        self.assertNotIn("SECRET", json.dumps([first, second, recovered, fourth]))
        self.assertEqual(self.ledger.verify()["count"], 2)

    def test_bad_input_is_distinct_from_missing_configuration_and_never_connects(self):
        malformed, factory = self.invoke(raw=b'{"hook_event_name":"Stop","hook_event_name":"Stop"}')
        self.assertIn("INPUT_INVALID", malformed["systemMessage"])
        factory.assert_not_called()
        event = {"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"}
        missing, factory = self.invoke(event, environment={"BLOTTER_HOOK_ACTOR": ""})
        self.assertIn("CONFIGURATION_UNAVAILABLE", missing["systemMessage"])
        factory.assert_not_called()

    def test_recursive_tool_does_not_touch_private_configuration_or_diagnostics(self):
        event = {"hook_event_name": "PostToolUse", "tool_name": "mcp__blotter__read_turn_page"}
        result, factory = self.invoke(event, environment={"BLOTTER_URL": ""})
        self.assertEqual(result, {})
        factory.assert_not_called()
        self.assertFalse(list((Path(self.temp.name) / "status").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
