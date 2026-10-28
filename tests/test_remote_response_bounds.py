"""Adversarial response-size tests for the authenticated remote client."""
import io
import http.client
import json
import unittest
import urllib.error

from agent_blotter.remote import RemoteBlotter
from agent_blotter.probe import _error_code
from agent_blotter.store import BlotterError


class _Response(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.requested_sizes = []

    def read(self, size=-1):
        self.requested_sizes.append(size)
        return super().read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _Opener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def open(self, req, timeout):
        if self.error is not None:
            raise self.error
        return self.response


class _TruncatedResponse(_Response):
    def read(self, size=-1):
        self.requested_sizes.append(size)
        raise http.client.IncompleteRead(b"partial", 10)


class ResponseBoundsTests(unittest.TestCase):
    def setUp(self):
        self.client = RemoteBlotter("https://private.tailnet", actor="agent.one", token="A" * 40)

    def test_tail_rejects_oversized_response_without_unbounded_read(self):
        content = json.dumps({"events": [{"message": "X" * (2 * 1024 * 1024)}]}).encode()
        response = _Response(content)
        self.client._opener = _Opener(response)
        with self.assertRaisesRegex(BlotterError, "response exceeds safe size"):
            self.client.tail(limit=20)
        self.assertTrue(response.requested_sizes)
        self.assertTrue(all(size >= 0 for size in response.requested_sizes))
        self.assertEqual(self.client.actor, "agent.one")

    def test_record_oversized_response_is_ambiguous_not_rejected_write(self):
        response = _Response(json.dumps({"huge": "X" * 200000}).encode())
        self.client._opener = _Opener(response)
        with self.assertRaisesRegex(BlotterError, "write outcome may be unknown"):
            self.client.record(kind="tool.executed", message="one", event_id="evt-100")
        self.assertTrue(all(size >= 0 for size in response.requested_sizes))

    def test_record_truncated_response_is_ambiguous_not_uncaught(self):
        self.client._opener = _Opener(_TruncatedResponse(b""))
        with self.assertRaisesRegex(BlotterError, "write outcome may be unknown"):
            self.client.record(kind="tool.executed", message="one", event_id="evt-101")

    def test_get_truncated_response_has_no_verified_result(self):
        self.client._opener = _Opener(_TruncatedResponse(b""))
        with self.assertRaisesRegex(BlotterError, "no read result was verified"):
            self.client.health()

    def test_record_deeply_nested_json_is_ambiguous_not_uncaught(self):
        nested_json = b"[" * 10000 + b"0" + b"]" * 10000
        self.client._opener = _Opener(_Response(nested_json))
        with self.assertRaisesRegex(BlotterError, "write outcome may be unknown"):
            self.client.record(kind="tool.executed", message="one", event_id="evt-102")

    def test_health_deeply_nested_json_has_no_verified_result(self):
        nested_json = b"[" * 10000 + b"0" + b"]" * 10000
        self.client._opener = _Opener(_Response(nested_json))
        with self.assertRaisesRegex(BlotterError, "no read result verified"):
            self.client.health()

    def test_valid_small_health_response_accepted(self):
        self.client._opener = _Opener(_Response(b'{"ok":true}'))
        self.assertEqual(self.client.health(), {"ok": True})

    def test_http_error_body_does_not_read_without_limit(self):
        content = b'{"error":"' + b"X" * (2 * 1024 * 1024) + b'"}'
        fp = _Response(content)
        http_error = urllib.error.HTTPError("https://private.tailnet/v1/health", 503,
                                           "unavailable", {}, fp)
        self.client._opener = _Opener(error=http_error)
        with self.assertRaisesRegex(BlotterError, r"server rejected request \(503\)"):
            self.client.health()
        self.assertTrue(fp.requested_sizes)
        self.assertTrue(all(size >= 0 for size in fp.requested_sizes))

    def test_proxy_504_after_record_is_ambiguous(self):
        error = urllib.error.HTTPError("https://private.tailnet/v1/record", 504,
                                      "gateway timeout", {}, _Response(b'{}'))
        self.client._opener = _Opener(error=error)
        with self.assertRaisesRegex(BlotterError, "write outcome may be unknown") as caught:
            self.client.record(kind="tool.executed", message="one", event_id="evt-103")
        self.assertEqual(_error_code(caught.exception, stage="append"),
                         "APPEND_OUTCOME_UNKNOWN")

    def test_auth_failure_after_record_is_definite_rejection(self):
        error = urllib.error.HTTPError("https://private.tailnet/v1/record", 401,
                                      "unauthorized", {}, _Response(b'{"error":"bad token"}'))
        self.client._opener = _Opener(error=error)
        with self.assertRaisesRegex(BlotterError, "server rejected request \\(401\\)"):
            self.client.record(kind="tool.executed", message="one", event_id="evt-104")


if __name__ == "__main__":
    unittest.main()
