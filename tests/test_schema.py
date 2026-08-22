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


# --- a vault this code does not understand must not be touched -----------------------


def test_a_mismatched_vault_is_not_reorganised_on_open(tmp_path: Path) -> None:
    """Retiring folders ran before the schema was checked, so a read-only open would
    reorganise — and partly delete — a vault written by a different layout."""
    vault = tmp_path / "vault"
    (vault / "Knowledge").mkdir(parents=True)
    (vault / "Knowledge" / "paper.pdf").write_text("attachment", encoding="utf-8")
    (vault / VAULT_SCHEMA_FILE).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION - 1}), encoding="utf-8"
    )

    service = BrainService(vault)

    assert (vault / "Knowledge" / "paper.pdf").read_text() == "attachment"
    assert service.schema_mismatch() is not None


def test_retiring_a_folder_never_deletes_non_markdown_files(tmp_path: Path) -> None:
    """The emptiness guard only looked for *.md, so attachments were collateral."""
    vault = tmp_path / "vault"
    BrainService(vault)
    stray = vault / "Knowledge"
    stray.mkdir(parents=True, exist_ok=True)
    (stray / "diagram.png").write_text("image bytes", encoding="utf-8")

    BrainService(vault)

    assert (stray / "diagram.png").exists()


def test_an_empty_retired_folder_is_still_removed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    (vault / "Knowledge").mkdir(parents=True, exist_ok=True)

    BrainService(vault)

    assert not (vault / "Knowledge").exists()


# --- the stamp must not create churn or hide disagreement ----------------------------


def test_opening_a_matching_vault_leaves_the_stamp_byte_identical(tmp_path: Path) -> None:
    """It is a tracked file; rewriting a timestamp on every read dirties the notes repo."""
    vault = tmp_path / "vault"
    BrainService(vault)
    before = (vault / VAULT_SCHEMA_FILE).read_bytes()

    BrainService(vault)

    assert (vault / VAULT_SCHEMA_FILE).read_bytes() == before


def test_a_stamp_naming_different_axes_is_reported_not_overwritten(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    (vault / VAULT_SCHEMA_FILE).write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "domains": ["work", "life"], "note_types": ["memo"]}
        ),
        encoding="utf-8",
    )

    service = BrainService(vault)

    assert service.schema_mismatch() is not None
    assert "work" in (vault / VAULT_SCHEMA_FILE).read_text(encoding="utf-8")


# --- unreadable and malformed stamps --------------------------------------------------


def test_an_unreadable_stamp_is_reported_rather_than_crashing(tmp_path: Path) -> None:
    """read_text was inside the try, but OSError was not in the caught tuple — so the
    exact case the error message names was the one that escaped it."""
    vault = tmp_path / "vault"
    BrainService(vault)
    stamp = vault / VAULT_SCHEMA_FILE
    stamp.unlink()
    stamp.mkdir()  # a directory where a file belongs raises IsADirectoryError

    service = BrainService(vault)  # must not raise

    assert "unreadable" in (service.schema_mismatch() or "")


@pytest.mark.parametrize("raw", [2.9, "2", True, None, [2]])
def test_a_non_integer_version_is_rejected_rather_than_coerced(tmp_path: Path, raw) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    (vault / VAULT_SCHEMA_FILE).write_text(json.dumps({"schema_version": raw}), encoding="utf-8")

    service = BrainService(vault)

    assert service.schema_mismatch() is not None


def test_register_tags_is_blocked_on_a_mismatched_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    BrainService(vault)
    (vault / VAULT_SCHEMA_FILE).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION + 1}), encoding="utf-8"
    )
    service = BrainService(vault)

    with pytest.raises(SchemaMismatch):
        service.register_tags(["brand-new-tag", "another"])
