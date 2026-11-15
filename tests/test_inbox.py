from __future__ import annotations

from pathlib import Path

import pytest

from second_brain.models import ApprovalMismatch, ApproveResult, ValidationError
from second_brain.notes import dump_note, parse_note, read_note
from second_brain.service import BrainService


@pytest.fixture
def service(tmp_path: Path) -> BrainService:
    service = BrainService(tmp_path / "vault")
    service.register_tags(["ui", "design-review", "data"])
    return service


def _capture(service: BrainService, **overrides):
    payload = dict(
        content="Raw source text.",
        title="Design Review",
        summary="A prompt for design review. It has a second sentence.",
        body="## Prompt\n\nCheck hierarchy.",
        note_type="prompt",
        tags=["ui"],
    )
    payload.update(overrides)
    return service.capture(**payload)


def _set_status(service: BrainService, rel: str, status: str) -> None:
    path = service.vault / rel
    meta, body = read_note(path)
    meta["status"] = status
    path.write_text(dump_note(meta, body), encoding="utf-8")


def test_list_and_get_inbox(service: BrainService) -> None:
    first = _capture(service, title="First")
    _capture(service, title="Second")

    items = service.list_inbox()
    assert [item.title for item in items] == ["First", "Second"]
    item = service.get_inbox(first.note_id)
    assert item.content_sha == first.content_sha
    assert item.summary.startswith("A prompt for design review.")
    assert item.body == "## Prompt\n\nCheck hierarchy."
    assert item.text == (service.vault / first.note_path).read_text()
    assert item.problems == []
    with pytest.raises(KeyError):
        service.get_inbox("note-000000000000")


def test_update_inbox_edits_fields_and_changes_the_sha(service: BrainService) -> None:
    result = _capture(service, tags=[])
    assert result.problems
    created = read_note(service.vault / result.note_path)[0]["created"]

    updated = service.update_inbox(result.note_id, tags=["ui"], summary="New summary.", title="Renamed")

    assert updated.updated_existing and updated.problems == []
    assert updated.content_sha != result.content_sha
    assert updated.note_path == "Inbox/Renamed.md"
    assert not (service.vault / result.note_path).exists()
    meta, _ = read_note(service.vault / updated.note_path)
    assert meta["id"] == result.note_id and meta["created"] == created
    assert service.get_inbox(result.note_id).summary == "New summary."


def test_approve_moves_the_note_into_notes_and_updates_maps(service: BrainService) -> None:
    (service.vault / "Maps" / "UI.md").write_text(
        dump_note({"type": "map", "title": "UI", "map_tags": ["ui"]}, "# UI\n"), encoding="utf-8"
    )
    result = _capture(service)

    approved = service.approve(result.note_id, content_sha=result.content_sha)

    assert approved.note_path == "Notes/Design Review.md" and not approved.revised
    assert approved.maps_updated == ["UI"]
    assert not (service.vault / result.note_path).exists()
    meta, body = read_note(service.vault / approved.note_path)
    assert meta["status"] == "approved" and meta["id"] == result.note_id
    assert meta["reviewed"] and "revises" not in meta
    assert "## Prompt" in body and "## Source" in body
    map_text = (service.vault / "Maps" / "UI.md").read_text()
    assert "- [[Design Review]] — A prompt for design review." in map_text
    source_meta, _ = read_note(service.vault / result.source_path)
    assert source_meta["note_id"] == result.note_id
    assert service.list_inbox() == []
    assert service.validate()["findings"] == []


def test_approve_with_a_stale_sha_changes_nothing(service: BrainService) -> None:
    result = _capture(service)
    service.update_inbox(result.note_id, summary="Edited after review.")
    before = (service.vault / result.note_path).read_text()

    with pytest.raises(ApprovalMismatch):
        service.approve(result.note_id, content_sha=result.content_sha)

    assert (service.vault / result.note_path).read_text() == before
    assert not list((service.vault / "Notes").glob("*.md"))


def test_approve_refuses_an_invalid_note_and_leaves_it_untouched(service: BrainService) -> None:
    result = _capture(service, tags=["not-registered"])
    before = (service.vault / result.note_path).read_text()

    with pytest.raises(ValidationError, match="not registered"):
        service.approve(result.note_id, content_sha=result.content_sha)

    assert (service.vault / result.note_path).read_text() == before


def test_plain_approval_with_a_title_clash_gets_a_suffix(service: BrainService) -> None:
    first = _capture(service)
    service.approve(first.note_id, content_sha=first.content_sha)
    second = _capture(service)

    approved = service.approve(second.note_id, content_sha=second.content_sha)

    assert approved.note_path == "Notes/Design Review (2).md"
    assert read_note(service.vault / approved.note_path)[0]["title"] == "Design Review (2)"


