from __future__ import annotations

import sqlite3
import time

import pytest
from test_retrieval import write_note

from second_brain.embedding import EmbeddingWorker
from second_brain.index import BrainIndex

CONCEPTS = {
    "coffee": ("espresso", "brew", "caffeine", "latte", "morning drink"),
    "motion": ("animation", "transition", "easing", "motion"),
    "money": ("budget", "invoice", "savings", "tax"),
}


class ConceptEmbedder:
    """Maps text onto concept axes so paraphrases with no shared words still match."""

    model = "concept-test"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        lowered = text.lower()
        vector = [0.01, 0.01, 0.01]
        for axis, words in enumerate(CONCEPTS.values()):
            vector[axis] += sum(lowered.count(word) for word in words)
        return vector


def build_vault(vault):
    write_note(vault, "Espresso Notes", details="Dial in the espresso grind and brew time.")
    write_note(vault, "Easing Curves", details="Animation easing and transition timing.")
    write_note(vault, "Tax Season", details="Invoice and budget tax planning.")
    write_note(vault, "Draft Latte Idea", folder="Inbox", details="A latte recipe with caffeine.")


@pytest.fixture(params=[True, False], ids=["sqlite-vec", "python-cosine"])
def index(request, tmp_path):
    if request.param:
        probe = BrainIndex(tmp_path / "probe")
        if not probe.vec_enabled:
            pytest.skip("sqlite-vec extension cannot load here")
    vault = tmp_path / "vault"
    build_vault(vault)
    idx = BrainIndex(vault, embedder=ConceptEmbedder(), use_sqlite_vec=request.param)
    idx.reindex(embed_vectors=True)
    assert idx.vec_enabled is request.param
    return idx


def titles(hits):
    return [hit.title for hit in hits]


def test_vector_finds_paraphrase_with_no_lexical_overlap(index) -> None:
    hits = index.search("morning drink")  # no note contains these words as text... except by concept

    assert hits[0].title in {"Espresso Notes", "Draft Latte Idea"}
    assert "Tax Season" not in titles(hits)
    assert not any("Vector search" in w for w in index._search("morning drink", limit=5, hops=0)[1])


def test_fusion_promotes_notes_found_by_both_rankings(index) -> None:
    # "latte" matches the inbox note lexically and every coffee note semantically.
    hits = index.search("espresso latte")

    assert hits[0].title == "Espresso Notes"
    assert set(titles(hits)) >= {"Espresso Notes", "Draft Latte Idea"}
    assert hits[0].score > hits[-1].score


def test_vec_and_python_paths_rank_identically(tmp_path) -> None:
    rankings = []
    for flag in (True, False):
        vault = tmp_path / f"vault-{flag}"
        build_vault(vault)
        idx = BrainIndex(vault, embedder=ConceptEmbedder(), use_sqlite_vec=flag)
        idx.reindex(embed_vectors=True)
        rankings.append(
            [titles(idx.search(q, limit=5)) for q in ("coffee brew", "animation", "tax budget", "latte")]
        )
    assert rankings[0] == rankings[1]


def test_include_inbox_false_applies_to_vector_hits(index) -> None:
    assert "Draft Latte Idea" in titles(index.search("caffeine"))
    assert "Draft Latte Idea" not in titles(index.search("caffeine", include_inbox=False))


def test_embedding_outage_degrades_to_lexical_with_warning(index) -> None:
    class Down(ConceptEmbedder):
        def embed(self, text):
            raise ConnectionError("ollama down")

    index.embedder = Down()
    index.embedder.model = "concept-test"
    hits, warnings = index._search("espresso grind", limit=5, hops=0)

    assert hits[0].title == "Espresso Notes"
    assert any("Vector search failed" in w for w in warnings)


