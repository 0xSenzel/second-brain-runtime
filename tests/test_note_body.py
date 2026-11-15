from __future__ import annotations

from pathlib import Path

import pytest

from second_brain.models import Connection
from second_brain.notes import parse_note, parse_sections, render_note
from second_brain.service import BrainService


@pytest.fixture
def service(tmp_path: Path) -> BrainService:
    service = BrainService(tmp_path / "vault")
    service.register_tags(["ui"])
    return service


def _capture(service: BrainService, **overrides):
    payload = dict(
        content="Raw transcript: spoken words and slide text.",
        title="Review a design",
        summary="Use this prompt to review a design.",
        note_type="prompt",
        tags=["ui"],
        body="## Prompt\n\n逐项检查界面。\n\n## Example\n\nReview the checkout form.",
    )
    payload.update(overrides)
    return service.capture(**payload)


def test_the_full_body_is_kept_and_the_raw_source_stays_in_sources(service: BrainService) -> None:
    long_body = "## Prompt\n\n" + "保留完整原文。" * 400
    result = _capture(service, body=long_body)
    approved = service.approve(result.note_id, content_sha=result.content_sha)

    _, body = parse_note((service.vault / approved.note_path).read_text(encoding="utf-8"))
    assert long_body in body
    assert "Raw transcript:" not in body
    assert "Raw transcript:" in (service.vault / result.source_path).read_text(encoding="utf-8")
    item_body = parse_sections(body)[1]
    assert item_body == long_body


def test_nested_headings_in_the_body_survive_a_parse_round_trip(service: BrainService) -> None:
    result = _capture(service)
    item = service.get_inbox(result.note_id)

    assert item.body.startswith("## Prompt") and item.body.endswith("Review the checkout form.")
    assert item.summary == "Use this prompt to review a design."


def test_a_note_without_details_omits_the_section(service: BrainService) -> None:
    result = _capture(service, body="")
    text = (service.vault / result.note_path).read_text(encoding="utf-8")

    assert "## Details" not in text
    assert parse_sections(parse_note(text)[1])[1] == ""


def test_render_note_lists_every_source_url_and_creator() -> None:
    meta = {
        "title": "T", "sources": ["[[a-T]]", "[[b-T]]"],
        "source_url": "https://x.test", "source_creator": "Ann",
    }
    text = render_note(meta, summary="S.", body="B", connections=[Connection("about", "X", "why")])

    assert "- Original: [[a-T]]\n- Original: [[b-T]]\n- URL: https://x.test\n- Creator: Ann" in text
    assert "- about: [[X]] — why" in text
    summary, details, connections = parse_sections(parse_note(text)[1])
    assert (summary, details) == ("S.", "B")
    assert connections == [Connection("about", "X", "why")]


def test_a_summary_with_headings_is_a_soft_problem(service: BrainService) -> None:
    result = _capture(service, summary="## A misplaced document")

    assert any("summary" in problem for problem in result.problems)
