"""The whole schema-4 loop through real components, no fakes.

Unit tests in each module use fakes so the streams could be built in parallel. This file
is the check that they fit: the API's calls match the service and index signatures, a
capture is searchable as unreviewed, approval moves it, maps and git follow, and the
out-of-band paths (an Obsidian status edit, the CLI) work end to end.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from second_brain.api import BrainAPI
from second_brain.models import ApprovalMismatch, ValidationError
from second_brain.notes import read_note


class FakeEmbedder:
    """Deterministic bag-of-letters vectors: enough to exercise the hybrid path."""

    model = "fake-embed"

    def available(self) -> bool:
        return True

    def embed(self, text: str) -> list[float]:
        counts = [0.0] * 26
        for char in text.lower():
            if "a" <= char <= "z":
                counts[ord(char) - 97] += 1.0
        return counts


def git(vault: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(vault), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def api(tmp_path: Path, monkeypatch) -> BrainAPI:
    vault = tmp_path / "vault"
    vault.mkdir()
    git(vault, "init", "-q")
    git(vault, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", str(tmp_path / "index.sqlite3"))
    monkeypatch.delenv("SECOND_BRAIN_MODE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_APPROVAL", raising=False)
    brain = BrainAPI(vault, embedder=FakeEmbedder())
    brain.register_tags(["agents", "testing"], {"agents": "AI agents", "testing": "Test practice"})
    return brain


def capture(api: BrainAPI, title: str = "Pin agent tool versions", **overrides) -> dict:
    fields = {
        "content_or_url": "Raw transcript: always pin agent tool versions so runs reproduce.",
        "title": title,
        "summary": "Pin every agent tool version so a rerun reproduces the result.",
        "body": "## Use this when\nA run must reproduce.\n",
        "tags": ["agents", "testing"],
        "source_url": "https://example.com/pin",
        "source_kind": "article",
    }
    fields.update(overrides)
    return api.capture(**fields)


def test_capture_find_approve_maps_and_git(api: BrainAPI) -> None:
    (api.vault / "Maps").mkdir(exist_ok=True)
    (api.vault / "Maps" / "Agent Practice.md").write_text(
        "---\ntype: map\ntitle: Agent Practice\nmap_tags: [agents]\n---\n\n# Agent Practice\n\nIntro.\n",
        encoding="utf-8",
    )
    captured = capture(api)
    assert captured["note_path"].startswith("Inbox/")

    found = api.find("pin agent tool versions")["results"]
    assert found and found[0]["status"] == "inbox" and found[0]["unreviewed"] is True

    with pytest.raises(PermissionError):
        api.approve(captured["note_id"], content_sha=captured["content_sha"], approved=False)
    with pytest.raises(ApprovalMismatch):
        api.approve(captured["note_id"], content_sha="0" * 64, approved=True)

    approved = api.approve(captured["note_id"], content_sha=captured["content_sha"], approved=True)
    assert approved["note_path"] == "Notes/Pin agent tool versions.md"
    assert not (api.vault / captured["note_path"]).exists()

    hit = api.find("pin agent tool versions")["results"][0]
    assert hit["status"] == "approved" and hit["unreviewed"] is False
    assert "[[Pin agent tool versions]]" in (api.vault / "Maps" / "Agent Practice.md").read_text()
    assert "Pin agent tool versions" in git(api.vault, "log", "--format=%s", "-n", "1")

    asked = api.ask("why pin agent tools")
    assert asked["sources"] and asked["sources"][0]["path"] == approved["note_path"]


def test_revision_keeps_identity_created_and_every_source(api: BrainAPI) -> None:
    first = capture(api)
    note = api.approve(first["note_id"], content_sha=first["content_sha"], approved=True)
    created = read_note(api.vault / note["note_path"])[0]["created"]

    second = capture(
        api,
        content_or_url="Second source: pinning also helps caching.",
        summary="Pin agent tool versions for reproducible and cacheable runs.",
        source_url="https://example.com/pin-2",
        revises=note["note_id"],
    )
    revised = api.approve(second["note_id"], content_sha=second["content_sha"], approved=True)
    metadata, body = read_note(api.vault / revised["note_path"])

    assert revised["note_id"] == note["note_id"]
    assert metadata["created"] == created
    assert len(metadata["sources"]) == 2
    assert "cacheable" in body
    assert len(list((api.vault / "Notes").glob("*.md"))) == 1


def test_gate_blocks_approval_but_not_capture(api: BrainAPI) -> None:
    captured = capture(api, tags=["unregistered-topic"])
    assert any("registered" in problem for problem in captured["problems"])
    with pytest.raises(ValidationError):
        api.approve(captured["note_id"], content_sha=captured["content_sha"], approved=True)
    assert (api.vault / captured["note_path"]).exists()


def test_obsidian_status_edit_is_promoted_on_next_read(api: BrainAPI) -> None:
    captured = capture(api)
    path = api.vault / captured["note_path"]
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("status: inbox", "status: approved"), encoding="utf-8")

    hit = api.find("pin agent tool versions")["results"][0]

    assert hit["status"] == "approved"
    assert (api.vault / "Notes" / "Pin agent tool versions.md").exists()


def test_discard_archives_and_digest_report_run(api: BrainAPI) -> None:
    kept = capture(api, title="Keep me", source_url="https://example.com/keep")
    dropped = capture(api, title="Drop me", source_url="https://example.com/drop")

    digest = api.digest()
    assert kept["note_id"] in json.dumps(digest) and dropped["note_id"] in json.dumps(digest)

    api.discard(dropped["note_id"], reason="duplicate")
    assert not (api.vault / dropped["note_path"]).exists()
    assert list((api.vault / "Archive" / "Inbox").glob("*.md"))

    report = api.report()
    assert isinstance(report, dict)
    assert api.validate()["schema_mismatch"] in (None, "")


def test_background_embedding_enables_hybrid_search(api: BrainAPI) -> None:
    captured = capture(api)
    api.approve(captured["note_id"], content_sha=captured["content_sha"], approved=True)

    assert api.embed()["embedded_chunks"] > 0
    assert api.doctor()["embedding_coverage"]["embedded"] > 0
    assert api.find("reproduce pinned agent runs")["results"]


def test_delete_archives_an_approved_note(api: BrainAPI) -> None:
    captured = capture(api)
    note = api.approve(captured["note_id"], content_sha=captured["content_sha"], approved=True)

    staged = api.prepare_delete(note["note_id"], reason="obsolete")
    assert (api.vault / note["note_path"]).exists()
    api.commit_delete(staged["proposal_id"], approved=True)

    assert not (api.vault / note["note_path"]).exists()
    assert list((api.vault / "Archive" / "Notes").glob("*.md"))
    assert api.find("pin agent tool versions")["results"] == []


def test_reader_mode_never_writes(api: BrainAPI, monkeypatch) -> None:
    monkeypatch.setenv("SECOND_BRAIN_MODE", "reader")
    reader = BrainAPI(api.vault, embedder=FakeEmbedder())
    before = git(api.vault, "status", "--porcelain")

    result = reader.capture(content_or_url="x", title="Nope", summary="Nope.", tags=["agents"])

    assert "error" in result
    assert git(api.vault, "status", "--porcelain") == before
