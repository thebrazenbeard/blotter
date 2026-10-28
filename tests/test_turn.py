import contextlib,io,json,os,tempfile,threading,unittest
from pathlib import Path
from unittest import mock
from agent_blotter import Blotter, BlotterError
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server
from agent_blotter.turn import begin_turn, main as turn_main


class WindowsConsoleOutputTests(unittest.TestCase):
    def test_turn_begin_prints_unicode_history_on_cp1252_stdout(self):
        """The cursor can already be committed when stdout encoding fails."""
        from agent_blotter.turn import main
        expected = {"cursor": 2, "events": [{"seq": 1, "message": "A → B and 漢字"}]}
        raw = io.BytesIO()
        stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with mock.patch("agent_blotter.turn.RemoteBlotter"), \
             mock.patch("agent_blotter.turn.begin_turn", return_value=expected), \
             mock.patch.dict(os.environ, {"BLOTTER_TOKEN": "A" * 40,
                                           "BLOTTER_URL": "http://127.0.0.1:8832"}), \
             mock.patch("sys.stdout", stdout):
            result = main(["--actor", "agent.one", "--url", "http://127.0.0.1:8832",
                           "begin", "--session", "turn-1"])
        stdout.flush()
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(raw.getvalue().decode("cp1252")), expected)

class TurnTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Blotter(Path(self.temp.name)/"shared.sqlite3")
        self.server=create_server(self.db,{"A"*40:"agent.one","B"*40:"agent.two"},port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,
                     kwargs={"poll_interval":0.01},daemon=True)
        self.thread.start()
        url=f"http://127.0.0.1:{self.server.server_port}"
        self.one=RemoteBlotter(url,actor="agent.one",token="A"*40)
        self.two=RemoteBlotter(url,actor="agent.two",token="B"*40)
        self.state=Path(self.temp.name)/"cursors"
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()
    def test_two_agents_cursors_and_pagination(self):
        a=begin_turn(self.one,self.state,session="a1")
        self.assertEqual(a["events"],[])
        event=self.one.record(kind="tool.executed",message="actual action")
        self.one.record(kind="test.executed",message="second actual test action")
        b=begin_turn(self.two,self.state,session="b1",limit=2)
        self.assertIn(event["id"],[e["id"] for e in b["events"]])
        self.assertGreater(b["pages"],1)
        again=begin_turn(self.two,self.state,session="b2")
        self.assertEqual(again["events"],[])
        self.assertEqual(again["previous_cursor"],b["cursor"])
        anew=begin_turn(self.one,self.state,session="a2")
        self.assertIn("agent.two",[e["actor"] for e in anew["events"]])
        self.assertTrue(self.db.verify()["ok"])
    def test_different_ledger_cannot_reuse_cursor(self):
        begin_turn(self.one,self.state,session="a")
        wrong=RemoteBlotter("http://127.0.0.1:19083",actor="agent.one",token="A"*40)
        with self.assertRaises(BlotterError):begin_turn(wrong,self.state,session="b")
    def test_safe_reverse_proxy_path(self):
        good=RemoteBlotter("https://example.ts.net/blotter",actor="agent.one",token="A"*40)
        self.assertEqual(good.url,"https://example.ts.net/blotter")
        for url in ("https://example.ts.net/a/../b","https://example.ts.net//evil",
                    "https://example.ts.net/%2e%2e","https://example.ts.net/?x=1",
                    "http://example.ts.net/blotter"):
            with self.subTest(url=url),self.assertRaises(BlotterError):
                RemoteBlotter(url,actor="agent.one",token="A"*40)

    def test_actor_with_path_components_is_confined_to_cursor_directory(self):
        class FakeClient:
            actor = "a/../../outside"
            url = "https://ledger.example"
            def check(self, **kwargs):
                return {"events": [], "next_seq": 1, "has_more": False,
                        "check_event": {"seq": 1}, "next_hash": "a" * 64}
        with self.assertRaisesRegex(BlotterError, "ambiguous legacy actor path"):
            begin_turn(FakeClient(), self.state, session="safe")
        result = begin_turn(FakeClient(), self.state, session="safe",
                            re_enroll_legacy=True)
        self.assertEqual(result["actor"], FakeClient.actor)
        self.assertFalse((self.state.parent / "outside.json").exists())
        self.assertEqual(len(list(self.state.glob("*.json"))), 1)

    def test_same_url_replaced_ledger_rejects_old_cursor(self):
        class Switch:
            def __init__(self, current):
                self.current = current
            def check(self, **kwargs):
                return self.current.check(**kwargs)
        old = Blotter(Path(self.temp.name) / "old.sqlite3")
        replacement = Blotter(Path(self.temp.name) / "replacement.sqlite3")
        switch = Switch(old)
        server = create_server(switch, {"A" * 40: "agent.one"}, port=0)
        worker = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            client = RemoteBlotter(f"http://127.0.0.1:{server.server_port}",
                                   actor="agent.one", token="A" * 40)
            state = self.state / "cutover"
            first = begin_turn(client, state, session="before")
            self.assertEqual(first["cursor"], 1)
            for i in range(3):
                replacement.record(actor="agent.other", kind="test.executed",
                                   message=f"Replacement {i}")
            switch.current = replacement
            with self.assertRaisesRegex(BlotterError, "history mismatch"):
                begin_turn(client, state, session="after")
            self.assertEqual(first["cursor"], 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_legacy_cursor_without_checkpoint_requires_reenrollment(self):
        begin_turn(self.one, self.state, session="first")
        saved_path = next(self.state.glob("*.json"))
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["cursor_hash"]), 64)
        saved.pop("cursor_hash")
        saved_path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(BlotterError, "cursor belongs"):
            begin_turn(self.one, self.state, session="second")

    def test_prehashed_legacy_cursor_requires_explicit_reenrollment(self):
        self.state.mkdir(parents=True, exist_ok=True)
        old = self.state / "agent.one.json"
        old.write_text(json.dumps({"url": self.one.url, "cursor": 18}),
                       encoding="utf-8")
        with self.assertRaisesRegex(BlotterError, "legacy cursor detected"):
            begin_turn(self.one, self.state, session="legacy")
        self.assertEqual(self.db.verify()["count"], 0)
        self.assertEqual(len(list(self.state.glob("actor-*.json"))), 0)

    def test_nested_legacy_cursor_is_detected_without_path_escape(self):
        class NestedClient:
            actor = "team/alpha"
            url = "https://ledger.example"
            def check(self, **kwargs):
                raise AssertionError("must reject legacy cursor before contacting server")
        old = self.state / "team" / "alpha.json"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text(json.dumps({"url": NestedClient.url, "cursor": 41}),
                       encoding="utf-8")
        with self.assertRaisesRegex(BlotterError, "legacy cursor detected"):
            begin_turn(NestedClient(), self.state, session="legacy")
        self.assertEqual(len(list(self.state.glob("actor-*.json"))), 0)

    def test_at_sign_legacy_actor_is_detected(self):
        class AtClient:
            actor = "agent@one"
            url = "https://ledger.example"
            def check(self, **kwargs):
                raise AssertionError("must reject before server check")
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "agent@one.json").write_text(
            json.dumps({"url": AtClient.url, "cursor": 4}), encoding="utf-8")
        with self.assertRaisesRegex(BlotterError, "legacy cursor detected"):
            begin_turn(AtClient(), self.state, session="legacy")

    def test_punctuation_prefixed_nested_legacy_cursors_are_detected(self):
        for actor in ("team/.hidden", "team/_node", "team/-node", "team/@node"):
            with self.subTest(actor=actor):
                class LegacyClient:
                    url = "https://ledger.example"
                    def check(self, **kwargs):
                        raise AssertionError("must not silently replay")
                client = LegacyClient()
                client.actor = actor
                legacy = self.state / "team" / (actor.split("/")[-1] + ".json")
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.write_text('{"cursor":5}', encoding="utf-8")
                with self.assertRaisesRegex(BlotterError, "legacy cursor detected"):
                    begin_turn(client, self.state, session="legacy")

    def test_slash_ambiguity_requires_explicit_legacy_reenrollment(self):
        class Fake:
            url = "https://ledger.example"
            def check(self, **kwargs):
                return {"events": [], "next_seq": 1, "has_more": False,
                        "check_event": {"seq": 1}, "next_hash": "b" * 64}
        for actor in ("a//b", "a/", "a/./b", "a/../b"):
            with self.subTest(actor=actor):
                client = Fake()
                client.actor = actor
                with self.assertRaisesRegex(BlotterError, "ambiguous legacy actor path"):
                    begin_turn(client, self.state, session="initial")
                result = begin_turn(client, self.state, session="explicit",
                                    re_enroll_legacy=True)
                self.assertEqual(result["actor"], actor)

