"""Capture service replacement preserves the durable work item and safe retry boundaries."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner import CaptureResumeBackend


class CaptureResumeTests(unittest.TestCase):
    def invoke(self, state, *, attempts=0):
        item = {"id": "stable-work-item", "capture_job_id": "old-capture", "restart_recaptures": attempts}
        received = []
        backend = SimpleNamespace(capture=lambda current, save: received.append(dict(current)) or {"ok": True})
        wrapper = CaptureResumeBackend(backend, SimpleNamespace(get=lambda identifier: state))
        result = wrapper.capture(item, lambda **fields: item.update(fields))
        return item, received, result

    def test_restart_reuses_work_item_with_new_capture_identity(self):
        item, received, result = self.invoke({"status": "failed", "error": "capture interrupted by service restart"})
        self.assertEqual(item["id"], "stable-work-item")
        self.assertIsNone(item["capture_job_id"])
        self.assertEqual(item["previous_capture_job_ids"], ["old-capture"])
        self.assertEqual(item["restart_recaptures"], 1)
        self.assertTrue(result["ok"])

    def test_expired_unretained_capture_can_be_reacquired(self):
        item, _, _ = self.invoke({"status": "expired"})
        self.assertIsNone(item["capture_job_id"])

    def test_access_failures_and_cancellations_are_not_reset(self):
        for state in ({"status": "failed", "error": "captcha"}, {"status": "cancelled"}, {"status": "complete"}):
            with self.subTest(state=state):
                item, _, _ = self.invoke(state)
                self.assertEqual(item["capture_job_id"], "old-capture")
                self.assertEqual(item["restart_recaptures"], 0)

    def test_repeated_restart_limit_stops_without_another_capture(self):
        with self.assertRaisesRegex(RuntimeError, "limit"):
            self.invoke({"status": "failed", "error": "capture interrupted by service restart"}, attempts=3)

    def test_restart_during_capture_queues_reconnect(self):
        states = iter([{"status": "running"}, {"status": "failed", "error": "capture interrupted by service restart"}])
        def fail(*args):
            raise RuntimeError("capture failed")
        wrapper = CaptureResumeBackend(SimpleNamespace(capture=fail), SimpleNamespace(get=lambda identifier: next(states)))
        with self.assertRaises(TimeoutError):
            wrapper.capture({"capture_job_id": "old-capture"}, lambda **fields: None)


if __name__ == "__main__":
    unittest.main()
