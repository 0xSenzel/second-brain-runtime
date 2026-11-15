from __future__ import annotations

from pathlib import Path

import yaml

from second_brain import maps
from second_brain.notes import dump_note, render_note
from second_brain.service import BrainService


def _approved(vault: Path, title: str, tags: list[str], summary: str, status: str = "approved") -> None:
    meta = {"id": f"note-{abs(hash(title)) % 10**8:08x}", "status": status, "type": "concept",
            "domain": "tech", "title": title, "tags": tags, "sources": []}
    (vault / "Notes" / f"{title}.md").write_text(
        render_note(meta, summary=summary, body="", connections=[]), encoding="utf-8"
    )


def _map(vault: Path, name: str, map_tags: list[str], body: str) -> Path:
    path = vault / "Maps" / f"{name}.md"
    path.write_text(dump_note({"type": "map", "title": name, "map_tags": map_tags}, body), encoding="utf-8")
    return path


def test_regenerate_lists_approved_notes_sorted_and_keeps_human_text(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    BrainService(vault)
    _approved(vault, "Zeta", ["ui"], "Zeta first. Second sentence.")
    _approved(vault, "Alpha", ["ui", "extra"], "Alpha only sentence")
    _approved(vault, "Other", ["data"], "Not on this map.")
    _approved(vault, "Pending", ["ui"], "Hidden.", status="inbox")
    path = _map(
        vault, "UI",
        ["ui"],
        f"# UI\n\nMy own prose.\n\n{maps.START_MARKER}\n- stale\n{maps.END_MARKER}\n\nTrailing prose.\n",
    )

    changed = maps.regenerate(vault)

    assert changed == [path]
    text = path.read_text()
    assert "- [[Alpha]] — Alpha only sentence\n- [[Zeta]] — Zeta first." in text
    assert "stale" not in text and "Other" not in text and "Pending" not in text
    assert "My own prose." in text and text.rstrip().endswith("Trailing prose.")
    assert maps.regenerate(vault) == []  # idempotent: nothing to rewrite


def test_map_without_markers_gets_the_block_appended(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    BrainService(vault)
    _approved(vault, "Alpha", ["ui"], "Alpha.")
    path = _map(vault, "UI", ["ui"], "# UI\n\nHuman text.\n")

    maps.regenerate(vault)

    text = path.read_text()
    assert "Human text." in text
    assert text.count(maps.START_MARKER) == 1 and "- [[Alpha]] — Alpha." in text


def test_only_tags_limits_which_maps_are_rewritten(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    BrainService(vault)
    _approved(vault, "Alpha", ["ui", "data"], "Alpha.")
    ui = _map(vault, "UI", ["ui"], "# UI\n")
    data = _map(vault, "Data", ["data"], "# Data\n")

    assert maps.regenerate(vault, only_tags={"data"}) == [data]
    assert maps.START_MARKER not in ui.read_text()


def test_maps_without_map_tags_are_never_touched(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    service = BrainService(vault)
    service.register_tags(["ui"])
    registry = vault / "Maps" / "Tag Registry.md"
    before = registry.read_text()

    maps.regenerate(vault)

    assert registry.read_text() == before


def test_review_base_is_created_once_in_a_fresh_vault(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    BrainService(vault)
    base = vault / "Views" / "Review.base"

    data = yaml.safe_load(base.read_text())
    assert data["filters"]["and"] == ['file.inFolder("Inbox")']
    assert data["views"][0]["type"] == "table"
    assert data["views"][0]["order"] == [
        "file.name", "status", "type", "domain", "tags", "created", "review_problems",
    ]

    base.write_text("user edited\n")
    BrainService(vault)
    assert base.read_text() == "user edited\n"
