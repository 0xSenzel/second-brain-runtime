from __future__ import annotations

from pathlib import Path

from second_brain.index import BrainIndex
from second_brain.notes import dump_note
from second_brain.service import BrainService


def _commit(service: BrainService, title: str, **overrides):
    payload = dict(
        note_type="concept",
        domain="tech",
        content=f"Body text for {title}.",
        title=title,
        summary=f"What {title} is for.",
        tags=["testing", "fixture"],
        aliases=[f"{title} alt", f"{title} alias"],
        maps=["Test Map"],
    )
    payload.update(overrides)
    return service.commit_save(service.prepare_save(**payload).id)


def test_lint_flags_a_hand_written_note_that_bypassed_the_save_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    (vault / "Tech" / "Notes" / "Hand Written.md").write_text(
        dump_note(
            {"id": "note-manual", "type": "concept", "domain": "tech",
             "status": "approved", "tags": ["testing"]},
            "# Hand Written\n\n## Summary\n\nAdded by hand.\n",
        ),
        encoding="utf-8",
    )

    report = service.validate()
    problems = next(f for f in report["findings"] if f["path"] == "Tech/Notes/Hand Written.md")["problems"]

    assert any("aliases" in problem for problem in problems)
    assert any("map" in problem for problem in problems)
    assert any("tags" in problem for problem in problems)


def test_lint_passes_a_note_written_through_the_save_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    service.register_tags(["testing", "fixture"])
    saved = _commit(service, "Conforming Note")

    report = service.validate()

    assert saved.note_path not in [finding["path"] for finding in report["findings"]]


def test_lint_reports_folders_left_over_from_the_old_layout(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    stray = vault / "Resources" / "Old.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("# Old\n", encoding="utf-8")

    assert service.validate()["unmigrated_folders"] == ["Resources"]


def test_null_source_url_is_reported_rather_than_treated_as_a_value(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    (vault / "Tech" / "Notes" / "Null URL.md").write_text(
        dump_note(
            {
                "id": "note-null",
                "type": "concept",
                "domain": "tech",
                "status": "approved",
                "tags": ["testing", "fixture"],
                "aliases": ["a", "b"],
                "maps": ["Test Map"],
                "source_url": None,
            },
            "# Null URL\n",
        ),
        encoding="utf-8",
    )

    problems = next(
        f for f in service.validate()["findings"] if f["path"] == "Tech/Notes/Null URL.md"
    )["problems"]

    assert any("source_url is null" in problem for problem in problems)


# --- maps must not crowd out the notes they index ------------------------------------


def test_a_map_does_not_outrank_the_note_it_indexes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)
    _commit(
        service,
        "Semantic Chunking",
        content="Semantic chunking splits documents on meaning rather than length.",
        summary="How semantic chunking splits documents on meaning.",
    )
    index = BrainIndex(vault, embedder=None)
    index.reindex()

    hits = index.search("semantic chunking", limit=5, hops=1)

    assert hits[0].path == "Tech/Notes/Semantic Chunking.md"
    assert hits[0].note_type != "map"


def test_graph_hops_do_not_travel_through_a_map(tmp_path: Path) -> None:
    """Every note links to its map, so hopping through one would relate them all."""
    vault = tmp_path / "vault"
    service = BrainService(vault)
    _commit(service, "Alpha Topic", content="Alpha concerns retrieval latency.")
    _commit(service, "Unrelated Beta", content="Beta concerns kitchen plumbing entirely.")
    index = BrainIndex(vault, embedder=None)
    index.reindex()

    hits = index.search("retrieval latency", limit=6, hops=1)

    assert "Tech/Notes/Unrelated Beta.md" not in [hit.path for hit in hits]


# --- ranking must not reward length ---------------------------------------------------


def test_a_long_source_does_not_outrank_its_own_short_note(tmp_path: Path) -> None:
    """Scores once summed over every matching chunk, so the longest file always won."""
    vault = tmp_path / "vault"
    service = BrainService(vault)
    long_body = "\n\n".join(
        f"Section {n}: soreness, microtears and hypertrophy explained at length. " * 12
        for n in range(12)
    )
    _commit(
        service,
        "Why microtears do not drive growth",
        content=long_body,
        summary="Soreness and microtears do not drive hypertrophy.",
    )
    index = BrainIndex(vault, embedder=None)
    index.reindex()

    hits = index.search("soreness microtears hypertrophy", limit=5, hops=1)

    assert hits[0].note_type != "source", (
        f"a raw source outranked its distilled note: {[(h.note_type, h.path) for h in hits]}"
    )
    assert hits[0].path == "Tech/Notes/Why microtears do not drive growth.md"


def test_graph_scores_do_not_cascade_between_seeds(tmp_path: Path) -> None:
    """Seed scores were read from a dict being mutated inside the same loop."""
    vault = tmp_path / "vault"
    service = BrainService(vault)
    _commit(service, "Alpha", content="Retrieval latency budgets and caching.")
    index = BrainIndex(vault, embedder=None)
    index.reindex()

    hits = index.search("retrieval latency", limit=6, hops=1)

    direct = [h for h in hits if not h.via_graph]
    hopped = [h for h in hits if h.via_graph]
    assert direct, "expected at least one direct match"
    if hopped:
        assert max(h.score for h in hopped) <= max(h.score for h in direct), (
            "a graph neighbour outscored every direct match"
        )
