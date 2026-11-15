from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

from second_brain.index import INDEX_VERSION, BrainIndex


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


class CountingEmbedder(TinyEmbedder):
    def __init__(self, model: str = "tiny-test") -> None:
        self.model = model
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return super().embed(text)


def write_note(
    vault: Path,
    title: str,
    *,
    folder: str = "Notes",
    status: str | None = None,
    summary: str = "",
    details: str = "",
    connections: tuple[tuple[str, str], ...] = (),
    note_id: str | None = None,
    **extra,
) -> Path:
    """Write a schema-4 note file directly, as the writer service would."""
    status = status or ("inbox" if folder == "Inbox" else "approved")
    note_id = note_id or "note-" + hashlib.sha256(title.encode()).hexdigest()[:12]
    frontmatter = {
        "id": note_id, "status": status, "type": "concept", "domain": "tech", "title": title,
        "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
        "tags": ["testing"], **extra,
    }
    lines = [f"# {title}", "", "## Summary", "", summary or f"About {title}.", ""]
    if details:
        lines += ["## Details", "", details, ""]
    lines += ["## Connections", ""]
    lines += [f"- {relation}: [[{target}]] — reason" for relation, target in connections] or ["- None yet"]
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    text = f"---\n{header}---\n\n" + "\n".join(lines) + "\n"
    path = vault / folder / f"{title}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


def test_index_covers_notes_and_inbox_only_and_follows_graph(vault: Path) -> None:
    write_note(vault, "UI Design", summary="Principles for clear interfaces.",
               details="Visual hierarchy and accessibility improve interface clarity.")
    write_note(vault, "Frontend Critique Skill", summary="Reviews interfaces.",
               connections=(("uses", "UI Design"),))
    write_note(vault, "Pending Idea", folder="Inbox", details="Secret accessibility idea")
    write_note(vault, "Old Source", folder="Sources", details="accessibility raw source")
    write_note(vault, "Archived", folder="Archive/Notes", details="accessibility archived")
    write_note(vault, "Retired Review", folder="Reviews", details="accessibility review")

    index = BrainIndex(vault)
    result = index.reindex()
    hits = index.search("visual hierarchy accessibility", limit=5, hops=1)

    assert result["indexed_notes"] == 3
    assert (result["approved_notes"], result["inbox_notes"]) == (2, 1)
    titles = {hit.title for hit in hits}
    assert titles == {"UI Design", "Frontend Critique Skill", "Pending Idea"}
    assert hits[0].title == "UI Design"
    assert any(hit.title == "Frontend Critique Skill" and hit.via_graph for hit in hits)


def test_inbox_hits_are_weighted_flagged_and_can_be_excluded(vault: Path) -> None:
    write_note(vault, "Approved Espresso", details="espresso grind size and extraction time")
    write_note(vault, "Inbox Espresso", folder="Inbox", details="espresso grind size and extraction time")
    index = BrainIndex(vault)
    index.reindex()

    hits = index.search("espresso grind extraction")
    by_title = {hit.title: hit for hit in hits}

    assert hits[0].title == "Approved Espresso"
    assert by_title["Approved Espresso"].as_dict()["status"] == "approved"
    assert by_title["Approved Espresso"].as_dict()["unreviewed"] is False
    assert by_title["Inbox Espresso"].as_dict()["status"] == "inbox"
    assert by_title["Inbox Espresso"].as_dict()["unreviewed"] is True
    approved_score = by_title["Approved Espresso"].score
    assert by_title["Inbox Espresso"].score == pytest.approx(approved_score * 0.5, rel=0.3)
    assert [h.title for h in index.search("espresso", include_inbox=False)] == ["Approved Espresso"]
    bundle = index.context("espresso grind", include_inbox=False)
    assert all(source["status"] == "approved" for source in bundle["sources"])
    assert "unreviewed" in index.context("espresso grind")["context"]


