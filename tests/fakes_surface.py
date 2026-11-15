"""Small fake service/index/embedder that follow the schema 4 contract names."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from second_brain.models import (
    ApprovalMismatch,
    ApproveResult,
    ArchiveResult,
    CaptureResult,
    InboxItem,
)


def make_item(note_id: str = "note-aaaaaaaaaaaa", **overrides: Any) -> InboxItem:
    values: dict[str, Any] = dict(
        note_id=note_id,
        note_path=f"Inbox/{note_id}.md",
        title="Example",
        note_type="concept",
        domain="tech",
        summary="A summary.",
        body="A body.",
        tags=["ui"],
        aliases=[],
        source_url="https://example.com/post/1234567",
        created="2026-11-01T00:00:00Z",
        content_sha="sha-1",
        text="full text",
    )
    values.update(overrides)
    return InboxItem(**values)


class FakeService:
    def __init__(self, vault: Path | None = None):
        self.vault = vault or Path("/nonexistent-vault")
        self.calls: list[tuple[str, tuple, dict]] = []
        self.inbox_items: list[InboxItem] = [make_item()]
        self.promoted: list[Any] = []
        self.promote_error: Exception | None = None
        self.tags = {"ui": "User interface."}

    def _log(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def capture(self, **kwargs: Any) -> CaptureResult:
        self._log("capture", **kwargs)
        return CaptureResult(
            note_id="note-bbbbbbbbbbbb",
            note_path="Inbox/New.md",
            source_path="Sources/s-New.md",
            content_sha="sha-new",
            problems=["summary is empty"] if not kwargs["summary"] else [],
        )

    def update_inbox(self, note_id: str, **kwargs: Any) -> CaptureResult:
        self._log("update_inbox", note_id, **kwargs)
        return CaptureResult(note_id, "Inbox/New.md", "Sources/s.md", "sha-updated")

    def list_inbox(self) -> list[InboxItem]:
        self._log("list_inbox")
        return list(self.inbox_items)

    def get_inbox(self, note_id: str) -> InboxItem:
        self._log("get_inbox", note_id)
        for item in self.inbox_items:
            if item.note_id == note_id:
                return item
        raise KeyError(note_id)

    def approve(self, note_id: str, *, content_sha: str) -> ApproveResult:
        self._log("approve", note_id, content_sha=content_sha)
        if content_sha != self.get_inbox(note_id).content_sha:
            raise ApprovalMismatch("The note changed after review")
        return ApproveResult(note_id, "Notes/Example.md")

    def discard(self, note_id: str, *, reason: str = "") -> ArchiveResult:
        self._log("discard", note_id, reason=reason)
        return ArchiveResult(note_id, ["Archive/Inbox/Example.md"], "Archive/Receipts/r.json")

    def promote_reviewed(self) -> list[Any]:
        self._log("promote_reviewed")
        if self.promote_error:
            raise self.promote_error
        return list(self.promoted)

    def register_tags(self, tags: Any, *, meanings: dict[str, str] | None = None) -> list[str]:
        self._log("register_tags", list(tags), meanings=meanings)
        self.tags.update({tag: (meanings or {}).get(tag, "") for tag in tags})
        return list(tags)

    def tag_catalog(self) -> dict[str, str]:
        return dict(self.tags)

    def registered_tags(self) -> set[str]:
        return set(self.tags)

    def validate(self) -> dict:
        self._log("validate")
        return {"schema_version": 4, "problems": []}

    def schema_mismatch(self) -> None:
        return None


class FakeIndex:
    db_path = Path("/nonexistent/index.sqlite3")

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.hits: list[dict] = [{"id": "note-1", "title": "Hit", "path": "Notes/Hit.md"}]
        self.embedder = None

    def reindex(self, *, embed_vectors: bool = False) -> dict:
        self.calls.append(("reindex", (), {"embed_vectors": embed_vectors}))
        return {"indexed": 1, "warnings": []}

    def find(self, query: str, limit: int = 5, include_inbox: bool = True) -> list[dict]:
        self.calls.append(("find", (query,), {"limit": limit, "include_inbox": include_inbox}))
        return list(self.hits)

    def context(self, query: str, **kwargs: Any) -> dict:
        self.calls.append(("context", (query,), kwargs))
        return {"query": query, "sources": [{"path": "Notes/Hit.md", "unreviewed": False}], "warnings": []}

    def read_note(self, note_id: str, *, section: str = "", max_chars: int = 12000) -> dict:
        self.calls.append(("read_note", (note_id,), {"section": section}))
        return {"id": note_id, "path": "Notes/Hit.md", "body": "Body"}

    def neighbors(self, note_id: str) -> list[dict]:
        return [{"id": "note-2"}]


class FakeEmbedder:
    model = "fake"

    def __init__(self, up: bool = True):
        self.up = up

    def available(self) -> bool:
        return self.up

    def embed(self, text: str) -> list[float]:
        raise AssertionError("not expected")
