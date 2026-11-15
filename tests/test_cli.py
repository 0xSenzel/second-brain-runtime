from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from fakes_surface import FakeIndex, FakeService

from second_brain import cli
from second_brain.api import BrainAPI


@pytest.fixture
def service(tmp_path: Path, monkeypatch) -> FakeService:
    fake = FakeService(tmp_path)

    def factory(vault=None):
        return BrainAPI(tmp_path, service=fake, index=FakeIndex(), approval="human")

    monkeypatch.setattr(cli, "BrainAPI", factory)
    return fake


def run(capsys, *argv: str) -> str:
    cli.main(list(argv))
    return capsys.readouterr().out


def test_every_contract_subcommand_parses() -> None:
    parser = cli.parser()
    for argv in (
        ["capture", "text"], ["inbox"], ["approve", "n"], ["discard", "n"], ["promote"], ["digest"],
        ["report"], ["find", "q"], ["ask", "q"], ["reindex"], ["embed"], ["validate"], ["doctor"],
        ["register-tags", "a", "b"], ["migrate-v4"], ["migrate-v4", "--apply"],
    ):
        parser.parse_args(argv)


def test_capture_passes_options(service: FakeService, capsys, tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text("Body text", encoding="utf-8")

    out = json.loads(run(capsys, "capture", "# Title\ntext", "--summary", "S", "--tag", "ui",
                         "--body-file", str(body), "--revises", "note-1", "--type", "prompt"))

    _, _, kwargs = service.calls[0]
    assert kwargs["body"] == "Body text"
    assert kwargs["revises"] == "note-1"
    assert kwargs["note_type"] == "prompt"
    assert out["status"] == "inbox"


def test_approve_uses_current_sha_and_works_in_human_mode(service: FakeService, capsys) -> None:
    out = json.loads(run(capsys, "approve", "note-aaaaaaaaaaaa"))
    assert out["status"] == "approved"
    assert ("approve", ("note-aaaaaaaaaaaa",), {"content_sha": "sha-1"}) in service.calls


def test_approve_with_stale_sha_fails_with_error(service: FakeService, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["approve", "note-aaaaaaaaaaaa", "--sha", "old"])
    assert exit_info.value.code == 1
    assert "changed after review" in capsys.readouterr().err


def test_digest_prints_text_and_json(service: FakeService, capsys) -> None:
    assert run(capsys, "digest").startswith("Inbox: 1 item")
    assert json.loads(run(capsys, "digest", "--json"))["inbox_count"] == 1


def test_inbox_discard_and_find(service: FakeService, capsys) -> None:
    assert json.loads(run(capsys, "inbox"))["count"] == 1
    assert json.loads(run(capsys, "discard", "note-aaaaaaaaaaaa", "--reason", "dup"))["archived"]
    assert json.loads(run(capsys, "find", "q"))["results"]


def test_migrate_v4_is_lazy_dry_run_by_default_and_never_builds_the_api(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "BrainAPI", lambda *a, **k: pytest.fail("API must not be built"))
    seen = {}
    fake = types.ModuleType("second_brain.migrate_v4")

    def plan(vault, *, tag_map=None, map_tags=None):
        seen["plan"] = (vault, tag_map, map_tags)
        return {"steps": ["move"]}

    fake.plan = plan
    fake.apply = lambda planned: {"applied": planned}
    monkeypatch.setitem(sys.modules, "second_brain.migrate_v4", fake)
    tag_map = tmp_path / "tags.json"
    tag_map.write_text('{"old": null}')

    dry = json.loads(run(capsys, "--vault", str(tmp_path), "migrate-v4", "--tag-map", str(tag_map)))
    applied = json.loads(run(capsys, "--vault", str(tmp_path), "migrate-v4", "--apply"))

    assert dry == {"dry_run": True, "plan": {"steps": ["move"]}}
    assert seen["plan"][0] == tmp_path.resolve()
    assert applied == {"applied": {"steps": ["move"]}}
    assert seen["plan"][1] is None
