---
name: lifecycle
description: Use for a durable video intake receipt, video status, or Inbox capture and cleanup linked to those jobs.
---

# Durable video lifecycle

Use this guide with the existing `short-video-capture` skill. Preserve its summary language,
evidence requirements, and exact-text approval rules.

## Reuse the intake receipt

The runtime saves supported, authorized Telegram video requests before acknowledging their
polling response. The receipt contains stable request and item IDs. A queued receipt does
not prove capture or analysis has started.

For an item in the receipt, inspect the existing job:

```sh
/opt/hermes/.venv/bin/python /opt/data/scripts/social_video_workflow.py status ITEM_ID
```

Read that item's `artifacts.result` before summarizing it. If the item does not exist yet,
inspect the intake worker instead of submitting a second request:

```sh
/opt/hermes/.venv/bin/python /opt/durable-video/inbox.py status
```

If the receipt says `processing_authorized: false`, activate it only when the user's
actual request authorizes video analysis:

```sh
/opt/hermes/.venv/bin/python /opt/durable-video/inbox.py activate REQUEST_ID
```

Do not activate capture-only, captions-only, negative, or cancelled requests as analysis.
Do not submit the same message with another request ID. A separate user request may create
another job for the same URL.

## Protect a draft awaiting the user's decision

Before presenting an inline draft for the user's decision, protect every source item:

```sh
/opt/hermes/.venv/bin/python /opt/durable-video/inbox.py review-open ITEM_ID
```

This saves lifecycle metadata only. It does not save the draft, write to the Second Brain,
or approve anything.

## Save to the Inbox

Saving needs no approval. Call `brain_capture` with the source text as `content`, and set
`source_url` to the video's canonical URL. The runtime matches that URL to the job and keeps
its media while the note is undecided. Pass text, not a file path: the brain runs in another
container.

A note in `Inbox/` (status `inbox`) keeps the media. The media becomes eligible for cleanup
when the note is approved (it moves to `Notes/`) or discarded (it moves to `Archive/Inbox/`).
Approval happens through `brain_approve`, the `/brain_approve` command, or Obsidian.

If you drop an inline draft that was never captured, close its review:

```sh
/opt/hermes/.venv/bin/python /opt/durable-video/inbox.py review-close ITEM_ID --outcome rejected
```

Do not close a review when replacing a draft; keep it protected until the replacement is ready.
`--outcome saved` records the closure only. It never authorizes cleanup by itself.

The cleanup worker independently reads the vault. Approval alone does not authorize cleanup
unless the approved note, and its source note, exist.

## Retention

- Keep active work, incomplete analysis, Inbox notes for the video, and protected inline reviews.
- Remove unchanged owned artifacts after the video's note is approved (with its source) or discarded.
- If the vault state cannot be read, keep everything.
- For completed analysis with no note for the video, retain work files for seven days.
- Check cleanup hourly and after save/reject events. Keep small identity and cleanup receipts.
- Preserve saved notes, vault source archives, unknown legacy jobs, and user-modified files.

Check `durable-video/heartbeat.json`, `worker-error.json`, and `last-prune.json` under the
Hermes data home when diagnosing the worker. A `.durable-pruned.json` receipt means that
the job's historical processing finished but its owned scratch artifacts were removed.
Do not claim its removed result is still available or silently capture it again.
