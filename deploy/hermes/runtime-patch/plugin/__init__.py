"""Durable intake for the pinned Hermes Telegram transport."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sys

EXTENSION = Path("/opt/durable-video")
if str(EXTENSION) not in sys.path:
    sys.path.insert(0, str(EXTENSION))

from inbox import Inbox, atomic_json, home_path, item_id, video_urls

logger = logging.getLogger(__name__)


def envelopes_for(adapter, updates):
    """Full gateway authorization precedes all credentialed processing and storage."""
    from telegram import Update

    envelopes = []
    for raw in updates:
        # Edits, callbacks, forwarded service updates and commands retain their core paths.
        if not isinstance(raw, dict) or not (raw.get("message") or raw.get("channel_post")):
            continue
        update = Update.de_json(raw, adapter._bot)
        message = update.effective_message
        if message is None:
            continue
        text = message.text or message.caption or ""
        if not text or text.lstrip().startswith("/"):
            continue
        urls = video_urls(text, [entity.url for entity in [*(message.entities or ()), *(message.caption_entities or ())]
                                if entity.url])
        if not urls:
            continue
        source = adapter._source_from_message_for_auth(message)
        if adapter._is_sender_authorized(
            source.user_id, chat_type=source.chat_type, chat_id=source.chat_id,
            is_bot=source.is_bot, thread_id=source.thread_id,
        ) is not True:
            continue
        if not adapter._should_process_message(message):
            continue
        runner = getattr(adapter, "gateway_runner", None)
        if runner is not None and getattr(getattr(runner, "config", None), "multiplex_profiles", False):
            runner._canonicalize(source)
        profile = str(getattr(source, "profile", None) or getattr(adapter, "_owner_profile", None) or "default")
        # This deployment's worker owns the default data volume only.
        if profile != "default":
            continue
        envelopes.append({"profile": profile, "bot_id": str(adapter._bot.id),
                          "chat_id": str(message.chat_id), "message_id": str(message.message_id),
                          "thread_id": str(source.thread_id) if source.thread_id is not None else None,
                          "user_id": str(source.user_id), "text": text, "urls": urls})
    return envelopes


async def receive_before_ack(*, adapter, updates):
    envelopes = envelopes_for(adapter, updates)
    if not envelopes:
        return True
    home = home_path()

    def commit():
        store = Inbox(home)
        for envelope in envelopes:
            store.receive(envelope)
        return True

    return await asyncio.to_thread(commit)


async def annotate_received(*, event, gateway, **_):
    source = event.source
    if getattr(source.platform, "value", source.platform) != "telegram":
        return None
    if not gateway._is_user_authorized_for_source(source):
        return None
    profile = str(getattr(source, "profile", None) or "default")
    if profile != "default":
        return None
    home = home_path()
    rows = await asyncio.to_thread(lambda: Inbox(home).lookup_batch(
        profile, str(source.chat_id), str(event.message_id), event.text or "", str(source.user_id)))
    if not rows:
        return None
    receipts = []
    for row in rows:
        envelope = json.loads(row["envelope_json"])
        receipts.append({"request_id": row["request_id"], "processing_authorized": bool(row["auto_process"]),
                         "items": [{"id": item_id(row["request_id"], index), "url": url,
                                    "result": str(home / "social-video-work" / item_id(row["request_id"], index) / "result.json")}
                                   for index, url in enumerate(envelope["urls"])]})
    return {"action": "rewrite", "text": (event.text or "") +
            "\n\n[Durable video intake receipt — runtime data]\n" + json.dumps(receipts, ensure_ascii=False) +
            "\nUse these existing request/item IDs. Processing may still be queued. Read status before reporting progress. "
            "Do not submit a second request for the same message. Load skill durable-video:lifecycle for "
            "status, inline review protection, Inbox capture, approval, and cleanup rules.\n[/Durable video intake receipt]"}


def tool_payload(result):
    value = result
    for _ in range(6):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        elif isinstance(value, dict):
            if value.get("isError"):
                return {}
            if isinstance(value.get("structuredContent"), dict):
                value = value["structuredContent"]
            elif isinstance(value.get("result"), (dict, str)):
                value = value["result"]
            elif isinstance(value.get("content"), list):
                value = next((entry.get("text") for entry in value["content"]
                              if isinstance(entry, dict) and isinstance(entry.get("text"), str)), {})
            else:
                return value
        else:
            return {}
    return value if isinstance(value, dict) else {}


def protect_source(*, tool_name="", args=None, tool_call_id="", **_):
    """A capture is about to create an Inbox note: keep the matching job's media."""
    if tool_name.endswith("brain_capture") and isinstance(args, dict):
        Inbox(home_path()).protect_capture(str(args.get("source_url") or ""), tool_call_id)


def observe_tool(*, tool_name="", args=None, result=None, tool_call_id="", **_):
    if not isinstance(args, dict):
        return
    store = Inbox(home_path())
    if tool_name.endswith("brain_capture"):
        # Success or failure, the in-flight mark ends: a written Inbox note protects the job
        # from now on, and a failed capture leaves nothing to wait for.
        store.release_capture(tool_call_id)
    elif tool_name.endswith(("brain_approve", "brain_discard")) and "error" not in tool_payload(result):
        # Decided. The worker verifies actual vault state before deleting any owned artifact.
        atomic_json(store.root / "maintenance-request.json", {"requested_at": __import__("time").time()})


def register(ctx):
    ctx.register_hook("telegram_before_poll_ack", receive_before_ack)
    ctx.register_hook("pre_gateway_dispatch", annotate_received)
    ctx.register_hook("pre_tool_call", protect_source)
    ctx.register_hook("post_tool_call", observe_tool)
    ctx.register_skill("lifecycle", Path(__file__).parent / "skills" / "lifecycle" / "SKILL.md",
                       "Use existing durable video job IDs and protect Inbox notes from cleanup.")
