"""Retention behavior tests with temporary home/vault fixtures only."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retention import prune_owned

NOW = 1_800_000_000.0

class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home, self.vault = self.root / "home", self.root / "vault"
        self.identifier = "a" * 32
        self.job = self.home / "social-video-work" / self.identifier
        self.job.mkdir(parents=True)
        for relative in ("Inbox", "Notes", "Sources", "Archive/Inbox", "Archive/Sources"):
            (self.vault / relative).mkdir(parents=True)
        self.write_schema()
        self.record = {"item_id": self.identifier, "request_id": "telegram:123:4", "original_url": "https://example.org/video"}
        self.item = dict(self.record, state="completed", finished_at=NOW - 1, canonical_url="https://example.org/canonical")
        self.media = self.job / "video.mp4"
        self.media.write_bytes(b"video content")
        info = self.media.stat()
        self.owner = {"item_id": self.identifier, "request_id": self.record["request_id"], "files": [
            {"path": "video.mp4", "size": info.st_size, "mtime_ns": info.st_mtime_ns, "inode": info.st_ino}]}
        self.write_item()
        self.write_owner()

    def write_item(self):
        (self.job / "item.json").write_text(json.dumps(self.item))

    def write_owner(self):
        (self.job / ".durable-owned.json").write_text(json.dumps(self.owner))

    def write_schema(self, version=4):
        (self.vault / ".brain-schema.json").write_text(json.dumps({"schema_version": version}))

    def note(self, folder, *, status, name="note.md", url=None, updated=NOW + 10, sources=("source",)):
        url = url or self.item["canonical_url"]
        listed = ", ".join(f'"[[{stem}]]"' for stem in sources)
        (self.vault / folder / name).write_text(
            f"---\nid: note-aaaaaaaaaaaa\nstatus: {status}\ntitle: Video note\n"
            f"updated: {datetime.fromtimestamp(updated, timezone.utc).isoformat()}\n"
            f"sources: [{listed}]\nsource_url: {url}\n---\n\n# Video note\n")

    def source(self, folder="Sources"):
        (self.vault / folder / "source.md").write_text("---\nid: source-aaaaaaaaaaaa\n---\n\n# Source\n")

    def save(self):
        self.note("Notes", status="approved")
        self.source()

    def inbox(self, **kwargs):
        self.note("Inbox", status="inbox", **kwargs)

    def discard(self):
        self.note("Archive/Inbox", status="inbox")

    def prune(self, now=NOW, **kwargs):
        return prune_owned(self.home, self.vault, [self.record], now=now, **kwargs)

    def assert_kept(self, result):
        self.assertTrue(self.media.exists(), result)
        self.assertEqual(result["deleted_bytes"], 0, result)

    LATE = NOW - 1 + 8 * 86400

    def test_inbox_note_keeps_even_expired_job(self):
        self.inbox()
        self.assert_kept(self.prune(now=self.LATE))

    def test_inbox_note_matched_by_post_id_keeps(self):
        self.item["canonical_url"] = "https://www.tiktok.com/@a/video/7123456789012345678"
        self.write_item()
        self.inbox(url="https://m.tiktok.com/v/7123456789012345678.html")
        self.assert_kept(self.prune(now=self.LATE))

    def test_human_approved_but_unpromoted_inbox_note_keeps(self):
        self.note("Inbox", status="approved")
        self.assert_kept(self.prune())

    def test_approved_note_prunes_and_keeps_vault_and_metadata(self):
        self.save()
        vault_before = {str(p.relative_to(self.vault)): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        result = self.prune()
        self.assertFalse(self.media.exists(), result)
        self.assertEqual(result["jobs"][0]["reason"], "capture_decided_verified")
        self.assertEqual(result["deleted_bytes"], len(b"video content"))
        for name in ("item.json", ".durable-owned.json", ".durable-pruned.json"):
            self.assertTrue((self.job / name).is_file())
        self.assertEqual(vault_before, {str(p.relative_to(self.vault)): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()})

    def test_discarded_note_prunes_immediately(self):
        self.discard()
        result = self.prune()
        self.assertFalse(self.media.exists(), result)

    def test_approved_note_with_archived_source_prunes(self):
        self.note("Notes", status="approved")
        self.source("Archive/Sources")
        result = self.prune()
        self.assertFalse(self.media.exists(), result)

    def test_approved_note_with_missing_source_keeps(self):
        self.note("Notes", status="approved")
        self.assert_kept(self.prune())

    def test_approved_note_without_sources_keeps(self):
        self.note("Notes", status="approved", sources=())
        self.source()
        self.assert_kept(self.prune())

    def test_note_with_wrong_status_in_notes_keeps(self):
        self.note("Notes", status="inbox")
        self.source()
        self.assert_kept(self.prune())

    def test_new_inbox_note_protects_after_earlier_discard(self):
        self.discard()
        self.inbox(name="again.md")
        self.assert_kept(self.prune())

    def test_note_older_than_job_cannot_close_new_job(self):
        self.note("Notes", status="approved", updated=NOW - 100)
        self.source()
        self.assert_kept(self.prune(now=NOW + 3600))
        result = self.prune(now=self.LATE)
        self.assertFalse(self.media.exists(), result)

    def test_unrelated_note_is_ignored(self):
        self.inbox(url="https://example.org/other")
        result = self.prune(now=self.LATE)
        self.assertFalse(self.media.exists(), result)

    def test_seven_day_threshold(self):
        self.assert_kept(self.prune(now=NOW - 1 + 7 * 86400 - 1))
        result = self.prune(now=NOW - 1 + 7 * 86400)
        self.assertFalse(self.media.exists(), result)

    def test_capture_in_flight_protects_expired_artifacts(self):
        self.owner["capture_preparations"] = {"call-1": {"at": NOW}}
        self.write_owner()
        self.assert_kept(self.prune(now=NOW + 8 * 86400))

    def test_inline_review_keeps_until_user_decides(self):
        marker = self.job / ".durable-lifecycle.json"
        marker.write_text(json.dumps({"awaiting_review": True}))
        self.assert_kept(self.prune(now=NOW + 8 * 86400))
        marker.write_text(json.dumps({"awaiting_review": False, "review_closed": True, "outcome": "rejected"}))
        self.assertGreater(self.prune()["deleted_bytes"], 0)

    def test_saved_marker_without_actual_note_does_not_authorize_pruning(self):
        (self.job / ".durable-lifecycle.json").write_text(json.dumps({"review_closed": True, "outcome": "saved"}))
        self.assert_kept(self.prune())

    def test_vault_state_unreadable_keeps(self):
        self.save()
        (self.vault / ".brain-schema.json").unlink()
        self.assert_kept(self.prune(now=self.LATE))

    def test_schema_3_vault_keeps(self):
        self.save()
        self.write_schema(3)
        self.assert_kept(self.prune(now=self.LATE))

    def test_missing_inbox_folder_keeps(self):
        self.save()
        (self.vault / "Inbox").rmdir()
        self.assert_kept(self.prune(now=self.LATE))

    def test_unparseable_note_mentioning_video_keeps(self):
        self.save()
        (self.vault / "Inbox/broken.md").write_text("no frontmatter " + self.item["canonical_url"])
        self.assert_kept(self.prune(now=self.LATE))

    def test_unparseable_unrelated_note_is_ignored(self):
        self.save()
        (self.vault / "Inbox/other.md").write_text("no frontmatter at all")
        result = self.prune()
        self.assertFalse(self.media.exists(), result)

    def test_wrong_request_keeps(self):
        self.item["request_id"] = "other"
        self.write_item()
        self.assert_kept(self.prune(now=self.LATE))

    def test_active_worker_keeps(self):
        with (self.home / "social-video-work/worker.lock").open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.prune(now=self.LATE)
            self.assertEqual(result["reason"], "active_worker")
            self.assert_kept(result)

    def test_user_changed_inventory_file_keeps_whole_job(self):
        self.media.write_bytes(b"user changes")
        self.assert_kept(self.prune(now=self.LATE))

    def test_symbolic_escape_keeps_target(self):
        target = self.vault / "user-video.mp4"
        target.write_bytes(self.media.read_bytes())
        self.media.unlink()
        self.media.symlink_to(target)
        result = self.prune(now=self.LATE)
        self.assertTrue(target.exists(), result)
        self.assertTrue(self.media.is_symlink(), result)
        self.assertEqual(result["deleted_bytes"], 0)

    def test_hardlink_escape_keeps_target(self):
        target = self.vault / "user-video.mp4"
        os.link(self.media, target)
        self.assert_kept(self.prune(now=self.LATE))
        self.assertTrue(target.exists())

    def test_unknown_user_file_is_kept(self):
        user_file = self.job / "my-notes.txt"
        user_file.write_text("user work")
        result = self.prune(now=self.LATE)
        self.assertFalse(self.media.exists(), result)
        self.assertEqual(user_file.read_text(), "user work")

    def test_failed_without_note_keeps(self):
        self.item["state"] = "failed"
        self.write_item()
        self.assert_kept(self.prune(now=self.LATE))

    def test_dry_run_does_not_delete_or_write_tombstone(self):
        result = self.prune(now=NOW - 1 + 8 * 86400, dry_run=True)
        self.assertTrue(self.media.exists())
        self.assertEqual(result["deleted_bytes"], len(b"video content"))
        self.assertFalse((self.job / ".durable-pruned.json").exists())

    def test_checksum_inventory_matches(self):
        self.owner["files"][0]["sha256"] = hashlib.sha256(self.media.read_bytes()).hexdigest()
        self.owner["files"][0].pop("inode")
        self.write_owner()
        result = self.prune(now=NOW - 1 + 8 * 86400)
        self.assertFalse(self.media.exists(), result)

    def test_metadata_inventory_is_refused(self):
        self.owner["files"].append({"path": "item.json", "size": 0, "mtime_ns": 0, "inode": 0})
        self.write_owner()
        self.assert_kept(self.prune(now=NOW - 1 + 8 * 86400))


if __name__ == "__main__":
    unittest.main()
