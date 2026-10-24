from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from second_brain.index import BrainIndex
from second_brain.service import BrainService


class TinyEmbedder:
    """Stable local stand-in; production uses Ollama."""

    model = "tiny-test"

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * 32
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode()).digest()
            vector[digest[0] % len(vector)] += 1.0
        return vector


class FlakyEmbedder(TinyEmbedder):
    def __init__(self) -> None:
        self.fail_next = True

    def embed(self, text: str) -> list[float]:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("temporary embedding outage")
        return super().embed(text)


class SecondTinyEmbedder(TinyEmbedder):
    model = "second-tiny-test"


class CountingSecondTinyEmbedder(SecondTinyEmbedder):
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return super().embed(text)


def save(service: BrainService, **kwargs):
    """Commit a note, filling in whatever structural metadata the test did not specify."""
    kwargs.setdefault("note_type", "concept")
    kwargs.setdefault("domain", "tech")
    kwargs.setdefault("content", f"Raw fixture for {kwargs['title']}.")
    kwargs.setdefault("summary", kwargs.get("content", "")[:80])
    kwargs.setdefault("tags", ["testing", "fixture"])
    kwargs.setdefault("aliases", [kwargs["title"] + " alt", kwargs["title"] + " alias"])
    kwargs.setdefault("maps", ["Test Map"])
    proposal = service.prepare_save(**kwargs)
    return service.commit_save(proposal.id)


