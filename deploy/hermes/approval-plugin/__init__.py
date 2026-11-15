"""Out-of-band approval for second-brain Inbox notes.

Slash commands run in the gateway process, before and outside any model turn
(gateway/session_commands.py `_run_runner_command`). Each handler calls the brain
over MCP through `ctx.call_mcp`, which needs `plugins.entries.second-brain-approval.
mcp_allowlist: ["second-brain-approver"]`. That server entry registers no tools for the
model, and the model-facing `second-brain` entry hides `brain_approve`, so the model has
no path to approval.

Telegram shows these as /brain_inbox, /brain_review, /brain_approve, /brain_discard
(it allows no hyphens; Hermes maps the underscore form back). Every argument is optional,
because the Telegram menu leaves out commands whose hint marks a required argument
(a menu tap sends the bare command):

    /brain_inbox                         list Inbox items
    /brain_review [note_id]              show one item verbatim (default: the oldest)
    /brain_approve [note_id sha8]        approve; with no arguments, the item this chat
                                         last reviewed, if it is unchanged since
    /brain_discard [note_id] [reason]    archive; with no arguments, the item last reviewed

The no-argument forms keep the approval bound to what was shown: the full content_sha
from the review is checked again before approving.
"""
from __future__ import annotations

import json
from typing import Any

SERVER = "second-brain-approver"
SHA_PREFIX = 8

# session_key -> (note_id, content_sha) of the item that chat reviewed last. In memory on
# purpose: after a gateway restart the user reviews again rather than approving blind.
_LAST_REVIEWED: dict[str, tuple[str, str]] = {}


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

    def listing() -> list[dict]:
        items = _items(call("brain_inbox"))
        return sorted(items, key=lambda item: str(item.get("created") or ""))

    def find(note_id: str) -> dict:
        # brain_inbox(note_id) returns {"item": {...}} with the full body and content_sha;
        # the listing form omits the body, so a review built from it would be incomplete.
        payload = call("brain_inbox", {"note_id": note_id})
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and item.get("note_id") == note_id:
            return item
        raise LookupError(f"{note_id} is not in the Inbox")

    def remaining() -> str:
        count = len(listing())
        return f"{count} left in the Inbox." + (" Next: /brain_review" if count else "")

    def inbox(raw_args: str = "", **_context: Any) -> str:
        items = listing()
        if not items:
            return "Inbox empty."
        lines = [f"Inbox: {len(items)} item{'s' if len(items) != 1 else ''}"]
        for item in items:
            label = " (revision)" if item.get("revises") else ""
            lines.append(f"- {item.get('title')}{label}  {item.get('note_id')}")
        lines.append("Review the oldest: /brain_review")
        return "\n".join(lines)

    def review(raw_args: str = "", session_key: str = "", **_context: Any) -> str:
        note_id = raw_args.strip()
        if not note_id:
            items = listing()
            if not items:
                return "Inbox empty."
            note_id = str(items[0]["note_id"])
        item = find(note_id)
        sha = str(item.get("content_sha", ""))
        _LAST_REVIEWED[session_key] = (note_id, sha)
        revises = f"\nRevises: {item['revises']}" if item.get("revises") else ""
        problems = item.get("problems") or []
        blocked = ("\nCannot be approved yet: " + "; ".join(problems)) if problems else ""
        return (
            f"{item.get('title')}  [{item.get('note_type')}, {item.get('domain')}]{revises}\n"
            f"tags: {', '.join(item.get('tags') or [])}{blocked}\n\n"
            f"SUMMARY\n{item.get('summary', '')}\n\n"
            f"BODY\n{item.get('body', '') or '(empty)'}\n\n"
            f"Approve this version: /brain_approve\n"
            f"Discard it: /brain_discard\n"
            f"Skip: /brain_inbox\n"
            f"(explicit form: /brain_approve {note_id} {sha[:SHA_PREFIX]})"
        )

    def approve(raw_args: str = "", session_key: str = "", **_context: Any) -> str:
        parts = raw_args.split()
        if not parts:
            if session_key not in _LAST_REVIEWED:
                return "Run /brain_review first, then /brain_approve approves exactly what it showed."
            note_id, reviewed_sha = _LAST_REVIEWED[session_key]
            sha8 = reviewed_sha[:SHA_PREFIX]
        elif len(parts) == 2 and len(parts[1]) >= SHA_PREFIX:
            note_id, sha8 = parts[0], parts[1].lower()
            reviewed_sha = ""
        else:
            return f"Usage: /brain_approve, or /brain_approve <note_id> <sha{SHA_PREFIX}>"
        item = find(note_id)
        sha = str(item.get("content_sha", ""))
        if (reviewed_sha and sha != reviewed_sha) or not sha.startswith(sha8):
            return f"Refused: {item.get('title')} changed since you reviewed it. Run /brain_review {note_id} again."
        result = call("brain_approve", {"note_id": note_id, "content_sha": sha, "user_approved": True})
        _LAST_REVIEWED.pop(session_key, None)
        path = result.get("note_path", "") if isinstance(result, dict) else ""
        return f"Approved {item.get('title')} → {path}\n{remaining()}"

    def discard(raw_args: str = "", session_key: str = "", **_context: Any) -> str:
        parts = raw_args.split(maxsplit=1)
        if parts and parts[0].startswith("note-"):
            note_id, reason = parts[0], (parts[1] if len(parts) > 1 else "")
        elif session_key in _LAST_REVIEWED:
            note_id, reason = _LAST_REVIEWED[session_key][0], raw_args.strip()
        else:
            return "Run /brain_review first, or use /brain_discard <note_id> [reason]."
        call("brain_discard", {"note_id": note_id, "reason": reason or "discarded from chat"})
        _LAST_REVIEWED.pop(session_key, None)
        return f"Discarded {note_id} (archived, recoverable).\n{remaining()}"

    ctx.register_command("brain-inbox", inbox, "List second-brain Inbox items", "")
    ctx.register_command("brain-review", review, "Review the next Inbox note", "[note_id]")
    ctx.register_command("brain-approve", approve, "Approve the note you reviewed", "[note_id sha8]")
    ctx.register_command("brain-discard", discard, "Discard the note you reviewed", "[note_id] [reason]")
