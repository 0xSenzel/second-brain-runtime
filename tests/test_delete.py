from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from second_brain.api import BrainAPI
from second_brain.models import ValidationError
from second_brain.notes import dump_note, read_note


def _approved_note(
    api: BrainAPI,
    *,
    title: str = "Delete Me",
    maps: tuple[str, ...] = ("Delete Map",),
) -> str:
    proposal = api.prepare_save(
        content_or_url="Distinctive body for delete testing",
        note_type="concept",
        domain="tech",
        title=title,
        summary="A note used to test recoverable deletion.",
        tags=["delete-test", "recovery"],
        aliases=["Delete target", "Recovery target"],
        maps=list(maps),
    )
    saved = api.commit_save(proposal["proposal_id"], approved=True)
    return saved["note_path"]


def _stage(api: BrainAPI, note_path: str, *, reason: str = "No longer useful") -> dict:
    metadata, _ = read_note(api.vault / note_path)
    return api.prepare_delete(str(metadata["id"]), reason=reason)


def test_delete_tools_are_discoverable_over_stdio(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = dict(os.environ)
        env["SECOND_BRAIN_VAULT"] = str(tmp_path / "vault")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "second_brain.mcp_server"],
            env=env,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"brain_prepare_delete", "brain_commit_delete"} <= names

    anyio.run(scenario)


