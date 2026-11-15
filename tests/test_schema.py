"""The vault records which layout the code that wrote it expected.

Two repositories means the code and the notes can move independently. Nothing else
records that pairing, so a rollback of one would silently disagree with the other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from second_brain.models import SCHEMA_VERSION, VAULT_SCHEMA_FILE, SchemaMismatch
from second_brain.service import BrainService


def _stamp(vault: Path, payload: dict | str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (vault / VAULT_SCHEMA_FILE).write_text(text, encoding="utf-8")


def _write_attempts(service: BrainService) -> list:
    return [
        lambda: service.capture(content="c", title="T", summary="S", tags=["x"]),
        lambda: service.register_tags(["x"]),
        lambda: service.approve("note-000000000000", content_sha="0"),
        lambda: service.discard("note-000000000000"),
        lambda: service.update_inbox("note-000000000000", title="x"),
    ]


def test_a_new_vault_is_stamped_and_gets_the_schema_4_folders(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)

    stamp = json.loads((vault / VAULT_SCHEMA_FILE).read_text(encoding="utf-8"))

    assert stamp["schema_version"] == SCHEMA_VERSION == 4
    assert stamp["domains"] == ["personal", "tech"]
    assert "concept" in stamp["note_types"]
    for folder in ("Inbox", "Notes", "Sources", "Maps", "Views", "Templates",
                   "Archive/Inbox", "Archive/Notes", "Archive/Sources", "Archive/Receipts"):
        assert (vault / folder).is_dir(), folder
    assert (vault / "Views" / "Review.base").is_file()


def test_the_stamp_is_tracked_rather_than_hidden_in_the_generated_index(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)

    assert not VAULT_SCHEMA_FILE.startswith(".second-brain")
    assert (vault / VAULT_SCHEMA_FILE).is_file()


def test_opening_a_current_vault_does_not_rewrite_the_stamp(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    stamp = vault / VAULT_SCHEMA_FILE
    before = stamp.stat().st_mtime_ns

    BrainService(vault)

    assert stamp.stat().st_mtime_ns == before


@pytest.mark.parametrize("version", [3, SCHEMA_VERSION + 1])
def test_other_schemas_allow_reads_but_block_every_write(tmp_path: Path, version: int) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _stamp(vault, {"schema_version": version})
    service = BrainService(vault)  # must not raise

    assert service.schema_mismatch() is not None
    assert service.list_inbox() == [] and service.validate()["schema_mismatch"]
    for attempt in _write_attempts(service):
        with pytest.raises(SchemaMismatch):
            attempt()
    assert service.promote_reviewed() == []


def test_a_schema_3_vault_is_told_to_migrate_and_is_left_untouched(tmp_path: Path) -> None:
    """Schema 3 owns a `Notes/` folder; the old startup cleanup would have removed it."""
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Tech" / "Notes").mkdir(parents=True)
    (vault / "Tech" / "Notes" / "A.md").write_text("---\nid: note-1\n---\n")
    (vault / "Empty" / "Nested").mkdir(parents=True)
    _stamp(vault, {"schema_version": 3})

    service = BrainService(vault)

    assert "brain migrate-v4" in (service.schema_mismatch() or "")
    assert (vault / "Notes").is_dir() and (vault / "Empty" / "Nested").is_dir()
    assert not (vault / "Inbox").exists()
    assert service.unmigrated_folders() == ["Tech"]


def test_an_empty_notes_folder_is_never_removed_at_startup(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    BrainService(vault)

    assert (vault / "Notes").is_dir()


def test_an_unreadable_stamp_blocks_writes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _stamp(vault, "{not json")
    service = BrainService(vault)

    with pytest.raises(SchemaMismatch, match="unreadable"):
        service.register_tags(["x"])


@pytest.mark.parametrize("raw", ['"4"', "4.0", "true", "2.9"])
def test_a_non_integer_version_is_not_a_match(tmp_path: Path, raw: str) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _stamp(vault, '{"schema_version": ' + raw + "}")

    assert BrainService(vault).schema_mismatch() is not None


def test_a_matching_version_with_different_axes_is_reported(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _stamp(vault, {"schema_version": SCHEMA_VERSION, "domains": ["personal"], "note_types": ["concept"]})

    assert "domains" in (BrainService(vault).schema_mismatch() or "")