class TurnCliCredentialTests(unittest.TestCase):
    """Mock the remote transport: NEVER call /v1/check on a live server."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.temp.name) / "agent.token"
        self.token_file.write_text("T" * 40, encoding="utf-8")
        if os.name == "posix":
            self.token_file.chmod(0o600)
        self.url = "https://lappy.tail86ea75.ts.net/blotter"

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        values = {"BLOTTER_URL": self.url, "BLOTTER_TOKEN": "", **(environ or {})}
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                mock.patch.dict(os.environ, values), \
                mock.patch("agent_blotter.turn.RemoteBlotter") as factory:
            factory.return_value.record.return_value = {"ok": True}
            result = turn_main(list(args))
        return result, stdout.getvalue(), stderr.getvalue(), factory

    def record_args(self):
        return ("--actor", "agent.vn2", "--token-file", str(self.token_file),
                "record", "--session", "testing", "--kind", "test.executed",
                "--message", "local mocked test")

    @unittest.skipUnless(os.name == "posix", "POSIX private token file")
    def test_safe_file_uses_direct_transport_only(self):
        rc, out, err, factory = self.invoke(*self.record_args())
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"ok": True})
        factory.assert_called_once_with(
            self.url, actor="agent.vn2", token="T" * 40, direct_only=True)

    @unittest.skipUnless(os.name == "posix", "POSIX private token file")
    def test_group_world_readable_file_rejected_without_network(self):
        self.token_file.chmod(0o644)
        rc, out, err, factory = self.invoke(*self.record_args())
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("private token file unavailable or unsafe", err)
        self.assertNotIn(str(self.token_file), err)
        factory.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX private token file")
    def test_symlinked_file_rejected_without_network(self):
        alias = Path(self.temp.name) / "alias.token"
        alias.symlink_to(self.token_file)
        args = list(self.record_args())
        args[3] = str(alias)
        rc, _out, err, factory = self.invoke(*args)
        self.assertEqual(rc, 2)
        self.assertIn("private token file unavailable or unsafe", err)
        factory.assert_not_called()

    def test_remote_url_substitution_rejected_before_token_read(self):
        args = ["--actor", "agent.vn2", "--url", "https://untrusted.example/blotter",
                "--token-file", str(self.token_file), "record", "--session", "testing",
                "--kind", "test.executed", "--message", "not executed"]
        with mock.patch("agent_blotter.turn._read_private_token") as read_token:
            rc, _out, err, factory = self.invoke(*args)
        self.assertEqual(rc, 2)
        self.assertIn("does not match operator configuration", err)
        self.assertNotIn("untrusted.example", err)
        read_token.assert_not_called()
        factory.assert_not_called()

    def test_windows_token_file_is_denied_without_verified_acl(self):
        from agent_blotter.probe import _read_private_token
        with mock.patch("agent_blotter.probe.os.name", "nt"):
            with self.assertRaises(PermissionError):
                _read_private_token(self.token_file)

    def test_loopback_url_substitution_rejected_when_pinned(self):
        args = ["--actor", "agent.vn2", "--url", "http://127.0.0.1:12345",
                "record", "--session", "testing", "--kind", "test.executed",
                "--message", "not executed"]
        rc, _out, err, factory = self.invoke(*args, environ={"BLOTTER_TOKEN": "T" * 40})
        self.assertEqual(rc, 2)
        self.assertIn("does not match operator configuration", err)
        factory.assert_not_called()

    def test_unpinned_remote_url_rejected(self):
        args = ["--actor", "agent.vn2", "--url", self.url,
                "record", "--session", "testing", "--kind", "test.executed",
                "--message", "not executed"]
        rc, _out, err, factory = self.invoke(*args, environ={"BLOTTER_URL": "",
                                                           "BLOTTER_TOKEN": "T" * 40})
        self.assertEqual(rc, 2)
        self.assertIn("does not match operator configuration", err)
        factory.assert_not_called()

    def test_unpinned_loopback_url_rejected(self):
        args = ["--actor", "agent.vn1", "--url", "http://127.0.0.1:19083",
                "record", "--session", "testing", "--kind", "test.executed",
                "--message", "not executed"]
        rc, _out, err, factory = self.invoke(*args, environ={"BLOTTER_URL": "",
                                                           "BLOTTER_TOKEN": "T" * 40})
        self.assertEqual(rc, 2)
        self.assertIn("does not match operator configuration", err)
        factory.assert_not_called()

    def test_process_scoped_token_supports_windows_without_token_file(self):
        args = ["--actor", "agent.vn1", "record", "--session", "testing",
                "--kind", "test.executed", "--message", "not sent"]
        rc, _out, err, factory = self.invoke(*args, environ={"BLOTTER_TOKEN": "T" * 40})
        self.assertEqual(rc, 0, err)
        factory.assert_called_once_with(
            self.url, actor="agent.vn1", token="T" * 40, direct_only=True)

    def test_control_character_token_rejected(self):
        args = ["--actor", "agent.vn1", "record", "--session", "testing",
                "--kind", "test.executed", "--message", "not sent"]
        rc, _out, err, factory = self.invoke(*args, environ={"BLOTTER_TOKEN": "T" * 40 + "\r"})
        self.assertEqual(rc, 2)
        self.assertIn("invalid private token configuration", err)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