def test_prepare_delete_stages_exact_review_without_mutating_canonical_files(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    note_path = _approved_note(api)
    source_path = next((api.vault / "Sources").glob("*.md"))
    map_path = api.vault / "Maps" / "Delete Map.md"
    before = {path: path.read_bytes() for path in (api.vault / note_path, source_path, map_path)}

    proposal = _stage(api, note_path, reason="Superseded by a verified note")

    assert proposal["status"] == "pending"
    assert proposal["approval_required"] is True
    assert proposal["note_path"] == note_path
    assert proposal["source_path"] == source_path.relative_to(api.vault).as_posix()
    assert proposal["affected_maps"] == ["Maps/Delete Map.md"]
    assert proposal["reason"] == "Superseded by a verified note"
    assert {path: path.read_bytes() for path in before} == before
    review = (api.vault / "Reviews" / "Pending" / f"{proposal['proposal_id']}.md").read_text()
    assert note_path in review
    assert source_path.relative_to(api.vault).as_posix() in review
    assert "[[Delete Me]]" in review
    assert "Superseded by a verified note" in review


def test_commit_refuses_without_exact_explicit_approval_and_leaves_pending(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    note_path = _approved_note(api)
    proposal = _stage(api, note_path)

    with pytest.raises(PermissionError):
        api.commit_delete(proposal["proposal_id"], approved=False)
    with pytest.raises(PermissionError):
        api.commit_delete(proposal["proposal_id"], approved=1)
    with pytest.raises((KeyError, ValidationError)):
        api.commit_delete(f"delete-{'0' * 32}", approved=True)

    assert (api.vault / note_path).exists()
    assert proposal["status"] == "pending"
    assert (api.vault / "Reviews" / "Pending" / f"{proposal['proposal_id']}.md").exists()


def test_commit_archives_note_and_source_cleans_only_its_map_entry_and_reindexes(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    note_path = _approved_note(api)
    source_path = next((api.vault / "Sources").glob("*.md"))
    map_path = api.vault / "Maps" / "Delete Map.md"
    unrelated_path = _approved_note(api, title="Keep Me")
    note_bytes = (api.vault / note_path).read_bytes()
    source_bytes = source_path.read_bytes()
    map_before = map_path.read_bytes()
    assert b"[[Delete Me]]" in map_before and b"[[Keep Me]]" in map_before
    api.index.reindex()
    assert api.index.search("Distinctive body for delete testing", limit=10)

    proposal = _stage(api, note_path)
    result = api.commit_delete(proposal["proposal_id"], approved=True)

    archive = api.vault / "Reviews" / "Deleted" / proposal["proposal_id"]
    assert result["status"] == "deleted"
    assert (archive / note_path).read_bytes() == note_bytes
    assert (archive / source_path.relative_to(api.vault)).read_bytes() == source_bytes
    assert not (api.vault / note_path).exists()
    assert not source_path.exists()
    map_after = map_path.read_bytes()
    assert b"[[Delete Me]]" not in map_after
    assert b"[[Keep Me]]" in map_after
    receipt = json.loads((archive / "receipt.json").read_text())
    assert receipt["note"]["sha256"] == hashlib.sha256(note_bytes).hexdigest()
    assert receipt["maps"][0]["original_base64"]
    remaining = api.index.search("Distinctive body for delete testing", limit=10)
    assert not any(hit.path == note_path for hit in remaining)
    assert (api.vault / unrelated_path).exists()
    assert not (api.vault / "Reviews" / "Pending" / f"{proposal['proposal_id']}.md").exists()


@pytest.mark.parametrize("stale_target", ["note", "source", "map"])
def test_stale_note_source_or_map_aborts_without_partial_changes(tmp_path: Path, stale_target: str) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    note_path = _approved_note(api)
    source_path = next((api.vault / "Sources").glob("*.md"))
    map_path = api.vault / "Maps" / "Delete Map.md"
    proposal = _stage(api, note_path)
    target = {"note": api.vault / note_path, "source": source_path, "map": map_path}[stale_target]
    target.write_bytes(target.read_bytes() + b"\nconcurrent edit\n")
    current = {path: path.read_bytes() for path in (api.vault / note_path, source_path, map_path)}

    with pytest.raises(ValidationError, match="changed"):
        api.commit_delete(proposal["proposal_id"], approved=True)

    assert {path: path.read_bytes() for path in current} == current
    assert (api.vault / note_path).exists()
    assert (api.vault / "Reviews" / "Pending" / f"{proposal['proposal_id']}.md").exists()
    assert not (api.vault / "Reviews" / "Deleted" / proposal["proposal_id"]).exists()


def test_prepare_delete_rejects_unknown_ambiguous_maps_pending_and_unsafe_notes(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    with pytest.raises((KeyError, ValidationError), match="unknown|approved|canonical"):
        api.prepare_delete("note-does-not-exist")

    note_path = _approved_note(api)
    note = api.vault / note_path
    metadata, body = read_note(note)
    duplicate = api.vault / "Tech" / "Notes" / "Duplicate.md"
    duplicate.write_text(dump_note({**metadata, "status": "pending"}, body), encoding="utf-8")
    with pytest.raises(ValidationError, match="Ambiguous"):
        api.prepare_delete(str(metadata["id"]))
    duplicate.unlink()

    with pytest.raises(ValidationError, match="Map"):
        api.prepare_delete("map-delete-map")

    metadata["status"] = "pending"
    note.write_text(dump_note(metadata, body), encoding="utf-8")
    with pytest.raises(ValidationError, match="approved"):
        api.prepare_delete(str(metadata["id"]))
    metadata["status"] = "approved"
    note.write_text(dump_note(metadata, body), encoding="utf-8")

    unsafe = api.vault / "Tech" / "Notes" / "Unsafe.md"
    unsafe.symlink_to(api.vault.parent / "outside.md")
    (api.vault.parent / "outside.md").write_text(dump_note({**metadata, "id": "note-unsafe"}, body))
    with pytest.raises(ValidationError, match="Unsafe"):
        api.prepare_delete("note-unsafe")


def test_missing_linked_source_is_reported_and_only_note_is_archived(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    note_path = _approved_note(api)
    source_path = next((api.vault / "Sources").glob("*.md"))
    source_path.unlink()

    proposal = _stage(api, note_path)

    assert proposal["source_missing"] is True
    assert "missing" in proposal["review"]
    api.commit_delete(proposal["proposal_id"], approved=True)
    archive = api.vault / "Reviews" / "Deleted" / proposal["proposal_id"]
    assert (archive / note_path).exists()
    assert not (archive / source_path.relative_to(api.vault)).exists()


def test_multiple_matching_sources_abort_staging(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    note_path = _approved_note(api)
    source_path = next((api.vault / "Sources").glob("*.md"))
    duplicate = api.vault / "Sources" / "duplicate.md"
    duplicate.write_bytes(source_path.read_bytes())
    metadata, _ = read_note(api.vault / note_path)

    with pytest.raises(ValidationError, match="Multiple source"):
        api.prepare_delete(str(metadata["id"]))


def test_archive_and_receipt_retain_byte_for_byte_recovery_data(tmp_path: Path) -> None:
    api = BrainAPI(tmp_path / "vault", use_embeddings=False)
    note_path = _approved_note(api)
    source_path = next((api.vault / "Sources").glob("*.md"))
    map_path = api.vault / "Maps" / "Delete Map.md"
    originals = {
        note_path: (api.vault / note_path).read_bytes(),
        source_path.relative_to(api.vault).as_posix(): source_path.read_bytes(),
        "Maps/Delete Map.md": map_path.read_bytes(),
    }
    proposal = _stage(api, note_path)
    api.commit_delete(proposal["proposal_id"], approved=True)

    archive = api.vault / "Reviews" / "Deleted" / proposal["proposal_id"]
    receipt = json.loads((archive / "receipt.json").read_text())
    recovered = {
        note_path: (archive / note_path).read_bytes(),
        source_path.relative_to(api.vault).as_posix(): (
            archive / source_path.relative_to(api.vault)
        ).read_bytes(),
        "Maps/Delete Map.md": base64.b64decode(receipt["maps"][0]["original_base64"]),
    }
    assert recovered == originals

