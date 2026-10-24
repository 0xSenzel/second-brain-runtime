from pathlib import Path

import pytest

from second_brain.api import BrainAPI


class NoCallEmbedder:
    model = "test"

    def embed(self, text: str) -> list[float]:
        raise AssertionError("brain_find should not call the embedding service")


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

    assert saved["note_path"] == "Tech/Prompts/Accessibility Review.md"
    assert answer["sources"]
    assert answer["sources"][0]["obsidian_uri"].startswith("obsidian://open?")


def test_open_returns_uri_without_launching_an_application(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "My Brain", use_embeddings=False)

    result = api.open_note("Prompts/Example.md")

    assert "vault=My%20Brain" in result["obsidian_uri"]
    assert "file=Prompts%2FExample.md" in result["obsidian_uri"]


def test_find_stays_on_the_lexical_path_when_embeddings_are_configured(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    proposal = api.prepare_save(
        content_or_url="Review interfaces for keyboard navigation.",
        note_type="concept",
        title="Keyboard Navigation Review",
        summary="A review method for keyboard access.",
        body="## Details\n\nCheck tab order and visible focus.",
        tags=["accessibility", "design-review"],
        aliases=["keyboard access review", "tab order review"],
        maps=["Frontend Design"],
    )
    api.commit_save(proposal["proposal_id"], approved=True)
    embedder = NoCallEmbedder()
    api.embedder = api.index.embedder = embedder

    result = api.find("Keyboard Navigation Review")

    assert result["results"][0]["title"] == "Keyboard Navigation Review"


def test_api_construction_does_not_probe_the_embedding_service(monkeypatch, tmp_path: Path) -> None:
    from second_brain import api as api_module

    def fail_if_probed(self):
        raise AssertionError("API construction must not wait on the optional model service")

    monkeypatch.setattr(api_module.OllamaEmbedder, "available", fail_if_probed)

    api = BrainAPI(tmp_path / "vault")

    assert api.embedder is not None
    assert api.index.embedder is api.embedder
