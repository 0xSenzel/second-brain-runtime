"""Schema 4 brain tool hooks: capture protects, approve/discard request cleanup."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "extension"))
PLUGIN = ROOT / "plugin" / "__init__.py"
if not PLUGIN.exists():
    PLUGIN = Path("/opt/hermes/plugins/durable-video/__init__.py")
spec = importlib.util.spec_from_file_location("durable_video_plugin_hooks_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FakeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.calls = []

    def protect_capture(self, url, call_id):
        self.calls.append(("protect", url, call_id))

    def release_capture(self, call_id):
        self.calls.append(("release", call_id))


class HookTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FakeStore(self.directory.name)
        for target in ("home_path",):
            patcher = patch.object(plugin, target, return_value=Path(self.directory.name))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(plugin, "Inbox", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self):
        return self.store.root / "maintenance-request.json"

    def test_capture_protects_by_source_url_on_prefixed_tool_name(self):
        plugin.protect_source(tool_name="mcp_second_brain_brain_capture",
                              args={"source_url": "https://x.example/v/1", "content": "text"}, tool_call_id="c1")
        self.assertEqual(self.store.calls, [("protect", "https://x.example/v/1", "c1")])

    def test_other_tools_do_not_protect(self):
        plugin.protect_source(tool_name="mcp_second_brain_brain_find", args={"query": "x"}, tool_call_id="c1")
        plugin.protect_source(tool_name="mcp_second_brain_brain_prepare_save", args={"content_or_url": "/x"}, tool_call_id="c1")
        self.assertEqual(self.store.calls, [])

    def test_capture_result_releases_whether_it_succeeded_or_failed(self):
        for result in (json.dumps({"note_id": "note-1"}), json.dumps({"error": "boom"}), {"isError": True}):
            self.store.calls.clear()
            plugin.observe_tool(tool_name="mcp_second_brain_brain_capture", args={"source_url": "u"},
                                result=result, tool_call_id="c1")
            self.assertEqual(self.store.calls, [("release", "c1")])
        self.assertFalse(self.request().exists())

    def test_approve_and_discard_request_cleanup(self):
        for name in ("brain_approve", "brain_discard"):
            plugin.observe_tool(tool_name=f"mcp_second_brain_{name}", args={"note_id": "note-1"},
                                result=json.dumps({"note_id": "note-1"}), tool_call_id="c2")
            self.assertTrue(self.request().exists(), name)
            self.request().unlink()

    def test_failed_approve_requests_nothing(self):
        plugin.observe_tool(tool_name="mcp_second_brain_brain_approve", args={"note_id": "n"},
                            result=json.dumps({"error": "content_sha mismatch"}), tool_call_id="c3")
        self.assertFalse(self.request().exists())


if __name__ == "__main__":
    unittest.main()
