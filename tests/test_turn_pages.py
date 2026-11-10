"""Durable bounded delivery of real shared history before cursor acknowledgment."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from agent_blotter import Blotter, BlotterError
from agent_blotter.store import GENESIS
from agent_blotter.turn import begin_turn
from agent_blotter.turn_pages import acknowledge_turn_page, read_turn_page


class LocalClient:
    """Use the actual store/check contract without sockets or live credentials."""
    url = "https://synthetic-ledger.invalid/blotter"

    def __init__(self, store, actor="agent.one"):
        self.store, self.actor = store, actor

    def check(self, **kwargs):
        return self.store.check(actor=self.actor, **kwargs)


class TurnPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Blotter(self.root / "synthetic.sqlite3")
        self.client = LocalClient(self.store)
        self.state = self.root / "reader-state"

    def seed(self, count, message="actual synthetic activity"):
        return [self.store.record(actor="agent.other", kind="test.executed",
                                  message=message, event_id=f"activity-{i}")
                for i in range(count)]

    def saved(self, state=None):
        return json.loads(next((state or self.state).glob("actor-*.json"))
                          .read_text(encoding="utf-8"))

    def test_large_history_is_delivered_incrementally_without_omissions(self):
        originals = self.seed(6005, message="history " + "x" * 256)
        observed, pages = [], 0
        while True:
            page = read_turn_page(self.client, self.state, session="large-turn", limit=100)
            pages += 1
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=True).encode("utf-8")),
                                 1_750_000)
            self.assertEqual(self.saved()["cursor"], page["previous_cursor"])
            observed.extend(item["id"] for item in page["events"]
                            if item["kind"] != "turn.checked")
            ack = acknowledge_turn_page(self.client, self.state, receipt=page["receipt"])
            self.assertEqual(ack["cursor"], page["cursor"])
            self.assertEqual(ack["cursor_hash"], page["cursor_hash"])
            if not page["has_more"]:
                break
            self.assertLess(pages, 100)
        self.assertGreater(pages, 40)
        self.assertEqual(observed, [event["id"] for event in originals])
        self.assertTrue(self.store.verify()["ok"])

    def test_unacknowledged_page_replays_exactly_after_new_session_and_restart(self):
        self.seed(3)
        page = read_turn_page(self.client, self.state, session="lost-response", limit=2)
        count = self.store.verify()["count"]
        restarted = LocalClient(Blotter(self.store.path))
        again = read_turn_page(restarted, self.state, session="restarted-turn", limit=20)
        self.assertEqual(again, page)
        self.assertEqual(self.store.verify()["count"], count)
        self.assertEqual(self.saved()["cursor"], 0)
        self.assertEqual(self.saved()["cursor_hash"], GENESIS)

    def test_wrong_and_stale_receipts_cannot_advance_cursor(self):
        self.seed(5)
        first = read_turn_page(self.client, self.state, session="one", limit=2)
        with self.assertRaises(BlotterError):
            acknowledge_turn_page(self.client, self.state, receipt="f" * 64)
        self.assertEqual(self.saved()["cursor"], 0)
        ack = acknowledge_turn_page(self.client, self.state, receipt=first["receipt"])
        self.assertEqual(acknowledge_turn_page(self.client, self.state,
                                            receipt=first["receipt"]), ack)
        second = read_turn_page(self.client, self.state, session="two", limit=2)
        # Retrying an earlier successful acknowledgment must preserve newer delivery.
        self.assertEqual(acknowledge_turn_page(self.client, self.state,
                                            receipt=first["receipt"]), ack)
        self.assertEqual(read_turn_page(self.client, self.state, session="retry"), second)
        acknowledge_turn_page(self.client, self.state, receipt=second["receipt"])
        with self.assertRaises(BlotterError):
            acknowledge_turn_page(self.client, self.state, receipt=first["receipt"])

    def test_interrupted_pending_save_does_not_commit_or_skip_history(self):
        originals = self.seed(3)
        with mock.patch("agent_blotter.turn.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                read_turn_page(self.client, self.state, session="interrupted", limit=2)
        self.assertFalse(list(self.state.glob("actor-*.json")))
        page = read_turn_page(self.client, self.state, session="recovered", limit=2)
        self.assertEqual([e["id"] for e in page["events"]], [e["id"] for e in originals[:2]])
        self.assertEqual(page["previous_cursor"], 0)

    def test_interrupted_acknowledgment_replays_the_same_pending_page(self):
        self.seed(3)
        page = read_turn_page(self.client, self.state, session="before", limit=2)
        with mock.patch("agent_blotter.turn.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                acknowledge_turn_page(self.client, self.state, receipt=page["receipt"])
        self.assertEqual(self.saved()["cursor"], 0)
        self.assertEqual(read_turn_page(self.client, self.state, session="restart"), page)
        self.assertEqual(acknowledge_turn_page(self.client, self.state,
                                             receipt=page["receipt"])["cursor"], page["cursor"])

    def test_byte_budget_shrinks_delivery_without_dropping_events(self):
        originals = self.seed(6, message="x" * 1000)
        page = read_turn_page(self.client, self.state, session="small-budget",
                              limit=20, max_bytes=4200)
        self.assertEqual(len(page["events"]), 2)
        self.assertTrue(page["has_more"])
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=True).encode("utf-8")), 4200)
        self.assertEqual([event["id"] for event in page["events"]],
                         [event["id"] for event in originals[:len(page["events"])]])
        self.assertEqual(self.saved()["cursor"], 0)

    def test_single_record_pages_are_rejected_before_adding_check_markers(self):
        self.seed(3)
        before = self.store.verify()["count"]
        with self.assertRaisesRegex(BlotterError, "2.*100"):
            read_turn_page(self.client, self.state, session="would-chase-own-markers", limit=1)
        self.assertEqual(self.store.verify()["count"], before)
        self.assertFalse(list(self.state.glob("actor-*.json")))

    def test_two_record_pages_eventually_exhaust_their_own_check_markers(self):
        originals = self.seed(6)
        observed = []
        for _ in range(10):
            page = read_turn_page(self.client, self.state, session="minimum-progress", limit=2)
            observed.extend(event["id"] for event in page["events"]
                            if event["kind"] != "turn.checked")
            acknowledge_turn_page(self.client, self.state, receipt=page["receipt"])
            if not page["has_more"]:
                break
        else:
            self.fail("minimum page size chased its own markers without catching up")
        self.assertEqual(observed, [event["id"] for event in originals])

    def test_byte_shrink_cannot_reduce_below_two_and_chase_its_own_markers(self):
        self.seed(3, message="x" * 1000)
        with self.assertRaisesRegex(BlotterError, "byte|size"):
            read_turn_page(self.client, self.state, session="minimum-too-large",
                           limit=20, max_bytes=3000)
        self.assertFalse(list(self.state.glob("actor-*.json")))

    def test_corrupt_json_cursor_is_rejected_without_network_or_private_output(self):
        page = read_turn_page(self.client, self.state, session="saved")
        path = next(self.state.glob("actor-*.json"))
        valid = path.read_text(encoding="utf-8")
        corruptions = [
            '{"nested":' * 2000 + '"private-record-body"' + '}' * 2000,
            valid.replace('"cursor":0', '"cursor":0,"cursor":0', 1),
            valid[:-1] + ',"unsupported":NaN}',
            valid[:-1] + ',"unsupported":1e999}',
        ]
        for corruption in corruptions:
            with self.subTest(corruption=corruption[:20]):
                path.write_text(corruption, encoding="utf-8")
                with mock.patch.object(self.client, "check") as check:
                    with self.assertRaises(BlotterError) as caught:
                        read_turn_page(self.client, self.state, session="retry")
                    self.assertNotIn("private-record-body", str(caught.exception))
                    check.assert_not_called()
                self.assertEqual(path.read_text(encoding="utf-8"), corruption)
        path.write_text(valid, encoding="utf-8")
        self.assertEqual(read_turn_page(self.client, self.state, session="recovered"), page)

    def test_cursor_parser_recursion_failure_is_generic_and_keeps_pending_state(self):
        page = read_turn_page(self.client, self.state, session="saved")
        path = next(self.state.glob("actor-*.json"))
        before = path.read_bytes()
        with mock.patch("agent_blotter.turn._decode_response",
                        side_effect=RecursionError("private-record-body")), \
                mock.patch.object(self.client, "check") as check:
            with self.assertRaises(BlotterError) as caught:
                read_turn_page(self.client, self.state, session="retry")
            self.assertNotIn("private-record-body", str(caught.exception))
            check.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(read_turn_page(self.client, self.state, session="recovered"), page)

    def test_decoder_accepted_deep_state_is_rejected_before_remote_check(self):
        page = read_turn_page(self.client, self.state, session="saved")
        acknowledge_turn_page(self.client, self.state, receipt=page["receipt"])
        path = next(self.state.glob("actor-*.json"))
        before = path.read_bytes()
        count = self.store.verify()["count"]
        accepted = json.loads(before)
        nested = "private-record-body"
        for _ in range(1500):
            nested = [nested]
        accepted["unknown_future_field"] = nested
        # Python versions can decode deeper JSON than their persistence encoder
        # accepts. Exercise that actual object rather than a decoder exception.
        with mock.patch("agent_blotter.turn._decode_response", return_value=accepted), \
                mock.patch.object(self.client, "check", wraps=self.client.check) as check:
            with self.assertRaises(BlotterError) as caught:
                read_turn_page(self.client, self.state, session="retry")
            self.assertNotIn("private-record-body", str(caught.exception))
            check.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.verify()["count"], count)

    def test_pending_page_rejects_boolean_cursor_even_when_equal_to_zero(self):
        self.seed(3)
        page = read_turn_page(self.client, self.state, session="before", limit=2)
        saved = self.saved()
        saved["pending_page"]["previous_cursor"] = False
        next(self.state.glob("actor-*.json")).write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(BlotterError):
            acknowledge_turn_page(self.client, self.state, receipt=page["receipt"])

    def test_cursor_hash_tamper_during_pending_delivery_is_rejected(self):
        self.seed(3)
        page = read_turn_page(self.client, self.state, session="before", limit=2)
        path = next(self.state.glob("actor-*.json"))
        saved = self.saved()
        saved["cursor_hash"] = "f" * 64
        path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(BlotterError):
            acknowledge_turn_page(self.client, self.state, receipt=page["receipt"])

    def test_same_url_replacement_rejects_checkpoint_on_next_page(self):
        self.seed(3)
        first = read_turn_page(self.client, self.state, session="before", limit=2)
        acknowledge_turn_page(self.client, self.state, receipt=first["receipt"])
        replacement = Blotter(self.root / "replacement.sqlite3")
        for i in range(5):
            replacement.record(actor="agent.replacement", kind="test.executed", message=str(i))
        self.client.store = replacement
        with self.assertRaisesRegex(BlotterError, "history mismatch"):
            read_turn_page(self.client, self.state, session="after", limit=2)
        self.assertEqual(self.saved()["cursor"], first["cursor"])

    def test_url_retarget_cannot_replay_or_acknowledge_pending_page(self):
        page = read_turn_page(self.client, self.state, session="original")
        self.client.url = "https://different.invalid/blotter"
        for action in (lambda: read_turn_page(self.client, self.state, session="other"),
                       lambda: acknowledge_turn_page(self.client, self.state,
                                                      receipt=page["receipt"])):
            with self.assertRaises(BlotterError):
                action()

    def test_legacy_cursor_is_never_silently_reset(self):
        self.state.mkdir()
        (self.state / "agent.one.json").write_text('{"cursor":14}', encoding="utf-8")
        with self.assertRaisesRegex(BlotterError, "legacy cursor"):
            read_turn_page(self.client, self.state, session="legacy")
        self.assertEqual(self.store.verify()["count"], 0)

    def test_begin_turn_cannot_skip_a_pending_page(self):
        self.seed(2)
        page = read_turn_page(self.client, self.state, session="paged")
        with self.assertRaisesRegex(BlotterError, "pending"):
            begin_turn(self.client, self.state, session="old-client")
        self.assertEqual(self.saved()["cursor"], 0)
        acknowledge_turn_page(self.client, self.state, receipt=page["receipt"])
        result = begin_turn(self.client, self.state, session="old-client")
        self.assertEqual(result["previous_cursor"], page["cursor"])

    def test_same_actor_lock_serializes_begin_with_pending_save(self):
        entered, release = threading.Event(), threading.Event()
        original_check = self.client.check

        def held_check(**kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original_check(**kwargs)

        results = {}

        def read_page():
            try:
                results["page"] = read_turn_page(self.client, self.state, session="held")
            except BaseException as exc:
                results["read_error"] = exc

        def old_begin():
            try:
                begin_turn(self.client, self.state, session="contender")
                results["begin_succeeded"] = True
            except BaseException as exc:
                results["begin_error"] = exc

        with mock.patch.object(self.client, "check", side_effect=held_check):
            reader = threading.Thread(target=read_page)
            reader.start()
            self.assertTrue(entered.wait(5))
            contender = threading.Thread(target=old_begin)
            contender.start()
            release.set()
            reader.join(5)
            contender.join(5)
        self.assertFalse(reader.is_alive() or contender.is_alive())
        self.assertNotIn("read_error", results)
        self.assertNotIn("begin_succeeded", results)
        self.assertIsInstance(results.get("begin_error"), BlotterError)
        self.assertIn("pending", str(results["begin_error"]))

    def test_actor_and_reader_cursors_are_independent(self):
        originals = self.seed(3)
        one = read_turn_page(self.client, self.state, session="one", limit=2)
        acknowledge_turn_page(self.client, self.state, receipt=one["receipt"])
        same_actor_other_reader = read_turn_page(self.client, self.root / "other-reader",
                                                 session="other-reader", limit=2)
        other_actor = read_turn_page(LocalClient(self.store, actor="agent.two"),
                                     self.state, session="other-actor", limit=2)
        expected = [event["id"] for event in originals[:2]]
        self.assertEqual([event["id"] for event in same_actor_other_reader["events"]], expected)
        self.assertEqual([event["id"] for event in other_actor["events"]], expected)

    def test_oversized_single_event_fails_without_pending_or_cursor_progress(self):
        self.seed(1, message="x" * 8192)
        with self.assertRaisesRegex(BlotterError, "byte|size"):
            read_turn_page(self.client, self.state, session="too-large", limit=2, max_bytes=2000)
        self.assertFalse(list(self.state.glob("actor-*.json")))

    def test_malformed_remote_pages_never_persist_a_receipt(self):
        self.seed(3)
        original_check = self.client.check
        for corruption in ("sequence", "hash", "head", "marker", "marker_payload_type", "has_more"):
            with self.subTest(corruption=corruption):
                state = self.root / corruption

                def corrupt(**kwargs):
                    result = copy.deepcopy(original_check(**kwargs))
                    if corruption == "sequence":
                        result["events"][0]["seq"] += 1
                    elif corruption == "hash":
                        result["next_hash"] = "f" * 64
                    elif corruption == "head":
                        result["visible_head_seq"] = 0
                    elif corruption == "marker":
                        result["check_event"]["actor"] = "agent.wrong"
                    elif corruption == "marker_payload_type":
                        result["check_event"]["payload"]["from_seq"] = False
                    else:
                        result["has_more"] = "yes"
                    return result

                with mock.patch.object(self.client, "check", side_effect=corrupt):
                    with self.assertRaises(BlotterError):
                        read_turn_page(self.client, state, session="malformed", limit=2)
                self.assertFalse(list(state.glob("actor-*.json")))


if __name__ == "__main__":
    unittest.main()
