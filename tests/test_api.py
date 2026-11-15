from pathlib import Path

import pytest
from fakes_surface import FakeEmbedder, FakeIndex, FakeService, make_item

from second_brain import api as api_module
from second_brain.api import READ_ONLY_ERROR, BrainAPI
from second_brain.ingest import IngestedSource
from second_brain.models import ApprovalMismatch


def make_api(tmp_path: Path, **kwargs):
    service = kwargs.pop("service", None) or FakeService(tmp_path)
    index = kwargs.pop("index", None) or FakeIndex()
    api = BrainAPI(tmp_path, service=service, index=index, **kwargs)
    return api, service, index


class StubIngestor:
    def __init__(self, source: IngestedSource):
        self.source = source

    def ingest(self, value: str, *, kind: str | None = None) -> IngestedSource:
        return self.source


def test_capture_derives_provenance_and_reindexes_lexically(tmp_path: Path) -> None:
    api, service, index = make_api(tmp_path)

    result = api.capture("# Heading\n\nSome text", summary="S", tags=["ui"], connections=[
        {"relation": "uses", "target": "Other", "reason": "x"}
    ])

    _, _, kwargs = service.calls[0]
    assert kwargs["title"] == "Heading"
    assert kwargs["source_kind"] == "note"
    assert kwargs["content"].startswith("# Heading")
    assert kwargs["connections"][0].target == "Other"
    assert result["status"] == "inbox"
    assert result["content_sha"] == "sha-new"
    assert ("reindex", (), {"embed_vectors": False}) in index.calls or any(
        name == "reindex" for name, _, _ in index.calls
    )


def test_capture_explicit_provenance_wins(tmp_path: Path) -> None:
    ingested = IngestedSource(kind="article", title="Fetched", content="text", source_url="https://derived/x")
    api, service, _ = make_api(tmp_path, ingestor=StubIngestor(ingested))

    api.capture("https://derived/x", source_url="https://canonical/1", source_kind="douyin-video")
    explicit = service.calls[-1][2]
    api.capture("https://derived/x")
    derived = service.calls[-1][2]

    assert explicit["source_url"] == "https://canonical/1"
    assert explicit["source_kind"] == "douyin-video"
    assert explicit["title"] == "Fetched"
    assert derived["source_url"] == "https://derived/x"
    assert derived["source_kind"] == "article"


def test_capture_surfaces_soft_problems(tmp_path: Path) -> None:
    api, _, _ = make_api(tmp_path)
    assert api.capture("text", title="T")["problems"] == ["summary is empty"]


def test_approve_requires_explicit_approval_and_matching_sha(tmp_path: Path) -> None:
    api, service, index = make_api(tmp_path)

    with pytest.raises(PermissionError):
        api.approve("note-aaaaaaaaaaaa", "sha-1", approved=False)
    with pytest.raises(ApprovalMismatch):
        api.approve("note-aaaaaaaaaaaa", "stale", approved=True)
    result = api.approve("note-aaaaaaaaaaaa", "sha-1", approved=True)

    assert result["status"] == "approved"
    assert result["note_path"] == "Notes/Example.md"
    assert result["obsidian_uri"].startswith("obsidian://open?")
    assert "approve" in service.names()
    assert any(name == "reindex" for name, _, _ in index.calls)


def test_human_approval_mode_refuses_tool_approval_but_not_cli(tmp_path: Path) -> None:
    api, service, _ = make_api(tmp_path, approval="human")

    with pytest.raises(PermissionError, match="human-only"):
        api.approve("note-aaaaaaaaaaaa", "sha-1", approved=True)
    assert "approve" not in service.names()
    assert api.approve("note-aaaaaaaaaaaa", "sha-1", approved=True, out_of_band=True)["status"] == "approved"


def test_approve_and_discard_purge_capture_scratch(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "1234567").mkdir()
    (cache / "1234567-video.mp4").write_text("x")
    (cache / "7654321.mp4").write_text("x")
    monkeypatch.setenv("SECOND_BRAIN_CAPTURE_CACHE", str(cache))
    api, _, _ = make_api(tmp_path)

    assert api.approve("note-aaaaaaaaaaaa", "sha-1", approved=True)["scratch_purged"] == 2
    assert (cache / "7654321.mp4").exists()
    (cache / "1234567-video.mp4").write_text("x")
    assert api.discard("note-aaaaaaaaaaaa", reason="dup")["scratch_purged"] == 1
    assert (cache / "7654321.mp4").exists()


