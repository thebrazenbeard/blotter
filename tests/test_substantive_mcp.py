"""New read-only activity view in the credential-bound MCP adapter."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_blotter import Blotter
from agent_blotter.mcp_stdio import BlotterMCP
from agent_blotter.remote import RemoteBlotter
from agent_blotter.server import create_server


class SubstantiveMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Blotter(Path(self.temp.name) / "main.sqlite3")
        self.server = create_server(self.ledger, {"Q" * 40: "observer"}, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        client = RemoteBlotter(f"http://127.0.0.1:{self.server.server_port}",
                               actor="observer", token="Q" * 40)
        self.mcp = BlotterMCP(client, Path(self.temp.name) / "cursors")
        self.mcp.handle({"jsonrpc": "2.0", "id": 1,
                         "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18",
                                    "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"}}})
        self.mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def call(self, name, arguments=None):
        return self.mcp.handle({"jsonrpc": "2.0", "id": 3,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": arguments or {}}})

    def test_noncheck_view_is_additive_not_catchup(self):
        self.ledger.record(actor="worker", kind="source.checked",
                           message="meaningful")
        for i in range(30):
            self.ledger.check(actor="observer", session=f"read-{i}", after_seq=0, limit=1)
        before = self.ledger.verify()
        old = self.call("latest20")
        self.assertEqual(len(json.loads(old["result"]["content"][0]["text"])["events"]), 20)
        newer = self.call("latest_activity20")
        self.assertFalse(newer["result"]["isError"])
        data = json.loads(newer["result"]["content"][0]["text"])
        self.assertEqual([e["kind"] for e in data["events"]], ["source.checked"])
        self.assertEqual(data["last_activity_seq"], 1)
        self.assertEqual(self.ledger.verify(), before)
        self.assertIn("latest_activity20",
                      [x["name"] for x in self.mcp.handle(
                          {"jsonrpc": "2.0", "id": 4, "method": "tools/list",
                           "params": {}})["result"]["tools"]])


if __name__ == "__main__":
    unittest.main()
