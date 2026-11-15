from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from second_brain.deletion import DeleteWorkflow
from second_brain.models import ValidationError
from second_brain.notes import dump_note, read_note
from second_brain.service import BrainService


@pytest.fixture
def service(tmp_path: Path) -> BrainService:
    service = BrainService(tmp_path / "vault")
    service.register_tags(["delete-test", "other"])
    (service.vault / "Maps" / "Delete Map.md").write_text(
        dump_note(
            {"type": "map", "title": "Delete Map", "map_tags": ["delete-test"]},
            "# Delete Map\n\nMine.\n",
        ),
        encoding="utf-8",
    )
    return service


def _approved(service: BrainService, title: str = "Delete Me", tags: tuple[str, ...] = ("delete-test",)):
    captured = service.capture(
        content="Distinctive source for delete testing", title=title,
        summary="A note used to test recoverable deletion.", body="Body.", tags=tags,
    )
    return captured, service.approve(captured.note_id, content_sha=captured.content_sha)


def test_prepare_changes_nothing_and_lists_exact_effects(service: BrainService) -> None:
    captured, approved = _approved(service)
    workflow = DeleteWorkflow(service.vault)

    proposal = workflow.prepare(captured.note_id, reason="obsolete")

    assert proposal["note_path"] == approved.note_path
    assert proposal["source_paths"] == [captured.source_path]
    assert proposal["affected_maps"] == ["Maps/Delete Map.md"]
    assert proposal["map_changes"][0]["removed_entries"] == [
        "- [[Delete Me]] — A note used to test recoverable deletion."
    ]
    assert proposal["archive_paths"] == ["Archive/Notes/Delete Me.md", f"Archive/{captured.source_path}"]
    assert proposal["approval_required"] and "Distinctive source" in proposal["review"]
    assert (service.vault / approved.note_path).exists() and (service.vault / captured.source_path).exists()
    assert proposal["review_path"].startswith(".second-brain/delete-proposals/")


def test_commit_archives_note_source_and_regenerates_maps(service: BrainService) -> None:
    captured, approved = _approved(service)
    _approved(service, title="Keep Me")
    workflow = DeleteWorkflow(service.vault)
    proposal = workflow.prepare(captured.note_id, reason="obsolete")

    result = workflow.commit(proposal["proposal_id"])

    assert not (service.vault / approved.note_path).exists()
    assert not (service.vault / captured.source_path).exists()
    assert (service.vault / "Archive/Notes/Delete Me.md").is_file()
    assert (service.vault / f"Archive/{captured.source_path}").is_file()
    receipt = json.loads((service.vault / result["receipt_path"]).read_text())
    assert receipt["note_id"] == captured.note_id and receipt["reason"] == "obsolete"
    assert result["receipt_path"].startswith("Archive/Receipts/")
    map_text = (service.vault / "Maps" / "Delete Map.md").read_text()
    assert "Delete Me" not in map_text and "[[Keep Me]]" in map_text and "Mine." in map_text
    with pytest.raises(ValidationError):
        workflow.commit(proposal["proposal_id"])


def test_a_changed_note_keeps_the_proposal_pending(service: BrainService) -> None:
    captured, approved = _approved(service)
    workflow = DeleteWorkflow(service.vault)
    proposal = workflow.prepare(captured.note_id)
    note = service.vault / approved.note_path
    note.write_text(note.read_text() + "\nedited\n")

    with pytest.raises(ValidationError, match="changed since staging"):
        workflow.commit(proposal["proposal_id"])

    assert note.exists() and not (service.vault / "Archive/Notes/Delete Me.md").exists()


def test_a_changed_source_keeps_the_proposal_pending(service: BrainService) -> None:
    captured, _ = _approved(service)
    workflow = DeleteWorkflow(service.vault)
    proposal = workflow.prepare(captured.note_id)
    source = service.vault / captured.source_path
    source.write_text(source.read_text() + "\nedited\n")

    with pytest.raises(ValidationError, match="changed since staging"):
        workflow.commit(proposal["proposal_id"])

    assert source.exists()


def test_a_source_shared_with_another_note_stays_in_place(service: BrainService) -> None:
    captured, approved = _approved(service)
    note_id = read_note(service.vault / approved.note_path)[0]["id"]
    revision = service.capture(
        content="More.", title="Delete Me v2", summary="Revised.", tags=["delete-test"], revises=note_id
    )
    service.approve(revision.note_id, content_sha=revision.content_sha)
    # A second note lists the first source too.
    other, other_approved = _approved(service, title="Other Note")
    path = service.vault / other_approved.note_path
    meta, body = read_note(path)
    meta["sources"] = [*meta["sources"], f"[[{Path(captured.source_path).stem}]]"]
    path.write_text(dump_note(meta, body), encoding="utf-8")
    workflow = DeleteWorkflow(service.vault)

    proposal = workflow.prepare(note_id)
    result = workflow.commit(proposal["proposal_id"])

    assert proposal["kept_sources"] == [Path(captured.source_path).stem]
    assert (service.vault / captured.source_path).exists()
    assert not (service.vault / revision.source_path).exists()
    assert len(result["archived"]) == 2


def test_inbox_notes_maps_and_unknown_ids_are_not_deletable(service: BrainService) -> None:
    pending = service.capture(content="c", title="Pending", summary="S.", tags=["delete-test"])
    workflow = DeleteWorkflow(service.vault)

    with pytest.raises(ValidationError, match="Unknown canonical note ID"):
        workflow.prepare(pending.note_id)
    with pytest.raises(ValidationError, match="Map notes"):
        workflow.prepare("map-tag-registry")
    with pytest.raises(ValidationError):
        workflow.prepare("")


def test_delete_is_journaled_in_git(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=vault, check=True)
    service = BrainService(vault)
    service.register_tags(["delete-test"])
    captured, _ = _approved(service)
    workflow = DeleteWorkflow(vault)

    result = workflow.commit(workflow.prepare(captured.note_id)["proposal_id"])

    assert result["warnings"] == []
    shown = subprocess.run(
        ["git", "show", "--name-status", "--no-renames", "--format=%s", "HEAD"],
        cwd=vault, capture_output=True, text=True,
    ).stdout
    assert "Archive Delete Me" in shown
    assert "D\tNotes/Delete Me.md" in shown and "A\tArchive/Notes/Delete Me.md" in shown
