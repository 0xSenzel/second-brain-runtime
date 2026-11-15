from __future__ import annotations

from pathlib import Path

import pytest

from second_brain.models import MAX_SUMMARY
from second_brain.notes import dump_note, render_note
from second_brain.service import BrainService
from second_brain.validation import check_for_approval

REGISTERED = {"ui", "data"}


def _meta(**overrides):
    meta = {"type": "concept", "domain": "tech", "tags": ["ui"], "summary": "A summary.", "revises": ""}
    meta.update(overrides)
    return meta


def _check(tmp_path: Path, **overrides) -> list[str]:
    return check_for_approval(_meta(**overrides), registered_tags=REGISTERED, vault=tmp_path)


def test_a_conforming_note_has_no_problems(tmp_path: Path) -> None:
    assert _check(tmp_path) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"summary": ""}, "summary cannot be empty"),
        ({"summary": "## Heading\n\ntext"}, "Markdown headings"),
        ({"summary": "x" * (MAX_SUMMARY + 1)}, "over the"),
        ({"tags": []}, "at least"),
        ({"tags": ["a", "b", "c", "d", "e", "f"]}, "at most"),
        ({"tags": ["Not Kebab"]}, "kebab-case"),
        ({"tags": ["unknown-tag"]}, "not registered"),
        ({"type": "bogus"}, "type"),
        ({"domain": "mars"}, "domain"),
        ({"revises": "note-000000000000"}, "no approved note"),
    ],
)
def test_each_blocking_rule(tmp_path: Path, overrides: dict, fragment: str) -> None:
    problems = _check(tmp_path, **overrides)
    assert any(fragment in problem for problem in problems), problems


def test_aliases_and_maps_are_not_required(tmp_path: Path) -> None:
    assert _check(tmp_path) == []  # no aliases, no maps key at all


def test_lint_covers_notes_and_reports_inbox_and_orphan_sources_separately(tmp_path: Path) -> None:
    service = BrainService(tmp_path / "vault")
    service.register_tags(["ui"])
    vault = service.vault
    good = {"id": "note-aaaaaaaaaaaa", "status": "approved", "type": "concept", "domain": "tech",
            "title": "Good", "tags": ["ui"], "sources": []}
    (vault / "Notes" / "Good.md").write_text(
        render_note(good, summary="Fine.", body="", connections=[]), encoding="utf-8"
    )
    (vault / "Notes" / "Hand Written.md").write_text(
        dump_note({"id": "note-bbbbbbbbbbbb", "type": "concept", "domain": "tech",
                   "status": "approved", "tags": []}, "# Hand Written\n\nNo sections.\n"),
        encoding="utf-8",
    )
    service.capture(content="c", title="Draft", summary="", tags=[])
    (vault / "Sources" / "deadbeef0000-Lonely.md").write_text("---\nid: source-deadbeef0000\n---\n")

    report = service.validate()

    assert report["checked_notes"] == 2 and report["conforming"] == 1
    assert [f["path"] for f in report["findings"]] == ["Notes/Hand Written.md"]
    assert report["inbox"]["checked"] == 1
    assert report["inbox"]["findings"][0]["path"] == "Inbox/Draft.md"
    assert not any("status is" in p for p in report["inbox"]["findings"][0]["problems"])
    assert "Sources/deadbeef0000-Lonely.md" in report["orphan_sources"]
    assert len(report["orphan_sources"]) == 1  # the Draft's source is referenced by its inbox note


def test_lint_flags_a_note_whose_status_is_not_approved(tmp_path: Path) -> None:
    service = BrainService(tmp_path / "vault")
    service.register_tags(["ui"])
    meta = {"id": "note-aaaaaaaaaaaa", "status": "inbox", "type": "concept", "domain": "tech",
            "title": "Stuck", "tags": ["ui"], "sources": []}
    (service.vault / "Notes" / "Stuck.md").write_text(
        render_note(meta, summary="Fine.", body="", connections=[]), encoding="utf-8"
    )

    problems = service.validate()["findings"][0]["problems"]

    assert any("status is" in problem for problem in problems)
