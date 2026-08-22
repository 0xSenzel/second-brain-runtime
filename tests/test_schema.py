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


def _prepare(service: BrainService, **overrides):
    payload = dict(
        note_type="concept",
        domain="tech",
        content="Body text.",
        title="Schema Demo",
        summary="A note used to exercise the schema stamp.",
        tags=["testing", "fixture"],
        aliases=["Schema demo note", "Stamp demo"],
        maps=["Test Map"],
    )
    payload.update(overrides)
    return service.prepare_save(**payload)


def test_a_new_vault_is_stamped_with_the_current_schema(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)

    stamp = json.loads((vault / VAULT_SCHEMA_FILE).read_text(encoding="utf-8"))

    assert stamp["schema_version"] == SCHEMA_VERSION
    assert sorted(stamp["domains"]) == ["personal", "tech"]
    assert "concept" in stamp["note_types"]


def test_the_stamp_is_tracked_rather_than_hidden_in_the_generated_index(tmp_path: Path) -> None:
    """It has to travel with the notes, so it cannot live under the gitignored index."""
    vault = tmp_path / "vault"
    BrainService(vault)

    assert not VAULT_SCHEMA_FILE.startswith(".second-brain")
    assert (vault / VAULT_SCHEMA_FILE).is_file()


def test_reads_still_work_when_the_schema_disagrees(tmp_path: Path) -> None:
    """A mismatch must not brick retrieval; only writes are unsafe."""
    vault = tmp_path / "vault"
    BrainService(vault)
    (vault / VAULT_SCHEMA_FILE).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION + 1}), encoding="utf-8"
    )

    service = BrainService(vault)  # must not raise

    assert service.schema_mismatch() is not None


def test_a_newer_vault_blocks_writes_from_older_code(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    (vault / VAULT_SCHEMA_FILE).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION + 1}), encoding="utf-8"
    )
    service = BrainService(vault)

    with pytest.raises(SchemaMismatch, match="newer"):
        _prepare(service)


def test_an_older_vault_blocks_writes_until_it_is_migrated(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    (vault / VAULT_SCHEMA_FILE).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION - 1}), encoding="utf-8"
    )
    service = BrainService(vault)

    with pytest.raises(SchemaMismatch, match="older"):
        _prepare(service)


def test_a_matching_schema_permits_writes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    service = BrainService(vault)

    proposal = _prepare(service)
    saved = service.commit_save(proposal.id)

    assert (vault / saved.note_path).exists()
    assert service.schema_mismatch() is None


def test_doctor_and_validate_report_the_mismatch(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    (vault / VAULT_SCHEMA_FILE).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION + 1}), encoding="utf-8"
    )
    service = BrainService(vault)

    assert "newer" in service.validate()["schema_mismatch"]