def test_index_excludes_pending_and_follows_graph_in_both_directions(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    save(
        service,
        note_type="concept",
        content="Visual hierarchy and accessibility improve interface clarity.",
        title="UI Design",
        summary="Principles for clear and accessible interfaces.",
        body="## Details\n\nVisual hierarchy and accessibility improve interface clarity.",
    )
    save(
        service,
        note_type="skill",
        content="A repeatable interface review workflow.",
        title="Frontend Critique Skill",
        summary="Reviews interfaces using saved design principles.",
        connections=[{"relation": "uses", "target": "UI Design", "reason": "Review standard"}],
    )
    pending = service.prepare_save(
        note_type="prompt",
        content="Secret pending accessibility prompt",
        title="Pending Prompt",
    )

    index = BrainIndex(vault, embedder=TinyEmbedder())
    result = index.reindex()
    hits = index.search("visual hierarchy accessibility", limit=5, hops=1)

    # Normal retrieval contains only approved distilled notes from canonical folders.
    assert result["indexed_notes"] == 2
    assert all(hit.title != "Pending Prompt" for hit in hits)
    assert hits[0].title == "UI Design"
    assert any(hit.title == "Frontend Critique Skill" and hit.via_graph for hit in hits)
    assert (vault / "Reviews" / "Pending" / f"{pending.id}.md").exists()


def test_context_is_cited_budgeted_and_read_only(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    save(
        service,
        note_type="prompt",
        content="Review a UI for hierarchy, spacing, accessibility, and responsiveness.",
        title="Frontend Design Critique",
        summary="A concise checklist for interface reviews.",
        body="## Checklist\n\nReview accessibility, keyboard flow, and interface hierarchy.",
        source_url="https://example.com/prompt",
    )
    index = BrainIndex(vault, embedder=TinyEmbedder())
    index.reindex()
    before = {path: path.read_bytes() for path in vault.rglob("*.md")}

    bundle = index.context("accessibility review", max_tokens=120, hops=1)

    assert bundle["estimated_tokens"] <= 120
    assert "Frontend Design Critique" in bundle["context"]
    assert "Tech/Notes/" not in bundle["context"]  # this item belongs in Prompts
    assert "Tech/Prompts/Frontend Design Critique.md" in bundle["context"]
    assert "https://example.com/prompt" in bundle["context"]
    assert before == {path: path.read_bytes() for path in vault.rglob("*.md")}


def test_reindex_is_incremental_and_removes_deleted_notes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    item = save(service, note_type="concept", content="Graph retrieval", title="Graph Retrieval")
    index = BrainIndex(vault, embedder=TinyEmbedder())

    first = index.reindex()
    second = index.reindex()
    (vault / item.note_path).unlink()
    third = index.reindex()

    assert first["embedded_chunks"] > 0
    assert second["embedded_chunks"] == 0
    assert third["deleted_notes"] == 1
    assert all(hit.title != "Graph Retrieval" for hit in index.search("Graph Retrieval"))


def test_reindex_backfills_vectors_when_embeddings_are_enabled_later(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    save(service, note_type="concept", content="Local semantic retrieval", title="Semantic Search")

    lexical_only = BrainIndex(vault)
    lexical_only.reindex()

    semantic = BrainIndex(vault, embedder=TinyEmbedder())
    result = semantic.reindex()
    with sqlite3.connect(semantic.db_path) as db:
        missing_vectors = db.execute("SELECT COUNT(*) FROM chunks WHERE vector IS NULL").fetchone()[0]

    assert result["embedded_chunks"] > 0
    assert missing_vectors == 0


def test_reindex_retries_chunks_after_a_temporary_embedding_failure(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    save(
        service,
        note_type="concept",
        title="Retry Embeddings",
        body="## Details\n\n" + ("Retry semantic retrieval. " * 600),
    )
    embedder = FlakyEmbedder()
    index = BrainIndex(vault, embedder=embedder)

    first = index.reindex()
    with sqlite3.connect(index.db_path) as db:
        chunk_ids = [row[0] for row in db.execute("SELECT id FROM chunks ORDER BY id")]
        for chunk_id in chunk_ids[1:]:
            db.execute(
                "UPDATE chunks SET vector=? WHERE id=?",
                (json.dumps([1.0] * 32), chunk_id),
            )
        first_missing = db.execute("SELECT COUNT(*) FROM chunks WHERE vector IS NULL").fetchone()[0]

    second = index.reindex()
    with sqlite3.connect(index.db_path) as db:
        second_missing = db.execute("SELECT COUNT(*) FROM chunks WHERE vector IS NULL").fetchone()[0]

    assert first["warnings"]
    assert first["embedded_chunks"] == 0
    assert first_missing == 1
    assert second["embedded_chunks"] == first_missing
    assert second_missing == 0


def test_lexical_reindex_does_not_claim_a_new_embedding_model(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    save(service, title="Model Version Note", content="Model version compatibility.")
    index = BrainIndex(vault, embedder=TinyEmbedder())
    index.reindex()
    index.embedder = SecondTinyEmbedder()

    skipped = index.reindex(embed_vectors=False)
    with sqlite3.connect(index.db_path) as db:
        skipped_model = db.execute(
            "SELECT value FROM metadata WHERE key='embedding_model'"
        ).fetchone()[0]
    refreshed = index.reindex()
    with sqlite3.connect(index.db_path) as db:
        refreshed_model = db.execute(
            "SELECT value FROM metadata WHERE key='embedding_model'"
        ).fetchone()[0]

    assert skipped["embedded_chunks"] == 0
    assert skipped_model == "tiny-test"
    assert refreshed["embedded_chunks"] > 0
    assert refreshed_model == "second-tiny-test"


def test_semantic_fallback_skips_vectors_from_a_different_model(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    save(
        service,
        title="Semantic Compatibility",
        content="A note about semantic compatibility and vector indexing.",
    )
    index = BrainIndex(vault, embedder=TinyEmbedder())
    index.reindex()
    replacement = CountingSecondTinyEmbedder()
    index.embedder = replacement

    context = index.context("qzparaphrase no lexical match")

    assert replacement.calls == 0
    assert context["sources"] == []
    assert any("indexed vectors use embedding model" in warning for warning in context["warnings"])
    assert any("reindex" in warning for warning in context["warnings"])


def test_semantic_fallback_warns_when_new_notes_have_no_vectors(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    save(
        service,
        title="Previously Indexed Note",
        content="This note already has a semantic embedding.",
    )
    index = BrainIndex(vault, embedder=TinyEmbedder())
    index.reindex()

    save(
        service,
        title="Recently Added Note",
        content="A recently added note receives vectors during background reindexing.",
    )
    result = index.reindex(embed_vectors=False)
    context = index.context("qzparaphrase no lexical match")

    assert result["embedded_chunks"] == 0
    assert any("incomplete coverage" in warning for warning in context["warnings"])
    assert any("no embeddings" in warning for warning in context["warnings"])


def test_find_ids_for_legacy_notes_without_frontmatter_ids_can_be_read(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note_path = vault / "Tech" / "Notes" / "Legacy Note.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        "---\nstatus: approved\ntype: concept\ndomain: tech\ntitle: Legacy Note\n---\n"
        "# Legacy Note\n\n## Summary\nA legacy note without a stable frontmatter ID.\n\n"
        "## Details\nThis exact note body remains readable through its derived stable ID.\n",
        encoding="utf-8",
    )
    index = BrainIndex(vault)
    index.reindex()

    hit = index.search("legacy exact body", limit=5)[0]
    result = index.read_note(hit.id)

    assert hit.id.startswith("note-")
    assert result["path"] == "Tech/Notes/Legacy Note.md"
    assert "This exact note body remains readable" in result["body"]


def test_reindex_revalidates_frontmatter_against_canonical_folder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    item = save(
        service,
        note_type="concept",
        content="Canonical folder validation sentinel phrase.",
        title="Canonical Folder Check",
    )
    index = BrainIndex(vault)
    index.reindex()
    note_path = vault / item.note_path
    content = note_path.read_text(encoding="utf-8")
    content = content.replace("type: concept", "type: prompt", 1)
    note_path.write_text(content, encoding="utf-8")
    stat = note_path.stat()

    # Simulate an existing index from the prior version that already recorded the new
    # file timestamp. The index-version bump must still force a metadata revalidation.
    with sqlite3.connect(index.db_path) as db:
        db.execute("UPDATE metadata SET value='1' WHERE key='index_version'")
        db.execute(
            "UPDATE nodes SET mtime_ns=?,file_size=? WHERE path=?",
            (stat.st_mtime_ns, stat.st_size, item.note_path),
        )
        note_id = db.execute("SELECT id FROM nodes WHERE path=?", (item.note_path,)).fetchone()[0]

    result = index.reindex()

    assert result["deleted_notes"] == 1
    assert index.search("canonical folder validation sentinel", limit=5) == []
    with pytest.raises(KeyError, match="Unknown approved canonical note ID"):
        index.read_note(note_id)


def test_reindex_catches_same_size_approval_change_when_mtime_is_restored(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    item = save(
        service,
        note_type="concept",
        content="Approval revocation cache invalidation sentinel.",
        title="Revoked Note",
    )
    index = BrainIndex(vault)
    index.reindex()
    note_path = vault / item.note_path
    original_stat = note_path.stat()
    content = note_path.read_text(encoding="utf-8")
    revoked = content.replace("status: approved", "status: rejected", 1)
    assert len(revoked) == len(content)
    note_path.write_text(revoked, encoding="utf-8")
    os.utime(note_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert note_path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert note_path.stat().st_ctime_ns != original_stat.st_ctime_ns
    result = index.reindex()

    assert result["deleted_notes"] == 1
    assert index.search("approval revocation cache invalidation", limit=5) == []


def test_find_returns_compact_alias_match_and_ask_returns_body_evidence(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    item = save(
        service,
        title="UI Microinteractions",
        summary="A guide to feedback and interface transitions.",
        body=("## Details\n\nUse immediate visual feedback to show an action was received. "
              "Keep motion brief and respect reduced motion settings."),
        aliases=["微交互设计指南", "interface feedback patterns"],
        source_url="https://example.com/ui-notes",
    )
    index = BrainIndex(vault)
    index.reindex()

    hit = index.search("微交互设计指南", limit=5)[0]
    context = index.context("show that an action was received", max_tokens=4000)

    assert hit.id == item.proposal_id or hit.id.startswith("note-")
    assert hit.title == "UI Microinteractions"
    assert hit.path == "Tech/Notes/UI Microinteractions.md"
    assert "alias" in hit.match_location
    assert len(hit.excerpt) <= 260
    assert hit.source_url == "https://example.com/ui-notes"
    assert "Use immediate visual feedback" in context["context"]
    assert "Tech/Notes/UI Microinteractions.md" in context["context"]
    assert context["estimated_tokens"] <= 4000
    assert len(context["sources"]) <= 4


def test_read_note_resolves_only_approved_canonical_ids_and_marks_truncation(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    item = save(
        service,
        title="Sectioned Note",
        body=("Full note body. " * 80) + "\n\n## Caveats\n\nNot part of details.",
    )
    index = BrainIndex(vault)
    index.reindex()
    canonical_path = vault / item.note_path
    metadata_text = canonical_path.read_text(encoding="utf-8")
    note_id = metadata_text.split("id: ", 1)[1].splitlines()[0]

    result = index.read_note(note_id, section="Details", max_chars=100)
    assert result["path"] == item.note_path
    assert result["section"] == "Details"
    assert result["truncated"] is True
    assert result["omitted_characters"] > 0
    assert "characters omitted" in result["body"]
    assert "Not part of details" not in result["body"]

    with pytest.raises(KeyError, match="Unknown approved canonical note ID"):
        index.read_note("../../outside.md")
    with pytest.raises(KeyError, match="Section"):
        index.read_note(note_id, section="Missing")

    duplicate = vault / "Tech" / "Notes" / "Duplicate.md"
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_text(canonical_path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="Ambiguous"):
        index.read_note(note_id)


def test_read_note_loads_full_body_only_for_matching_frontmatter_id(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    target = save(
        service,
        title="Target Body Read",
        body="## Details\n\n" + ("Target body content. " * 1000),
    )
    save(
        service,
        title="Other Large Note",
        body="## Details\n\n" + ("Unrelated body content. " * 1000),
    )
    target_path = vault / target.note_path
    note_id = target_path.read_text(encoding="utf-8").split("id: ", 1)[1].splitlines()[0]
    original_read_text = Path.read_text
    full_body_reads: list[Path] = []

    def counted_read_text(path, *args, **kwargs):
        if path.suffix == ".md":
            full_body_reads.append(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)

    result = BrainIndex(vault).read_note(note_id, max_chars=30000)

    assert result["path"] == target.note_path
    assert result["truncated"] is False
    assert full_body_reads == [target_path]


def test_index_skips_unchanged_reads_and_invalidates_edit_rename_and_approval(
    tmp_path: Path, monkeypatch
):
    vault = tmp_path / "vault"
    service = BrainService(vault)
    item = save(service, title="Cache Test", body="## Details\n\nOldMarkerUnique.")
    index = BrainIndex(vault)
    index.reindex()
    path = vault / item.note_path

    original_read_text = Path.read_text
    note_reads = 0

    def counted_read_text(candidate, *args, **kwargs):
        nonlocal note_reads
        if candidate == path:
            note_reads += 1
        return original_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)
    index.reindex()
    assert note_reads == 0
    monkeypatch.setattr(Path, "read_text", original_read_text)

    text = path.read_text(encoding="utf-8").replace("OldMarkerUnique", "NewMarkerUnique")
    path.write_text(text, encoding="utf-8")
    index.reindex()
    assert index.search("NewMarkerUnique")
    assert not index.search("OldMarkerUnique")

    renamed = path.with_name("Cache Test Renamed.md")
    path.rename(renamed)
    index.reindex()
    assert index.search("NewMarkerUnique")[0].path == "Tech/Notes/Cache Test Renamed.md"

    unapproved = renamed.read_text(encoding="utf-8").replace("status: approved", "status: pending")
    renamed.write_text(unapproved, encoding="utf-8")
    index.reindex()
    assert not index.search("NewMarkerUnique")
