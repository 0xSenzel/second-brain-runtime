"""Out-of-band approval for second-brain Inbox notes.

Slash commands run in the gateway process, before and outside any model turn
(gateway/session_commands.py `_run_runner_command`). Each handler calls the brain
over MCP through `ctx.call_mcp`, which needs `plugins.entries.second-brain-approval.
mcp_allowlist: ["second-brain-approver"]`. That server entry registers no tools for the
model, and the model-facing `second-brain` entry hides `brain_approve`, so the model has
no path to approval.

    /brain-review <note_id>            show the exact summary and body, plus a sha8
    /brain-approve <note_id> <sha8>    approve only if sha8 matches the current note
    /brain-discard <note_id> [reason]  archive the note

ASSUMPTION to verify against the schema-4 build: `brain_inbox` returns the Inbox items
(a list, or an object holding one) and each item carries note_id, title, summary, body,
content_sha. brain_inbox(note_id) returns {"item": {...}} with the full body.
"""
from __future__ import annotations

import json
from typing import Any

SERVER = "second-brain-approver"
SHA_PREFIX = 8


def _payload(envelope: dict) -> Any:
    if not envelope.get("ok"):
        raise RuntimeError(str(envelope.get("error") or "brain call failed"))
    # The brain's tools return JSON as text. FastMCP also emits that text wrapped as
    # structuredContent {"result": "<text>"}, and Hermes forwards both, so the text in
    # `result` is the payload; structuredContent only fills in when it is absent.
    value = envelope.get("result")
    structured = envelope.get("structuredContent")
    if not value and isinstance(structured, dict):
        value = structured.get("result", structured)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _items(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        for key in ("items", "inbox", "notes"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    return [i for i in payload if isinstance(i, dict)] if isinstance(payload, list) else []


def register(ctx) -> None:
    def call(tool: str, arguments: dict | None = None) -> Any:
        return _payload(ctx.call_mcp(SERVER, tool, arguments or {}, timeout=60))

    def find(note_id: str) -> dict:
        # brain_inbox(note_id) returns {"item": {...}} with the full body and content_sha;
        # the listing form omits the body, so a review built from it would be incomplete.
        payload = call("brain_inbox", {"note_id": note_id})
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and item.get("note_id") == note_id:
            return item
        for item in _items(payload):
            if item.get("note_id") == note_id:
                return item
        raise LookupError(f"{note_id} is not in the Inbox")

    def review(raw_args: str) -> str:
        note_id = raw_args.strip()
        if not note_id:
            return "Usage: /brain-review <note_id>"
        item = find(note_id)
        sha8 = str(item.get("content_sha", ""))[:SHA_PREFIX]
        return (
            f"{item.get('title')}  [{item.get('note_type')}, {item.get('domain')}]\n"
            f"tags: {', '.join(item.get('tags') or [])}\n\n"
            f"SUMMARY\n{item.get('summary', '')}\n\n"
            f"BODY\n{item.get('body', '') or '(empty)'}\n\n"
            f"Approve: /brain-approve {note_id} {sha8}\n"
            f"Discard: /brain-discard {note_id}"
        )

    def approve(raw_args: str) -> str:
        parts = raw_args.split()
        if len(parts) != 2 or len(parts[1]) < SHA_PREFIX:
            return f"Usage: /brain-approve <note_id> <sha{SHA_PREFIX}> (run /brain-review first)"
        note_id, sha8 = parts[0], parts[1].lower()
        item = find(note_id)
        sha = str(item.get("content_sha", ""))
        if not sha.startswith(sha8):
            return f"Refused: {note_id} changed since you reviewed it. Run /brain-review {note_id} again."
        result = call("brain_approve", {"note_id": note_id, "content_sha": sha, "user_approved": True})
        return f"Approved {note_id}.\n{json.dumps(result, ensure_ascii=False)[:1500]}"

    def discard(raw_args: str) -> str:
        parts = raw_args.split(maxsplit=1)
        if not parts:
            return "Usage: /brain-discard <note_id> [reason]"
        args = {"note_id": parts[0], "reason": parts[1] if len(parts) > 1 else "discarded from chat"}
        result = call("brain_discard", args)
        return f"Discarded {parts[0]}.\n{json.dumps(result, ensure_ascii=False)[:1500]}"

    ctx.register_command("brain-review", review, "Show an Inbox note verbatim", "<note_id>")
    ctx.register_command("brain-approve", approve, "Approve an Inbox note you reviewed", "<note_id> <sha8>")
    ctx.register_command("brain-discard", discard, "Discard an Inbox note", "<note_id> [reason]")
