from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

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


def save(service: BrainService, **kwargs):
    """Commit a note, filling in whatever structural metadata the test did not specify."""
    kwargs.setdefault("note_type", "concept")
    kwargs.setdefault("domain", "tech")
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

    # Two distilled notes, their two raw sources, and the map they were filed on.
    assert result["indexed_notes"] == 5
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