def test_vectors_written_without_extension_are_resynced(tmp_path) -> None:
    probe = BrainIndex(tmp_path / "probe")
    if not probe.vec_enabled:
        pytest.skip("sqlite-vec extension cannot load here")
    vault = tmp_path / "vault"
    build_vault(vault)
    plain = BrainIndex(vault, embedder=ConceptEmbedder(), use_sqlite_vec=False)
    plain.reindex(embed_vectors=True)
    fast = BrainIndex(vault, embedder=ConceptEmbedder(), use_sqlite_vec=True)

    assert titles(fast.search("coffee brew"))[0] == "Espresso Notes"
    assert fast.vector_stats()["backend"] == "sqlite-vec"


def test_worker_run_once_embeds_in_small_batches(tmp_path) -> None:
    vault = tmp_path / "vault"
    build_vault(vault)
    embedder = ConceptEmbedder()
    index = BrainIndex(vault, embedder=embedder)
    index.reindex()
    assert embedder.calls == 0
    total = len(index.pending_chunks())
    worker = EmbeddingWorker(index, embedder, interval=0.01, batch_size=3)

    first = worker.run_once()
    rest = 0
    while (n := worker.run_once()):
        rest += n

    assert first == 3 and first + rest == total
    assert index.pending_chunks() == []
    assert worker.run_once() == 0
    assert titles(index.search("coffee brew"))[0] == "Espresso Notes"


def test_worker_backs_off_after_failures_and_recovers(tmp_path) -> None:
    class Flaky(ConceptEmbedder):
        down = True

        def embed(self, text):
            if self.down:
                raise ConnectionError("down")
            return super().embed(text)

    vault = tmp_path / "vault"
    build_vault(vault)
    embedder = Flaky()
    index = BrainIndex(vault, embedder=embedder)
    index.reindex()
    worker = EmbeddingWorker(index, embedder, interval=1.0, max_backoff=8.0)

    assert worker.run_once() == 0
    assert worker.run_once() == 0
    assert worker.failures == 2 and worker.next_delay() == 4.0
    for _ in range(5):
        worker.run_once()
    assert worker.next_delay() == 8.0  # capped
    embedder.down = False
    assert worker.run_once() > 0
    assert worker.failures == 0 and worker.next_delay() == 1.0


def test_worker_thread_start_and_stop_fills_vectors(tmp_path) -> None:
    vault = tmp_path / "vault"
    build_vault(vault)
    embedder = ConceptEmbedder()
    index = BrainIndex(vault, embedder=embedder)
    index.reindex()
    worker = EmbeddingWorker(index, embedder, interval=0.05)

    worker.start()
    deadline = time.time() + 5
    while index.pending_chunks() and time.time() < deadline:
        time.sleep(0.05)
    worker.stop()

    assert index.pending_chunks() == []
    assert worker._thread is None


def test_worker_replaces_vectors_from_a_different_model(tmp_path) -> None:
    vault = tmp_path / "vault"
    build_vault(vault)
    old = ConceptEmbedder()
    index = BrainIndex(vault, embedder=old)
    index.reindex(embed_vectors=True)

    new = ConceptEmbedder()
    new.model = "concept-v2"
    worker = EmbeddingWorker(index, new, batch_size=100)
    assert worker.run_once() > 0

    with sqlite3.connect(index.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM chunks WHERE vector IS NULL").fetchone()[0] == 0
    assert index.vector_stats()["model"] == "concept-v2"


def test_lexical_gate_applies_only_when_vector_side_works(tmp_path) -> None:
    class Orthogonal(ConceptEmbedder):
        """Puts every text far from every query, so only the lexical side can match."""

        def embed(self, text):
            return [0.0, 0.0, 1.0] if text.startswith("probe") else [1.0, 0.0, 0.0]

    vault = tmp_path / "vault"
    write_note(vault, "Espresso Notes", details="Dial in the espresso grind.")
    query = "probe espresso zebra migration corridors"

    plain = BrainIndex(vault)
    plain.reindex()
    gated = BrainIndex(vault, embedder=Orthogonal())
    gated.reindex(embed_vectors=True)

    assert titles(plain.search(query)) == ["Espresso Notes"]  # lexical-only keeps recall
    assert gated.search(query) == []  # one incidental term of five is not enough
    assert titles(gated.search("espresso grind")) == ["Espresso Notes"]
