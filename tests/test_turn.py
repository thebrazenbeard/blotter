import tempfile,threading,unittest
from pathlib import Path
from agent_blotter import Blotter, BlotterError
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server
from agent_blotter.turn import begin_turn

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

if __name__=="__main__":unittest.main()
