from pathlib import Path

import pytest

from second_brain.api import BrainAPI


def test_api_requires_approval_before_commit_and_returns_clickable_source(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    proposal = api.prepare_save(
        content_or_url="Review interfaces for accessibility.",
        note_type="prompt",
        title="Accessibility Review",
        summary="A prompt for accessibility reviews.",
        tags=["accessibility", "design-review"],
        aliases=["A11y review prompt", "Accessibility audit prompt"],
        maps=["Frontend Design"],
    )

    with pytest.raises(PermissionError):
        api.commit_save(proposal["proposal_id"], approved=False)

    saved = api.commit_save(proposal["proposal_id"], approved=True)
    answer = api.ask("accessibility", max_tokens=150)

    assert saved["note_path"] == "Prompts/Accessibility Review.md"
    assert answer["sources"]
    assert answer["sources"][0]["obsidian_uri"].startswith("obsidian://open?")


def test_open_returns_uri_without_launching_an_application(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "My Brain", use_embeddings=False)

    result = api.open_note("Prompts/Example.md")

    assert "vault=My%20Brain" in result["obsidian_uri"]
    assert "file=Prompts%2FExample.md" in result["obsidian_uri"]
