import json

import pytest

from second_brain.api import BrainAPI
from second_brain.models import Proposal
from second_brain.notes import read_note


def prepare(api, **overrides):
    args = dict(
        content_or_url="Raw transcript: spoken words and slide text.",
        note_type="prompt", title="Review a design", summary="Use this prompt to review a design.",
        tags=["ui", "design-review"], aliases=["Design critique", "界面评审"], maps=["Design"],
        body="## Prompt\n\n逐项检查界面。\n\n## Example\n\nReview the checkout form.",
    )
    args.update(overrides)
    return api.prepare_save(**args)


def test_review_and_canonical_note_contain_the_full_body(tmp_path):
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    result = prepare(api, body="## Prompt\n\n" + "保留完整原文。" * 400)
    assert result["body"].endswith("保留完整原文。" * 400)
    assert result["commit_would_succeed"]
    review = (api.vault / result["review_note"]).read_text()
    assert result["body"] in review
    assert not (api.vault / result["destination"]).exists()
    with pytest.raises(PermissionError):
        api.commit_save(result["proposal_id"], approved=False)
    saved = api.commit_save(result["proposal_id"], approved=True)
    metadata, body = read_note(api.vault / saved["note_path"])
    assert result["body"] in body
    assert "Raw transcript:" not in body
    assert "Raw transcript:" in (api.vault / saved["source_path"]).read_text()
    assert api.find("保留完整原文")["results"]
    assert not api.find("spoken words and slide text")["results"]
    read_result = api.read(metadata["id"])
    assert read_result["body"].startswith("# Review a design")
    assert "保留完整原文。" in read_result["body"]
    assert not read_result["truncated"]
    assert api.validate()["findings"] == []


def test_revision_omitting_body_preserves_existing_reusable_content(tmp_path):
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    first = prepare(api)
    api.commit_save(first["proposal_id"], approved=True)
    revision = prepare(api, body=None, summary="A revised description of this design prompt.")
    assert revision["is_revision"]
    assert revision["body"] == first["body"]
    saved = api.commit_save(revision["proposal_id"], approved=True)
    assert first["body"] in (api.vault / saved["note_path"]).read_text()


def test_explicit_body_replacement_is_reviewed(tmp_path):
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    first = prepare(api)
    api.commit_save(first["proposal_id"], approved=True)
    revision = prepare(api, body="## Prompt\n\nThe revised prompt.")
    assert revision["body"] == "## Prompt\n\nThe revised prompt."
    saved = api.commit_save(revision["proposal_id"], approved=True)
    content = (api.vault / saved["note_path"]).read_text()
    assert "The revised prompt." in content
    assert "逐项检查界面。" not in content


def test_old_pending_proposal_without_body_still_loads(tmp_path):
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    result = prepare(api)
    data = api.service.get_proposal(result["proposal_id"]).to_dict()
    data.pop("body", None)
    restored = Proposal.from_dict(json.loads(json.dumps(data)))
    assert restored.body == ""


def test_summary_validation_stays_separate_from_body(tmp_path):
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    result = prepare(api, summary="## A misplaced document")
    assert not result["commit_would_succeed"]
    assert any("summary" in error for error in result["blocking_problems"])


def test_independent_notes_from_one_source_keep_distinct_proposals(tmp_path):
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    first = prepare(api, source_url="https://example.com/video/123", title="Method one",
                    destination="Tech/Prompts/Method one.md")
    second = prepare(api, source_url="https://example.com/video/123", title="Method two",
                     destination="Tech/Prompts/Method two.md", body="## Method\n\nA second method.")
    assert {first["proposal_id"], second["proposal_id"]} <= {
        item["proposal_id"] for item in api.pending()
    }
    api.commit_save(first["proposal_id"], approved=True)
    # A third method with an explicit destination must not revise the first one.
    third = prepare(api, source_url="https://example.com/video/123", title="Method three",
                    destination="Tech/Prompts/Method three.md")
    assert third["destination"] == "Tech/Prompts/Method three.md"
    assert not third["is_revision"]