def test_inbox_only_note_is_found_and_ranks_by_weight_only(vault: Path) -> None:
    write_note(vault, "Fresh Capture", folder="Inbox", details="zebra migration corridors")
    index = BrainIndex(vault)
    index.reindex()

    hits = index.search("zebra migration")

    assert [h.title for h in hits] == ["Fresh Capture"]
    assert hits[0].status == "inbox"


def test_context_is_cited_budgeted_and_read_only(vault: Path) -> None:
    write_note(vault, "Frontend Design Critique", summary="A concise checklist for interface reviews.",
               details="Review accessibility, keyboard flow, and interface hierarchy.",
               source_url="https://example.com/prompt")
    index = BrainIndex(vault)
    index.reindex()
    before = {path: path.read_bytes() for path in vault.rglob("*.md")}

    bundle = index.context("accessibility review", max_tokens=120, hops=1)

    assert bundle["estimated_tokens"] <= 120
    assert "Frontend Design Critique" in bundle["context"]
    assert "Notes/Frontend Design Critique.md" in bundle["context"]
    assert "https://example.com/prompt" in bundle["context"]
    assert before == {path: path.read_bytes() for path in vault.rglob("*.md")}


def test_index_path_follows_env_then_vault_default(vault: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SECOND_BRAIN_INDEX_PATH", raising=False)
    assert BrainIndex(vault).db_path == vault.resolve() / ".second-brain" / "index.sqlite3"

    target = tmp_path / "elsewhere" / "idx.sqlite3"
    monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", str(target))
    index = BrainIndex(vault)
    assert index.db_path == target
    assert target.exists()


def test_reindex_never_calls_embedder_unless_asked(vault: Path) -> None:
    write_note(vault, "Graph Retrieval", details="graph retrieval notes")
    embedder = CountingEmbedder()
    index = BrainIndex(vault, embedder=embedder)

    result = index.reindex()

    assert embedder.calls == 0
    assert result["embedded_chunks"] == 0
    assert index.vector_stats()["embedded"] == 0
    assert index.reindex(embed_vectors=True)["embedded_chunks"] > 0


def test_reindex_is_incremental_and_removes_deleted_notes(vault: Path) -> None:
    path = write_note(vault, "Graph Retrieval", details="graph retrieval")
    index = BrainIndex(vault, embedder=TinyEmbedder())

    first = index.reindex(embed_vectors=True)
    second = index.reindex(embed_vectors=True)
    path.unlink()
    third = index.reindex()

    assert first["embedded_chunks"] > 0
    assert second["embedded_chunks"] == 0
    assert third["deleted_notes"] == 1
    assert index.search("Graph Retrieval") == []


def test_reindex_backfills_vectors_when_embeddings_are_enabled_later(vault: Path) -> None:
    write_note(vault, "Semantic Search", details="Local semantic retrieval")
    BrainIndex(vault).reindex()

    semantic = BrainIndex(vault, embedder=TinyEmbedder())
    result = semantic.reindex(embed_vectors=True)
    with sqlite3.connect(semantic.db_path) as db:
        missing = db.execute("SELECT COUNT(*) FROM chunks WHERE vector IS NULL").fetchone()[0]

    assert result["embedded_chunks"] > 0
    assert missing == 0


def test_reindex_retries_chunks_after_a_temporary_embedding_failure(vault: Path) -> None:
    write_note(vault, "Retry Embeddings", details=("Retry semantic retrieval. " * 600))
    index = BrainIndex(vault, embedder=FlakyEmbedder())

    first = index.reindex(embed_vectors=True)
    pending = len(index.pending_chunks())
    second = index.reindex(embed_vectors=True)

    assert first["warnings"] and first["embedded_chunks"] == 0
    assert pending > 1
    assert second["embedded_chunks"] == pending
    assert index.pending_chunks() == []


def test_model_change_drops_old_vectors_and_search_skips_mismatch(vault: Path) -> None:
    write_note(vault, "Model Version Note", details="Model version compatibility.")
    index = BrainIndex(vault, embedder=TinyEmbedder())
    index.reindex(embed_vectors=True)

    other = CountingEmbedder("other-model")
    index.embedder = other
    _hits, warnings = index._search("model version", limit=5, hops=0)
    assert any("tiny-test" in warning and "other-model" in warning for warning in warnings)
    assert other.calls == 0  # never embeds the query against vectors from another model

    result = index.reindex(embed_vectors=True)
    assert result["embedded_chunks"] > 0
    assert index.vector_stats()["model"] == "other-model"


def test_lexical_reindex_does_not_claim_an_embedding_model(vault: Path) -> None:
    write_note(vault, "Model Version Note", details="Model version compatibility.")
    index = BrainIndex(vault, embedder=TinyEmbedder())
    index.reindex()
    assert index.vector_stats()["model"] == ""


def test_new_notes_without_vectors_warn_about_partial_coverage(vault: Path) -> None:
    write_note(vault, "Vector Coverage", details="Vector coverage fixture.")
    index = BrainIndex(vault, embedder=TinyEmbedder())
    index.reindex(embed_vectors=True)
    write_note(vault, "Later Note", details="Added after the embedding pass.")
    index.reindex()

    _hits, warnings = index._search("vector coverage", limit=5, hops=0)

    assert any("covers" in warning for warning in warnings)


def test_ids_without_frontmatter_ids_can_be_read(vault: Path) -> None:
    path = vault / "Notes" / "Legacy Note.md"
    path.parent.mkdir()
    path.write_text(
        "---\nstatus: approved\ntype: concept\ndomain: tech\n---\n\n# Legacy Note\n\n## Summary\n\nOld.\n"
    )
    index = BrainIndex(vault)
    index.reindex()

    hit = index.search("Legacy Note")[0]

    assert index.read_note(hit.id)["title"] == "Legacy Note"


def test_reindex_skips_notes_whose_frontmatter_contradicts_their_folder(vault: Path) -> None:
    write_note(vault, "Rejected Thing", status="rejected", details="contradiction marker")
    write_note(vault, "Fine Thing", details="contradiction marker")
    index = BrainIndex(vault)
    index.reindex()

    assert [h.title for h in index.search("contradiction marker")] == ["Fine Thing"]


def test_reindex_catches_same_size_edit_when_mtime_is_restored(vault: Path) -> None:
    path = write_note(vault, "Stable Note", details="alpha token")
    index = BrainIndex(vault)
    index.reindex()
    stat = path.stat()
    path.write_text(path.read_text().replace("alpha", "gamma"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    index.reindex()

    assert index.search("alpha") == []
    assert index.search("gamma")[0].title == "Stable Note"


def test_approval_move_keeps_one_node_and_updates_status(vault: Path) -> None:
    inbox = write_note(
        vault, "Moving Note", folder="Inbox", note_id="note-fixed00001", details="moving token"
    )
    index = BrainIndex(vault)
    index.reindex()
    assert index.search("moving token")[0].status == "inbox"

    text = inbox.read_text().replace("status: inbox", "status: approved")
    inbox.unlink()
    (vault / "Notes").mkdir(exist_ok=True)
    (vault / "Notes" / "Moving Note.md").write_text(text)
    result = index.reindex()

    hits = index.search("moving token")
    assert (result["approved_notes"], result["inbox_notes"]) == (1, 0)
    assert len(hits) == 1 and hits[0].status == "approved"
    assert hits[0].id == "note-fixed00001"


def test_find_returns_compact_alias_match_and_body_evidence(vault: Path) -> None:
    write_note(vault, "UI Microinteractions", aliases=["微交互设计指南"], summary="Feedback patterns.",
               details="Keep transitions short and respect reduced motion.")
    index = BrainIndex(vault)
    index.reindex()

    alias = index.search("微交互设计指南")[0]
    body = index.context("transitions reduced motion")

    assert alias.title == "UI Microinteractions"
    assert "alias" in alias.match_location
    assert "Keep transitions short" in body["context"]


def test_read_note_resolves_ids_and_marks_truncation(vault: Path) -> None:
    write_note(vault, "Long Note", note_id="note-long00000001",
               details="## Alpha\n\n" + "x" * 300 + "\n\n## Omega\n\nlast words", source_url="https://example.com/x")
    write_note(vault, "Draft", folder="Inbox", note_id="note-draft0000001", details="draft body")
    write_note(vault, "Source Note", folder="Sources", note_id="note-source000001")
    index = BrainIndex(vault)

    full = index.read_note("note-long00000001")
    short = index.read_note("note-long00000001", max_chars=40)
    section = index.read_note("note-long00000001", section="Omega")

    assert full["truncated"] is False and "last words" in full["body"]
    assert full["status"] == "approved"
    assert full["provenance"] == {"source_url": "https://example.com/x"}
    assert short["truncated"] is True and short["omitted_characters"] > 0
    assert section["body"].startswith("## Omega") and "Alpha" in section["omitted_sections"]
    assert index.read_note("note-draft0000001")["status"] == "inbox"
    with pytest.raises(KeyError):
        index.read_note("note-draft0000001", include_inbox=False)
    with pytest.raises(KeyError):
        index.read_note("note-source000001")
    with pytest.raises(KeyError):
        index.read_note("../Notes/Long Note")


def test_read_note_rejects_ambiguous_ids(vault: Path) -> None:
    write_note(vault, "One", note_id="note-dup000000001")
    write_note(vault, "Two", note_id="note-dup000000001")
    with pytest.raises(ValueError, match="Ambiguous"):
        BrainIndex(vault).read_note("note-dup000000001")


def test_index_skips_unchanged_reads_and_invalidates_on_edit_and_rename(vault: Path, monkeypatch) -> None:
    path = write_note(vault, "Cache Note", details="cache token")
    index = BrainIndex(vault)
    index.reindex()

    reads: list[str] = []
    original = Path.read_text

    def spy(self, *args, **kwargs):
        reads.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    index.reindex()
    assert reads == []

    path.write_text(original(path) + "\nedited tail\n")
    time.sleep(0.01)
    index.reindex()
    assert reads == ["Cache Note.md"]
    assert index.search("edited tail")

    path.rename(vault / "Notes" / "Renamed Note.md")
    index.reindex()
    assert index.search("cache token")[0].path == "Notes/Renamed Note.md"


def test_stale_index_version_rebuilds_cleanly(vault: Path) -> None:
    write_note(vault, "Rebuild Me", details="rebuild token")
    index = BrainIndex(vault)
    index.reindex()
    with sqlite3.connect(index.db_path) as db:
        db.execute("UPDATE metadata SET value='3' WHERE key='index_version'")

    fresh = BrainIndex(vault)
    assert fresh.search("rebuild token") == []  # derived data dropped, not trusted
    fresh.reindex()
    with sqlite3.connect(fresh.db_path) as db:
        version = db.execute("SELECT value FROM metadata WHERE key='index_version'").fetchone()[0]
    assert version == INDEX_VERSION
    assert fresh.search("rebuild token")


def test_lexical_ranking_prefers_titles_and_ignores_stopword_noise(vault: Path) -> None:
    write_note(vault, "Spaced Repetition", details="Review cards on a schedule.")
    write_note(vault, "Cooking Basics", details="What is the best way to chop an onion and how to do it.")
    index = BrainIndex(vault)
    index.reindex()

    assert index.search("spaced repetition")[0].title == "Spaced Repetition"
    assert index.search("what is the best way to chop an onion")[0].title == "Cooking Basics"
    assert index.search("qzxwv unknownterm") == []


def test_json_vector_roundtrip_uses_expected_shape(vault: Path) -> None:
    write_note(vault, "Shape", details="shape")
    index = BrainIndex(vault, embedder=TinyEmbedder(), use_sqlite_vec=False)
    index.reindex(embed_vectors=True)
    with sqlite3.connect(index.db_path) as db:
        vector = json.loads(db.execute("SELECT vector FROM chunks").fetchone()[0])
    assert len(vector) == 32
