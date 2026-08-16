from pathlib import Path

from second_brain.migration import MigrationPlanner
from second_brain.service import BrainService


def test_migration_is_copy_only_bounded_and_staged_for_review(tmp_path: Path) -> None:
    source = tmp_path / "old-vault"
    source.mkdir()
    for number in range(5):
        (source / f"Note {number}.md").write_text(f"# Note {number}\n\nOriginal {number}", encoding="utf-8")
    original = {path: path.read_bytes() for path in source.rglob("*.md")}
    service = BrainService(tmp_path / "new-vault")
    planner = MigrationPlanner(service)

    preview = planner.plan(source, limit=3)
    applied = planner.apply(preview)

    assert len(preview) == 3
    assert applied["created_proposals"] == 3
    assert len(service.list_pending()) == 3
    assert not list((service.vault / "Tech" / "Notes").glob("Note *.md"))
    assert original == {path: path.read_bytes() for path in source.rglob("*.md")}


def test_migration_rerun_skips_already_pending_source(tmp_path: Path) -> None:
    source = tmp_path / "old-vault"
    source.mkdir()
    note = source / "Existing.md"
    note.write_text("# Existing\n\nKeep me", encoding="utf-8")
    service = BrainService(tmp_path / "new-vault")
    planner = MigrationPlanner(service)

    first = planner.apply(planner.plan(source))
    second = planner.apply(planner.plan(source))

    assert first["created_proposals"] == 1
    assert second["created_proposals"] == 0
    assert second["skipped_existing"] == 1
