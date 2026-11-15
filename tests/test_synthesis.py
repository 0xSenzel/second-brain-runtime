from datetime import UTC, datetime
from pathlib import Path

from fakes_surface import FakeService, make_item

from second_brain import synthesis

NOW = datetime(2026, 11, 15, tzinfo=UTC)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def note(vault: Path, name: str, **meta: object) -> None:
    body = meta.pop("body", "")
    lines = ["---", "status: approved", f"id: note-{name}", f"title: {name}"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines += ["---", f"# {name}", str(body)]
    write(vault / "Notes" / f"{name}.md", "\n".join(lines))


def test_digest_counts_ages_and_warnings(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service.inbox_items = [
        make_item("note-1", title="Old", created="2026-10-01T00:00:00Z", problems=["no tags"]),
        make_item("note-2", title="New", created="2026-11-14T00:00:00Z"),
    ]

    data = synthesis.digest(service, None, now=NOW)
    text = synthesis.render_digest(data)

    assert data["inbox_count"] == 2
    assert data["oldest_age_days"] == 45
    assert "Old [note-1] 45d WARN: no tags" in text
    assert "New [note-2] 1d" in text


def test_empty_inbox_digest_is_one_line() -> None:
    assert "empty" in synthesis.render_digest({"inbox_count": 0, "items": []}).lower()


def test_digest_truncates_under_the_message_limit(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service.inbox_items = [
        make_item(f"note-{i}", title=f"Item number {i} " + "x" * 80, problems=["summary is empty"])
        for i in range(200)
    ]

    text = synthesis.render_digest(synthesis.digest(service, None, now=NOW))

    assert len(text) < 3500
    assert "more (not shown)" in text
    assert text.splitlines()[0].startswith("Inbox: 200 items")


def test_report_finds_duplicates_orphans_unmapped_tags_and_sources(tmp_path: Path) -> None:
    vault = tmp_path
    note(vault, "Alpha", source_url="https://x/1", tags="[busy]")
    note(vault, "Beta", source_url="https://x/1", tags="[busy]")
    note(vault, "Gamma Guide", tags="[busy]", body="See [[Alpha]]")
    note(vault, "Gamma Guides", tags="[busy]")
    note(vault, "Delta", tags="[busy]", updated="2025-01-01T00:00:00Z")
    note(vault, "Mapped", tags="[mapped-tag]")
    write(
        vault / "Maps" / "Docs.md",
        "---\ntype: map\nmap_tags: [mapped-tag]\n---\n# Docs\n",
    )
    write(vault / "Sources" / "s1-used.md", "---\n---\nraw")
    write(vault / "Sources" / "s2-orphan.md", "---\n---\nraw")
    write(
        vault / "Notes" / "Source User.md",
        "---\nid: note-su\ntitle: Source User\nsources: ['[[s1-used]]']\n---\n",
    )
    write(vault / "Inbox" / "Fresh.md", "---\nstatus: inbox\nsources: ['[[s2-orphan]]']\n---\n")
    service = FakeService(vault)
    service.inbox_items = [make_item("note-old", created="2026-10-01T00:00:00Z")]

    result = synthesis.report(
        service,
        None,
        similar_pairs=lambda: [("note-Alpha", "note-Delta", 0.95), ("note-Alpha", "note-Mapped", 0.5)],
        now=NOW,
    )

    pairs = {frozenset(item["notes"]): item["reason"] for item in result["likely_duplicates"]}
    assert "same source_url" in pairs[frozenset({"Alpha", "Beta"})]
    assert pairs[frozenset({"Gamma Guide", "Gamma Guides"})] == "near-identical title"
    assert pairs[frozenset({"Alpha", "Delta"})] == "vector similarity"
    assert frozenset({"Alpha", "Mapped"}) not in pairs
    assert "Alpha" not in result["notes_without_links_or_map"]  # Gamma links to it
    assert "Mapped" not in result["notes_without_links_or_map"]  # its tag is on a map
    assert "Beta" in result["notes_without_links_or_map"]
    assert result["stale_inbox"][0]["note_id"] == "note-old"
    assert result["orphan_sources"] == []
    assert {"note": "Delta", "age_days": 683} in result["stale_notes"]


def test_report_flags_busy_unmapped_tags_and_orphan_source(tmp_path: Path) -> None:
    for i in range(5):
        note(tmp_path, f"N{i}", tags="[busy]")
    write(tmp_path / "Sources" / "lonely.md", "---\n---\n")

    result = synthesis.report(FakeService(tmp_path), None, now=NOW)

    assert result["tags_without_map"] == [{"tag": "busy", "notes": 5}]
    assert result["orphan_sources"] == ["lonely"]


def test_report_ignores_failing_vector_callable(tmp_path: Path) -> None:
    def broken():
        raise RuntimeError("no vectors")

    assert synthesis.report(FakeService(tmp_path), None, similar_pairs=broken, now=NOW)[
        "likely_duplicates"
    ] == []
