import io,json,os,tempfile,threading,unittest
from unittest import mock
from pathlib import Path
from agent_blotter import Blotter, BlotterError
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server
from agent_blotter.turn import begin_turn


class WindowsConsoleOutputTests(unittest.TestCase):
    def test_turn_begin_prints_unicode_history_on_cp1252_stdout(self):
        """The cursor can already be committed when stdout encoding fails."""
        from agent_blotter.turn import main
        expected = {"cursor": 2, "events": [{"seq": 1, "message": "A → B and 漢字"}]}
        raw = io.BytesIO()
        stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with mock.patch("agent_blotter.turn.RemoteBlotter"), \
             mock.patch("agent_blotter.turn.begin_turn", return_value=expected), \
             mock.patch.dict(os.environ, {"BLOTTER_TOKEN": "A" * 40}), \
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

if __name__=="__main__":unittest.main()
