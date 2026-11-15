from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from second_brain.models import ValidationError
from second_brain.notes import read_note
from second_brain.service import BrainService


def _capture(service: BrainService, **overrides):
    payload = dict(
        content="Review a UI for hierarchy, spacing, and accessibility.",
        title="Frontend Design Critique",
        summary="A reusable prompt for structured interface critique.",
        note_type="prompt",
        tags=["ui", "design-review"],
    )
    payload.update(overrides)
    return service.capture(**payload)


def test_capture_writes_inbox_note_and_source_at_once(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    result = _capture(service, source_url="https://example.com/a", source_kind="article")

    assert result.note_path == "Inbox/Frontend Design Critique.md"
    assert result.note_id.startswith("note-") and len(result.note_id) == len("note-") + 12
    assert result.source_path.startswith("Sources/")
    assert result.source_path.endswith("-Frontend Design Critique.md")
    meta, _ = read_note(vault / result.note_path)
    assert meta["status"] == "inbox" and meta["id"] == result.note_id
    assert meta["sources"] == [f"[[{Path(result.source_path).stem}]]"]
    assert meta["source_url"] == "https://example.com/a"
    assert "reviewed" not in meta and "revises" not in meta and "aliases" not in meta
    source_meta, source_body = read_note(vault / result.source_path)
    assert source_meta["note_id"] == result.note_id
    assert source_meta["id"] == f"source-{Path(result.source_path).stem.split('-')[0]}"
    assert source_meta["distilled_note"] == "[[Frontend Design Critique]]"
    assert "Review a UI" in source_body
    assert not list((vault / "Notes").glob("*.md"))


def test_capture_reports_soft_problems_instead_of_failing(tmp_path: Path) -> None:
    service = BrainService(tmp_path / "vault")  # no registered tags

    result = _capture(service, summary="", tags=[])

    assert any("summary" in problem for problem in result.problems)
    assert any("tag" in problem for problem in result.problems)
    assert (service.vault / result.note_path).exists()


@pytest.mark.parametrize(
    "overrides",
    [{"note_type": "bogus"}, {"domain": "mars"}, {"content": "  "}, {"title": " "}],
)
def test_capture_hard_fails_only_on_unusable_input(tmp_path: Path, overrides: dict) -> None:
    service = BrainService(tmp_path / "vault")
    with pytest.raises(ValidationError):
        _capture(service, **overrides)
    assert not list((service.vault / "Inbox").glob("*.md"))


def test_title_clashes_in_the_inbox_get_a_suffix(tmp_path: Path) -> None:
    service = BrainService(tmp_path / "vault")
    first = _capture(service)
    second = _capture(service)

    assert first.note_path != second.note_path
    assert second.note_path == "Inbox/Frontend Design Critique (2).md"


def test_recapturing_the_same_revision_updates_the_inbox_item_in_place(tmp_path: Path) -> None:
    service = BrainService(tmp_path / "vault")
    first = _capture(service, source_url="https://example.com/a", revises="note-aaaaaaaaaaaa")
    second = _capture(
        service, source_url="https://example.com/a", revises="note-aaaaaaaaaaaa",
        content="Newer content.", summary="A newer summary.",
    )

    assert second.updated_existing and second.note_id == first.note_id
    assert second.note_path == first.note_path and second.source_path == first.source_path
    assert len(service.list_inbox()) == 1
    assert "Newer content." in (service.vault / second.source_path).read_text()
    assert service.get_inbox(first.note_id).summary == "A newer summary."


def test_independent_notes_from_one_url_stay_separate(tmp_path: Path) -> None:
    service = BrainService(tmp_path / "vault")
    _capture(service, source_url="https://example.com/video", title="Method one")
    _capture(service, source_url="https://example.com/video", title="Method two")

    assert len(service.list_inbox()) == 2


def test_connections_are_rendered_and_round_trip(tmp_path: Path) -> None:
    service = BrainService(tmp_path / "vault")
    result = _capture(
        service, connections=[{"relation": "about", "target": "UI Design", "reason": "Primary topic"}]
    )

    text = (service.vault / result.note_path).read_text()
    assert "- about: [[UI Design]] — Primary topic" in text
    service.update_inbox(result.note_id, summary="Changed summary.")
    assert "- about: [[UI Design]] — Primary topic" in (service.vault / result.note_path).read_text()


def test_every_write_is_journaled_in_git_without_unrelated_files(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=vault, check=True)
    service = BrainService(vault)
    (vault / "scratch.md").write_text("user draft")

    result = _capture(service)

    log = subprocess.run(["git", "log", "--format=%s"], cwd=vault, capture_output=True, text=True).stdout
    assert "Capture Frontend Design Critique" in log
    shown = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"], cwd=vault, capture_output=True, text=True
    ).stdout.split("\n")
    assert result.note_path in shown and result.source_path in shown
    assert "scratch.md" not in shown


def test_approval_journals_the_removal_of_the_inbox_file(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
    service = BrainService(vault)
    service.register_tags(["ui", "design-review"])
    result = _capture(service)

    service.approve(result.note_id, content_sha=result.content_sha)

    shown = subprocess.run(
        ["git", "show", "--name-status", "--no-renames", "--format=", "HEAD"],
        cwd=vault, capture_output=True, text=True,
    ).stdout
    assert "D\tInbox/Frontend Design Critique.md" in shown
    assert "A\tNotes/Frontend Design Critique.md" in shown
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=vault, capture_output=True, text=True
    ).stdout
    assert "Inbox" not in status and "Notes" not in status and "Sources" not in status
