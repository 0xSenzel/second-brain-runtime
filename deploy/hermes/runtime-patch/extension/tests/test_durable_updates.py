"""Behavior contracts for required persistence before polling offset advancement."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent
if (ROOT / "durable_updates.py").exists():
    SPEC = importlib.util.spec_from_file_location("telegram_durable_updates_staged", ROOT / "durable_updates.py")
    durable = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(durable)
else:
    from plugins.platforms.telegram import durable_updates as durable


def adapter(required=True):
    return SimpleNamespace(config=SimpleNamespace(extra={"durable_updates_required": required}),
                           _teardown_started=False, _polling_progress_accepting=True,
                           _polling_generation=2)


class Request:
    def __init__(self, updates=None):
        self.result = (200, json.dumps({"ok": True, "result": updates or [{"update_id": 12}]}).encode())

    @staticmethod
    def parse_json_payload(payload):
        return json.loads(payload)

    async def do_request(self, *args, **kwargs):
        return self.result


class DurableUpdateTests(unittest.IsolatedAsyncioTestCase):
    def callbacks(self, *callbacks):
        manager = SimpleNamespace(iter_hook_callbacks=lambda name: callbacks)
        return patch("hermes_cli.plugins.get_plugin_manager", return_value=manager)

    async def test_required_without_consumer_refuses(self):
        request = Request()
        with self.callbacks(), self.assertRaises(OSError):
            await durable.persist_polling_updates(adapter(), request, 2, request.result)

    async def test_async_and_sync_consumers_must_confirm(self):
        calls = []
        async def asynchronous(**kwargs):
            calls.append(kwargs)
            return True
        def synchronous(**kwargs):
            calls.append(kwargs)
            return True
        receiving = adapter()
        request = Request()
        with self.callbacks(asynchronous, synchronous):
            await durable.persist_polling_updates(receiving, request, 2, request.result)
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0]["adapter"], receiving)
        self.assertEqual(calls[0]["updates"], ({"update_id": 12},))

    async def test_error_false_and_truthy_values_refuse(self):
        def failure(**kwargs):
            raise RuntimeError("disk unavailable")
        for callback in (failure, lambda **kwargs: False, lambda **kwargs: 1, lambda **kwargs: None):
            with self.subTest(callback=callback), self.callbacks(callback), self.assertRaises(OSError):
                request = Request()
                await durable.persist_polling_updates(adapter(), request, 2, request.result)

    async def test_default_and_old_generations_do_not_journal(self):
        def unexpected(**kwargs):
            self.fail("disabled or stale request reached persistence")
        request = Request()
        with self.callbacks(unexpected):
            await durable.persist_polling_updates(adapter(False), request, 2, request.result)
            await durable.persist_polling_updates(adapter(), request, 1, request.result)
            await durable.persist_polling_updates(adapter(), request, None, request.result)
            receiving = adapter()
            receiving._teardown_started = True
            await durable.persist_polling_updates(receiving, request, 2, request.result)

    async def test_bad_envelopes_remain_ptb_responsibility(self):
        request = Request()
        for result in ((500, b'{}'), (200, b'broken'), (200, b'{"ok":false}'),
                       (200, b'{"ok":true,"result":{}}')):
            with self.subTest(result=result), self.callbacks():
                await durable.persist_polling_updates(adapter(), request, 2, result)

    async def test_timeout_refuses_and_cancellation_propagates(self):
        async def blocked(**kwargs):
            await asyncio.Future()
        request = Request()
        with self.callbacks(blocked), patch.object(durable, "CALLBACK_TIMEOUT_SECONDS", 0):
            with self.assertRaises(OSError):
                await durable.persist_polling_updates(adapter(), request, 2, request.result)
        async def cancelled(**kwargs):
            raise asyncio.CancelledError()
        with self.callbacks(cancelled), self.assertRaises(asyncio.CancelledError):
            await durable.persist_polling_updates(adapter(), request, 2, request.result)

    async def test_repeat_delivery_reaches_idempotent_consumer(self):
        committed = set()
        attempts = []
        def persist(**kwargs):
            for update in kwargs["updates"]:
                attempts.append(update["update_id"])
                committed.add(update["update_id"])
            return True
        request = Request()
        with self.callbacks(persist):
            await durable.persist_polling_updates(adapter(), request, 2, request.result)
            await durable.persist_polling_updates(adapter(), request, 2, request.result)
        self.assertEqual(attempts, [12, 12])
        self.assertEqual(committed, {12})

    async def test_actual_instrumented_request_withholds_http_result_and_offset(self):
        name = "plugins.platforms.telegram._durable_adapter_test"
        staged = ROOT / "adapter.patched.py"
        if staged.exists():
            spec = importlib.util.spec_from_file_location(name, staged)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
        else:
            from plugins.platforms.telegram import adapter as module
        with patch.dict(sys.modules, {"plugins.platforms.telegram.durable_updates": durable}):
            if staged.exists():
                spec.loader.exec_module(module)
            receiving = adapter()
            observed = []
            receiving._observe_polling_request_result = lambda *args: observed.append(args)
            request = module.TelegramAdapter._instrument_polling_request(receiving, Request())
            offset = 0
            async def updater_once():
                nonlocal offset
                status, payload = await request.do_request()
                updates = request.parse_json_payload(payload)["result"]
                offset = max(update["update_id"] for update in updates) + 1
            token = module._POLLING_GENERATION_CONTEXT.set(2)
            try:
                with self.callbacks(), self.assertRaises(OSError):
                    await updater_once()
                self.assertEqual(offset, 0)
                self.assertEqual(observed, [])
                with self.callbacks(lambda **kwargs: True):
                    await updater_once()
                self.assertEqual(offset, 13)
                self.assertEqual(len(observed), 1)
            finally:
                module._POLLING_GENERATION_CONTEXT.reset(token)
                sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
