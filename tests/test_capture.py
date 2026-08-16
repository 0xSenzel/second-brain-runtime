from pathlib import Path

from second_brain.notes import read_note
from second_brain.service import BrainService


def test_prepare_is_pending_and_commit_requires_explicit_proposal(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    proposal = service.prepare_save(
        kind="prompt",
        content="Review a UI for hierarchy, spacing, and accessibility.",
        title="Frontend Design Critique",
        summary="A reusable prompt for structured interface critique.",
        tags=["ui", "design-review"],
        connections=[{"relation": "about", "target": "UI Design", "reason": "Primary topic"}],
    )

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
    proposal = service.prepare_save(kind="skill", content="# Skill\nDo useful work", title="Useful Skill")

    service.reject_save(proposal.id)

    assert not (vault / "Skills" / "Useful Skill.md").exists()
    assert (vault / "Reviews" / "Rejected" / f"{proposal.id}.md").exists()


def test_frontend_resources_are_routed_to_their_own_knowledge_folder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    proposal = service.prepare_save(
        kind="frontend-resource",
        content="A visual reference library for frontend design.",
        title="Interface Reference",
    )

    assert proposal.target_path == "Knowledge/Frontend Resources/Interface Reference.md"


def test_ai_coding_resources_are_routed_outside_knowledge(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    proposal = service.prepare_save(
        kind="ai-coding-resource",
        content="A tool for using coding agents with free or local model providers.",
        title="Free Coding Agent",
    )

    assert proposal.target_path == "Resources/AI Coding/Free Coding Agent.md"
    assert (vault / "Resources" / "AI Coding").is_dir()

    saved = service.commit_save(proposal.id)
    metadata, _ = read_note(vault / saved.note_path)

    assert metadata["type"] == "resource"


def test_duplicate_source_url_targets_a_reviewed_revision(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    first = service.prepare_save(
        kind="article",
        content="First version",
        title="Agent Memory",
        source_url="https://example.com/memory",
    )
    service.commit_save(first.id)

    revision = service.prepare_save(
        kind="article",
        content="Updated version",
        title="Agent Memory Updated",
        source_url="https://example.com/memory",
    )

    assert revision.target_path == "Knowledge/Agent Memory.md"
    assert revision.is_revision is True
