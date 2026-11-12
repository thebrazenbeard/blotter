"""PR39 + PR42 integration falsifier: both routes on one server."""
import tempfile
import threading
import unittest
from pathlib import Path

from agent_blotter import Blotter
from agent_blotter.channels import ChannelCatalog
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server

class CombinedRoutesTest(unittest.TestCase):
    def test_main_status_and_private_channel_dont_leak_or_break_each_other(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            main = Blotter(root / "main.sqlite3")
            channels = ChannelCatalog(root / "private")
            channels.provision("vera", owner_actor="vera")
            service = create_server(main, {"A"*40:"vera", "B"*40:"other"},
                                    channels=channels, port=0)
            thread = threading.Thread(target=service.serve_forever,
                                      kwargs={"poll_interval":.01}, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{service.server_port}"
                vera = RemoteBlotter(url, actor="vera", token="A"*40)
                other = RemoteBlotter(url, actor="other", token="B"*40)
                private = vera.channel("vera")
                private.record(kind="relational.audit", message="PRIVATE")
                main.record(actor="vera", kind="source.verified", message="SAFE")
                main.check(actor="other", session="check", after_seq=0)
                self.assertEqual([x["message"] for x in vera.tail_substantive()],
                                 ["SAFE"])
                self.assertEqual([x["message"] for x in other.tail_substantive()],
                                 ["SAFE"])
                self.assertEqual([x["kind"] for x in vera.tail()],
                                 ["source.verified", "turn.checked"])
                self.assertEqual([x["message"] for x in private.tail()],
                                 ["PRIVATE"])
                self.assertEqual([x["message"] for x in private.list()],
                                 ["PRIVATE"])
                self.assertEqual(main.verify()["count"], 2)
            finally:
                service.shutdown()
                service.server_close()
                thread.join(timeout=3)

if __name__ == "__main__":
    unittest.main()
