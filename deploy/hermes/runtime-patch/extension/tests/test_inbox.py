"""Receipt durability, retry identities, and artifact ownership contracts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inbox import Inbox, atomic_json, item_id, owned_file, video_urls, wants_analysis


class Workflow:
    def __init__(self, home):
        self.root = home / "social-video-work"
        self.root.mkdir()
        self.failure_after_first = False

    def directory(self, identifier):
        return self.root / identifier

    def status(self, identifier):
        return json.loads((self.directory(identifier) / "item.json").read_text())

    def submit(self, urls, *, request_id):
        items = []
        for index, url in enumerate(urls):
            identifier = item_id(request_id, index)
            directory = self.directory(identifier)
            path = directory / "item.json"
            if path.exists():
                item = self.status(identifier)
            else:
                directory.mkdir()
                item = {"id": identifier, "request_id": request_id, "original_url": url,
                        "state": "queued", "artifacts": {"result": str(directory / "result.json")}}
                atomic_json(path, item)
            items.append(item)
            if self.failure_after_first:
                raise OSError("simulated interruption")
        return items


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.store = Inbox(self.home)
        self.workflow = Workflow(self.home)
        self.envelope = {"profile": "default", "bot_id": "55", "chat_id": "1", "message_id": "8",
                         "text": "https://v.douyin.com/example/", "urls": ["https://v.douyin.com/example/"]}

    def test_commit_survives_reopen_and_duplicate_delivery(self):
        request = self.store.receive(self.envelope)
        reopened = Inbox(self.home)
        self.assertEqual(reopened.receive(self.envelope), request)
        self.assertEqual(len(reopened.requests()), 1)
        self.assertEqual(reopened.requests()[0]["submitted"], 0)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_identity_distinguishes_bot_and_chat_and_message(self):
        for key in ("bot_id", "chat_id", "message_id"):
            first = self.store.receive(self.envelope)
            other = {**self.envelope, key: "99"}
            self.assertNotEqual(self.store.receive(other), first)

    def test_same_identity_cannot_change_request(self):
        self.store.receive(self.envelope)
        with self.assertRaises(ValueError):
            self.store.receive({**self.envelope, "text": "changed"})

    def test_text_batch_reuses_each_source_message_receipt(self):
        first = {**self.envelope, "user_id": "7"}
        second = {**first, "message_id": "9", "text": "https://youtu.be/example", "urls": ["https://youtu.be/example"]}
        self.store.receive(first)
        self.store.receive(second)
        rows = self.store.lookup_batch("default", "1", "8", first["text"] + "\n" + second["text"], "7")
        self.assertEqual([row["message_id"] for row in rows], ["8", "9"])
        self.assertEqual(self.store.lookup_batch("default", "1", "8", first["text"], "another-user"), [])

    def test_multi_url_partial_submission_replays_same_items(self):
        envelope = {**self.envelope, "urls": [*self.envelope["urls"], "https://youtu.be/example"]}
        request = self.store.receive(envelope)
        self.workflow.failure_after_first = True
        self.assertEqual(self.store.submit_pending(self.workflow), 0)
        self.assertEqual(self.store.requests()[0]["submitted"], 0)
        self.workflow.failure_after_first = False
        reopened = Inbox(self.home)
        self.assertEqual(reopened.submit_pending(self.workflow), 1)
        self.assertEqual(reopened.submit_pending(self.workflow), 0)
        self.assertEqual({row["item_id"] for row in reopened.jobs()}, {item_id(request, 0), item_id(request, 1)})
        self.assertEqual(len(list(self.workflow.root.glob("*/item.json"))), 2)

    def test_ambiguous_or_negative_requests_wait_for_explicit_activation(self):
        request = self.store.receive({**self.envelope, "text": "do not process this video"})
        self.assertEqual(self.store.submit_pending(self.workflow), 0)
        self.store.activate(request)
        self.assertEqual(self.store.submit_pending(self.workflow), 1)

    def test_inventory_excludes_unknown_and_changed_user_files(self):
        self.store.receive(self.envelope)
        self.store.submit_pending(self.workflow)
        row = self.store.jobs()[0]
        directory = self.workflow.directory(row["item_id"])
        atomic_json(directory / "result.json", {"status": "complete"})
        (directory / "user-notes.md").write_text("keep me")
        item = self.workflow.status(row["item_id"])
        item["state"] = "completed"
        atomic_json(directory / "item.json", item)
        self.store.inventory(self.workflow)
        owner = json.loads((directory / ".durable-owned.json").read_text())
        self.assertEqual([entry["path"] for entry in owner["files"]], ["result.json"])

    def test_capture_protection_matches_job_by_url_and_releases(self):
        self.store.receive(self.envelope)
        self.store.submit_pending(self.workflow)
        row = self.store.jobs()[0]
        directory = self.workflow.directory(row["item_id"])
        marker = directory / ".durable-owned.json"
        self.assertEqual(self.store.protect_capture("https://unrelated.example/other", "call-0"), 0)
        self.assertEqual(self.store.protect_capture(row["original_url"], "call-1"), 1)
        self.assertIn("call-1", json.loads(marker.read_text())["capture_preparations"])
        self.assertEqual(self.store.release_capture("call-1"), 1)
        self.assertEqual(json.loads(marker.read_text())["capture_preparations"], {})
        self.assertEqual(self.store.release_capture("call-1"), 0)

    def test_symlinked_artifact_is_not_owned(self):
        directory = self.home / "owned"
        directory.mkdir()
        original = self.home / "outside"
        original.write_text("private")
        link = directory / "linked"
        link.symlink_to(original)
        self.assertIsNone(owned_file(directory, link))

    def test_only_supported_https_urls_are_extracted(self):
        self.assertEqual(video_urls("https://v.douyin.com/a/， https://evil.example/a https://youtu.be/abc"),
                         ["https://v.douyin.com/a/", "https://youtu.be/abc"])
        self.assertEqual(video_urls("https://user:pass@v.douyin.com/a http://v.douyin.com/a"), [])

    def test_analysis_intent_preserves_capture_only_and_negative_requests(self):
        url = self.envelope["urls"][0]
        self.assertTrue(wants_analysis(url, [url]))
        self.assertTrue(wants_analysis("复制打开抖音，看看作品 " + url, [url]))
        self.assertFalse(wants_analysis("download only " + url, [url]))
        self.assertFalse(wants_analysis("don't analyze " + url, [url]))


if __name__ == "__main__":
    unittest.main()