def test_revising_twice_keeps_one_note_its_created_date_and_every_source(service: BrainService) -> None:
    first = _capture(service, source_url="https://example.com/a")
    approved = service.approve(first.note_id, content_sha=first.content_sha)
    original, _ = read_note(service.vault / approved.note_path)

    sources = [first.source_path]
    for number in (2, 3):
        revision = _capture(
            service, content=f"Source number {number}.", summary=f"Revision {number}.",
            body=f"Body {number}.", revises=original["id"], source_url="https://example.com/a",
            tags=["data"], title=f"Retitled {number}",
        )
        assert revision.problems == []
        result = service.approve(revision.note_id, content_sha=revision.content_sha)
        assert result.revised and result.note_path == approved.note_path
        sources.append(revision.source_path)

    notes = list((service.vault / "Notes").glob("*.md"))
    assert notes == [service.vault / approved.note_path]
    meta, body = read_note(notes[0])
    assert meta["id"] == original["id"] and meta["created"] == original["created"]
    assert meta["updated"] and meta["reviewed"]
    assert meta["title"] == "Design Review"
    assert meta["sources"] == [f"[[{Path(path).stem}]]" for path in sources]
    assert "Revision 3." in body and "Body 3." in body and "Body 2." not in body
    assert meta["tags"] == ["data"]
    assert service.list_inbox() == []
    report = service.validate()
    assert report["orphan_sources"] == [] and report["findings"] == []
    for path in sources:
        assert read_note(service.vault / path)[0]["note_id"] == original["id"]


def test_revision_of_a_missing_note_is_a_blocking_problem(service: BrainService) -> None:
    result = _capture(service, revises="note-ffffffffffff")

    assert any("revises" in problem for problem in result.problems)
    with pytest.raises(ValidationError):
        service.approve(result.note_id, content_sha=result.content_sha)


def test_discard_archives_note_and_source_with_a_receipt(service: BrainService) -> None:
    result = _capture(service)

    archived = service.discard(result.note_id, reason="not useful")

    assert archived.archived == [
        "Archive/Inbox/Design Review.md",
        f"Archive/{result.source_path}",
    ]
    assert not (service.vault / result.note_path).exists()
    assert not (service.vault / result.source_path).exists()
    receipt = (service.vault / archived.receipt_path).read_text()
    assert "not useful" in receipt and result.note_id in receipt


def test_discard_keeps_a_source_another_note_still_lists(service: BrainService) -> None:
    first = _capture(service)
    approved = service.approve(first.note_id, content_sha=first.content_sha)
    original, _ = read_note(service.vault / approved.note_path)
    revision = _capture(service, revises=original["id"])
    service.approve(revision.note_id, content_sha=revision.content_sha)
    pending = _capture(service, title="Other")
    # Point the new inbox note at the first note's source, as a revision-in-progress would.
    path = service.vault / pending.note_path
    meta, body = read_note(path)
    meta["sources"] = [f"[[{Path(first.source_path).stem}]]"]
    path.write_text(dump_note(meta, body), encoding="utf-8")

    archived = service.discard(pending.note_id)

    assert archived.archived == ["Archive/Inbox/Other.md"]
    assert (service.vault / first.source_path).exists()


def test_promote_reviewed_promotes_a_note_a_human_marked_approved(service: BrainService) -> None:
    result = _capture(service)
    _set_status(service, result.note_path, "approved")

    outcomes = service.promote_reviewed()

    assert len(outcomes) == 1 and isinstance(outcomes[0], ApproveResult)
    assert outcomes[0].note_path == "Notes/Design Review.md"
    assert not (service.vault / result.note_path).exists()
    assert read_note(service.vault / outcomes[0].note_path)[0]["status"] == "approved"
    assert service.promote_reviewed() == []


def test_promote_reviewed_returns_an_invalid_note_to_the_inbox_with_reasons(service: BrainService) -> None:
    result = _capture(service, tags=["unregistered-tag"])
    _set_status(service, result.note_path, "approved")

    outcomes = service.promote_reviewed()

    assert outcomes[0]["note_id"] == result.note_id and outcomes[0]["problems"]
    meta, _ = read_note(service.vault / result.note_path)
    assert meta["status"] == "inbox"
    assert any("not registered" in problem for problem in meta["review_problems"])
    assert not list((service.vault / "Notes").glob("*.md"))
    # Fixing the note clears the stale reasons and a second approval goes through.
    service.update_inbox(result.note_id, tags=["ui"])
    assert "review_problems" not in read_note(service.vault / result.note_path)[0]
    _set_status(service, result.note_path, "approved")
    assert isinstance(service.promote_reviewed()[0], ApproveResult)


def test_promote_reviewed_ignores_notes_still_in_inbox(service: BrainService) -> None:
    _capture(service)
    assert service.promote_reviewed() == []
    assert len(service.list_inbox()) == 1


def test_promote_reviewed_survives_unreadable_inbox_files(service: BrainService) -> None:
    (service.vault / "Inbox" / "broken.md").write_text("---\n: [unclosed\n---\n", encoding="utf-8")
    _capture(service)
    assert service.promote_reviewed() == []
    assert parse_note("---\na: 1\n---\nx")[0] == {"a": 1}
