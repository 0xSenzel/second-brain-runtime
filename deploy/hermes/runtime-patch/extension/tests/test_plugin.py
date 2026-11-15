"""Native Telegram authentication precedes durable video processing."""
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "extension"))
PLUGIN = ROOT / "plugin" / "__init__.py"
if not PLUGIN.exists():
    PLUGIN = Path("/opt/hermes/plugins/durable-video/__init__.py")
spec = importlib.util.spec_from_file_location("durable_video_plugin_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def receiving(authorized):
    source = SimpleNamespace(user_id="7", chat_id="9", chat_type="dm", is_bot=False, thread_id=None, profile=None)
    return SimpleNamespace(_bot=SimpleNamespace(id=55), _owner_profile=None,
                           _source_from_message_for_auth=lambda message: source,
                           _is_sender_authorized=lambda *args, **kwargs: authorized,
                           _should_process_message=lambda message: True)


UPDATE = {"update_id": 10, "message": {"message_id": 8, "date": 1700000000,
          "chat": {"id": 9, "type": "private"}, "from": {"id": 7, "is_bot": False, "first_name": "Test"},
          "text": "https://v.douyin.com/example/"}}


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_or_unauthorized_sender_is_never_stored(self):
        for authorized in (None, False):
            self.assertEqual(plugin.envelopes_for(receiving(authorized), [UPDATE]), [])

    async def test_authorized_message_is_committed_before_return(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(plugin, "home_path", return_value=Path(directory)):
            self.assertIs(await plugin.receive_before_ack(adapter=receiving(True), updates=[UPDATE]), True)
            from inbox import Inbox
            self.assertEqual(len(Inbox(Path(directory)).requests()), 1)
            self.assertIs(await plugin.receive_before_ack(adapter=receiving(True), updates=[UPDATE]), True)
            self.assertEqual(len(Inbox(Path(directory)).requests()), 1)

    async def test_storage_failure_is_not_confirmed(self):
        with patch.object(plugin, "Inbox", side_effect=OSError("disk unavailable")), self.assertRaises(OSError):
            await plugin.receive_before_ack(adapter=receiving(True), updates=[UPDATE])

    async def test_batch_annotation_references_existing_jobs(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(plugin, "home_path", return_value=Path(directory)):
            adapter = receiving(True)
            await plugin.receive_before_ack(adapter=adapter, updates=[UPDATE])
            source = SimpleNamespace(platform="telegram", profile=None, chat_id="9", user_id="7")
            event = SimpleNamespace(source=source, message_id="8", text=UPDATE["message"]["text"])
            gateway = SimpleNamespace(_is_user_authorized_for_source=lambda source: True)
            reply = await plugin.annotate_received(event=event, gateway=gateway)
            self.assertEqual(reply["action"], "rewrite")
            self.assertIn("telegram-video:", reply["text"])
            self.assertIn("durable-video:lifecycle", reply["text"])

    async def test_nondefault_profile_is_not_processed_in_default_volume(self):
        adapter = receiving(True)
        adapter._owner_profile = "other"
        self.assertEqual(plugin.envelopes_for(adapter, [UPDATE]), [])

    async def test_group_trigger_gate_is_respected(self):
        adapter = receiving(True)
        adapter._should_process_message = lambda message: False
        self.assertEqual(plugin.envelopes_for(adapter, [UPDATE]), [])


if __name__ == "__main__":
    unittest.main()