def test_reader_mode_refuses_every_write_and_never_builds_a_service(tmp_path: Path, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("reader must not construct BrainService")

    monkeypatch.setattr(api_module, "BrainService", explode)
    missing = tmp_path / "no-vault"
    api = BrainAPI(missing, index=FakeIndex(), mode="reader")

    assert api.capture("x") == READ_ONLY_ERROR
    assert api.update_inbox("n", title="t") == READ_ONLY_ERROR
    assert api.approve("n", "s", approved=True) == READ_ONLY_ERROR
    assert api.discard("n") == READ_ONLY_ERROR
    assert api.promote() == READ_ONLY_ERROR
    assert api.register_tags(["a"]) == READ_ONLY_ERROR
    assert api.prepare_delete("n") == READ_ONLY_ERROR
    assert api.commit_delete("p", approved=True) == READ_ONLY_ERROR
    assert api.embed() == READ_ONLY_ERROR
    assert api.list_tags()["tags"] == []
    assert not missing.exists()


def test_reader_mode_does_not_promote_before_reads(tmp_path: Path) -> None:
    api, service, _ = make_api(tmp_path, mode="reader")
    api.find("q")
    assert "promote_reviewed" not in service.names()


def test_writer_promotes_before_read_tools_and_swallows_errors(tmp_path: Path) -> None:
    api, service, _ = make_api(tmp_path)

    api.find("q")
    assert service.names()[0] == "promote_reviewed"

    service.promote_error = RuntimeError("boom")
    result = api.find("q")
    assert result["results"][0]["title"] == "Hit"
    assert any("boom" in warning for warning in result["warnings"])
    assert any("boom" in warning for warning in api.read("note-1")["warnings"])


def test_find_and_ask_include_inbox_and_label_sources(tmp_path: Path) -> None:
    api, _, index = make_api(tmp_path)

    found = api.find("q", limit=50)
    asked = api.ask("q", hops=1)

    assert index.calls[1][2] == {"limit": 10, "include_inbox": True}
    assert found["results"][0]["obsidian_uri"].startswith("obsidian://open?")
    assert asked["sources"][0]["obsidian_uri"].startswith("obsidian://open?")
    assert "unreviewed" in asked["instruction"]


def test_inbox_lists_without_bodies_and_reads_one_in_full(tmp_path: Path) -> None:
    api, service, _ = make_api(tmp_path)
    service.inbox_items = [make_item(problems=["x"])]

    listing = api.inbox()
    single = api.inbox("note-aaaaaaaaaaaa")

    assert listing["count"] == 1
    assert "text" not in listing["items"][0] and "body" not in listing["items"][0]
    assert single["item"]["content_sha"] == "sha-1"
    assert single["item"]["body"] == "A body."


def test_update_inbox_passes_only_given_fields(tmp_path: Path) -> None:
    api, service, _ = make_api(tmp_path)
    api.update_inbox("note-aaaaaaaaaaaa", title="New", tags=None, summary="S")
    assert service.calls[-1] == ("update_inbox", ("note-aaaaaaaaaaaa",), {"title": "New", "summary": "S"})
    with pytest.raises(ValueError):
        api.update_inbox("n", bogus=1)


def test_promote_reports_results(tmp_path: Path) -> None:
    api, service, _ = make_api(tmp_path)
    service.promoted = [{"note_id": "n", "error": "no tags"}]
    assert api.promote()["promoted"] == [{"note_id": "n", "error": "no tags"}]


def test_embed_runs_worker_once(tmp_path: Path, monkeypatch) -> None:
    seen = {}

    class Worker:
        def __init__(self, index, embedder):
            seen["args"] = (index, embedder)

        def run_once(self) -> int:
            return 7

    monkeypatch.setattr(api_module, "EmbeddingWorker", Worker)
    embedder = FakeEmbedder()
    api, _, index = make_api(tmp_path, embedder=embedder)

    assert api.embed() == {"embedded_chunks": 7}
    assert seen["args"] == (index, embedder)


def test_delete_is_two_step_and_needs_approval(tmp_path: Path) -> None:
    class Deleter:
        def prepare(self, note_id, *, reason=""):
            return {"proposal_id": "del-1", "note_id": note_id}

        def commit(self, proposal_id):
            return {"archived": [proposal_id]}

    api, _, _ = make_api(tmp_path, deleter=Deleter())

    assert api.prepare_delete("note-1")["proposal_id"] == "del-1"
    with pytest.raises(PermissionError):
        api.commit_delete("del-1", approved=False)
    assert api.commit_delete("del-1", approved=True)["archived"] == ["del-1"]


def test_doctor_reports_modes_and_reachability(tmp_path: Path) -> None:
    api, _, _ = make_api(tmp_path, embedder=FakeEmbedder(up=False), approval="human")
    report = api.doctor()
    assert report["mode"] == "writer"
    assert report["approval"] == "human"
    assert report["ollama_reachable"] is False
    assert report["git_journal"]["vault_is_repository"] is False
    assert report["schema_version"] == 4


def test_invalid_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BrainAPI(tmp_path, service=FakeService(), index=FakeIndex(), mode="admin")


def test_open_returns_uri_without_launching_an_application(tmp_path: Path) -> None:
    api, _, _ = make_api(tmp_path / "My Brain")
    result = api.open_note("Notes/Example.md")
    assert "vault=My%20Brain" in result["obsidian_uri"]
    assert "file=Notes%2FExample.md" in result["obsidian_uri"]


def test_digest_and_report_use_synthesis(tmp_path: Path) -> None:
    (tmp_path / "Notes").mkdir()
    api, _, _ = make_api(tmp_path)
    assert api.digest()["inbox_count"] == 1
    assert "text" in api.digest()
    assert "likely_duplicates" in api.report()
