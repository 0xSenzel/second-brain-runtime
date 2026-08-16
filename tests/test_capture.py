from pathlib import Path

import pytest

from second_brain.models import ValidationError
from second_brain.notes import read_note
from second_brain.service import BrainService


def _prepare(service: BrainService, **overrides):
    """A proposal that satisfies every structural rule, so tests override one field at a time."""
    payload = dict(
        note_type="prompt",
        content="Review a UI for hierarchy, spacing, and accessibility.",
        title="Frontend Design Critique",
        summary="A reusable prompt for structured interface critique.",
        tags=["ui", "design-review"],
        aliases=["UI critique prompt", "Interface review prompt"],
        maps=["Frontend Design"],
        connections=[{"relation": "about", "target": "UI Design", "reason": "Primary topic"}],
    )
    payload.update(overrides)
    return service.prepare_save(**payload)


def test_prepare_is_pending_and_commit_writes_the_note(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    proposal = _prepare(service)

    assert proposal.status == "pending"
    assert not (vault / "Prompts" / "Frontend Design Critique.md").exists()
    assert (vault / "Reviews" / "Pending" / f"{proposal.id}.md").exists()

    saved = service.commit_save(proposal.id)

    assert saved.status == "approved"
    assert saved.note_path == "Prompts/Frontend Design Critique.md"
    assert (vault / saved.note_path).exists()
    assert (vault / saved.source_path).exists()
    assert not (vault / "Reviews" / "Pending" / f"{proposal.id}.md").exists()
    assert (vault / "Reviews" / "Approved" / f"{proposal.id}.md").exists()


def test_reject_never_writes_canonical_knowledge(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    proposal = _prepare(service, note_type="skill", title="Useful Skill")

    service.reject_save(proposal.id)

    assert not (vault / "Skills" / "Useful Skill.md").exists()
    assert (vault / "Reviews" / "Rejected" / f"{proposal.id}.md").exists()


# --- topic must never become a folder ------------------------------------------------


def test_topic_lives_in_tags_not_in_the_folder_path(tmp_path: Path) -> None:
    """The old CANONICAL_FOLDERS turned kind='frontend-resource' into a folder. It must not."""
    vault = tmp_path / "vault"
    service = BrainService(vault)

    proposal = _prepare(
        service,
        note_type="concept",
        title="Interface Reference",
        tags=["frontend", "design-reference"],
    )

    assert proposal.target_path == "Notes/Interface Reference.md"
    assert not (vault / "Knowledge").exists()
    assert not (vault / "Resources").exists()


def test_unknown_note_type_is_rejected_rather_than_silently_foldered(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    with pytest.raises(ValidationError, match="note_type"):
        _prepare(service, note_type="ai-coding-resource")


def test_every_note_type_maps_to_exactly_one_folder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    expected = {
        "concept": "Notes",
        "prompt": "Prompts",
        "skill": "Skills",
        "project": "Projects",
    }

    for note_type, folder in expected.items():
        proposal = _prepare(service, note_type=note_type, title=f"Item {note_type}")
        assert proposal.target_path == f"{folder}/Item {note_type}.md"


def test_sources_are_flat_and_id_prefixed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    proposal = _prepare(service, source_kind="tiktok-carousel")
    saved = service.commit_save(proposal.id)

    assert saved.source_path == f"Sources/{proposal.id}-Frontend Design Critique.md"
    assert (vault / saved.source_path).parent == vault / "Sources"
    assert [p for p in (vault / "Sources").iterdir() if p.is_dir()] == []


def test_source_kind_is_provenance_and_does_not_affect_placement(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    proposal = _prepare(service, note_type="concept", source_kind="reference-site", title="Refero")
    saved = service.commit_save(proposal.id)
    metadata, _ = read_note(vault / saved.note_path)

    assert saved.note_path == "Notes/Refero.md"
    assert metadata["type"] == "concept"
    assert metadata["source_kind"] == "reference-site"


# --- the validation gate -------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"tags": ["ui"]}, "at least 2 tags"),
        ({"tags": []}, "at least 2 tags"),
        ({"maps": []}, "at least 1 map"),
        ({"aliases": ["only one"]}, "at least 2 aliases"),
        ({"summary": ""}, "summary"),
    ],
)
def test_commit_is_blocked_when_the_schema_is_incomplete(
    tmp_path: Path, overrides: dict, expected: str
) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    proposal = _prepare(service, **overrides)

    with pytest.raises(ValidationError, match=expected):
        service.commit_save(proposal.id)

    assert not (vault / proposal.target_path).exists()
    assert (vault / "Reviews" / "Pending" / f"{proposal.id}.md").exists()


def test_a_blocked_commit_leaves_no_partial_write(tmp_path: Path) -> None:
    """Source and note are written together or not at all."""
    vault = tmp_path / "vault"
    service = BrainService(vault)
    proposal = _prepare(service, maps=[])

    with pytest.raises(ValidationError):
        service.commit_save(proposal.id)

    assert list((vault / "Sources").glob("*.md")) == []


def test_destination_may_be_chosen_but_must_match_the_type(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    ok = _prepare(service, note_type="project", destination="Projects/Vault Rebuild/Overview.md")
    assert ok.target_path == "Projects/Vault Rebuild/Overview.md"

    with pytest.raises(ValidationError, match="destination"):
        _prepare(service, note_type="prompt", destination="Notes/Wrong Place.md")


def test_destination_cannot_escape_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    with pytest.raises(ValidationError, match="destination"):
        _prepare(service, note_type="concept", destination="../../outside.md")


def test_new_tags_must_be_registered(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    service.register_tags(["ui", "design-review"])

    proposal = _prepare(service, tags=["ui", "totally-new-tag"])

    with pytest.raises(ValidationError, match="not registered"):
        service.commit_save(proposal.id)

    service.register_tags(["totally-new-tag"])
    saved = service.commit_save(proposal.id)
    assert (vault / saved.note_path).exists()


# --- maps are the retrieval layer ----------------------------------------------------


def test_commit_appends_the_note_to_its_map(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    proposal = _prepare(service, maps=["Frontend Design"])

    service.commit_save(proposal.id)

    map_text = (vault / "Maps" / "Frontend Design.md").read_text(encoding="utf-8")
    assert "[[Frontend Design Critique]]" in map_text
    assert "A reusable prompt for structured interface critique." in map_text


def test_map_append_is_idempotent_across_revisions(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    first = _prepare(service, source_url="https://example.com/critique")
    service.commit_save(first.id)
    second = _prepare(service, source_url="https://example.com/critique")
    service.commit_save(second.id)

    map_text = (vault / "Maps" / "Frontend Design.md").read_text(encoding="utf-8")
    assert map_text.count("[[Frontend Design Critique]]") == 1


def test_maps_are_indexable_notes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    service.commit_save(_prepare(service).id)

    metadata, _ = read_note(vault / "Maps" / "Frontend Design.md")

    assert metadata["type"] == "map"
    assert metadata["status"] == "approved"


# --- revisions -----------------------------------------------------------------------


def test_duplicate_source_url_targets_a_reviewed_revision(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    first = _prepare(
        service,
        note_type="concept",
        title="Agent Memory",
        source_url="https://example.com/memory",
    )
    service.commit_save(first.id)

    revision = _prepare(
        service,
        note_type="concept",
        title="Agent Memory Updated",
        source_url="https://example.com/memory",
    )

    assert revision.target_path == "Notes/Agent Memory.md"
    assert revision.is_revision is True


def test_revision_is_found_in_the_new_folders(tmp_path: Path) -> None:
    """_find_by_source_url used a hardcoded folder list that omitted Notes/."""
    vault = tmp_path / "vault"
    service = BrainService(vault)
    first = _prepare(service, note_type="concept", title="Local RAG", source_url="https://example.com/rag")
    service.commit_save(first.id)

    revision = _prepare(service, note_type="concept", title="Local RAG v2", source_url="https://example.com/rag")

    assert revision.is_revision is True
    assert revision.target_path == "Notes/Local RAG.md"


def test_revision_preserves_the_original_note_id(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    first = _prepare(service, note_type="concept", title="Chunking", source_url="https://example.com/chunk")
    saved = service.commit_save(first.id)
    original_id = read_note(vault / saved.note_path)[0]["id"]

    revision = _prepare(service, note_type="concept", title="Chunking", source_url="https://example.com/chunk")
    service.commit_save(revision.id)

    assert read_note(vault / saved.note_path)[0]["id"] == original_id


# --- layout ---------------------------------------------------------------------------


def test_layout_does_not_recreate_retired_folders(tmp_path: Path) -> None:
    """_ensure_layout used to resurrect Knowledge/, Resources/ and Inbox/ on every start."""
    vault = tmp_path / "vault"
    BrainService(vault)
    BrainService(vault)

    for retired in ("Inbox", "Knowledge", "Resources"):
        assert not (vault / retired).exists()
    for expected in ("Notes", "Maps", "Prompts", "Skills", "Projects", "Sources", "Templates"):
        assert (vault / expected).is_dir()


def test_layout_keeps_a_retired_folder_that_still_holds_files(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    stray = vault / "Knowledge" / "Unmigrated.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("keep me", encoding="utf-8")

    BrainService(vault)

    assert stray.exists()
