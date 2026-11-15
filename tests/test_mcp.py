from __future__ import annotations

import json
from pathlib import Path

import anyio
import httpx
import pytest
from fakes_surface import FakeIndex, FakeService

from second_brain import mcp_server
from second_brain.api import BrainAPI

WRITE_TOOLS = {
    "brain_capture", "brain_update_inbox", "brain_approve", "brain_discard",
    "brain_register_tags", "brain_prepare_delete", "brain_commit_delete",
}
READ_TOOLS = {
    "brain_find", "brain_ask", "brain_read", "brain_neighbors", "brain_open",
    "brain_list_tags", "brain_inbox", "brain_digest", "brain_report", "brain_validate",
}


@pytest.fixture
def api(tmp_path: Path, monkeypatch):
    instance = BrainAPI(tmp_path, service=FakeService(tmp_path), index=FakeIndex(), mode="writer")
    monkeypatch.setattr(mcp_server, "_api", lambda: instance)
    return instance


def call(name: str, arguments: dict):
    async def run():
        return await mcp_server.mcp.call_tool(name, arguments)

    result = anyio.run(run)
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_api_is_reused_for_same_configuration_and_split_by_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SECOND_BRAIN_VAULT", str(tmp_path / "first"))
    monkeypatch.setenv("SECOND_BRAIN_MODE", "reader")
    first = mcp_server._api()
    assert mcp_server._api() is first
    assert first.read_only
    monkeypatch.setenv("SECOND_BRAIN_MODE", "writer")
    assert mcp_server._api() is not first


def test_tool_set_matches_the_contract_and_uses_native_arguments() -> None:
    tools = {tool.name: tool for tool in anyio.run(mcp_server.mcp.list_tools)}

    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for tool in tools.values():
        assert not any(name.endswith("_json") for name in tool.inputSchema["properties"])
    capture = tools["brain_capture"].inputSchema["properties"]
    assert capture["tags"]["anyOf"][0]["type"] == "array"
    assert capture["connections"]["anyOf"][0]["type"] == "array"
    assert tools["brain_approve"].inputSchema["required"] == ["note_id", "content_sha"]


def test_instructions_are_short_and_state_the_rules() -> None:
    text = mcp_server.INSTRUCTIONS
    assert len(text) < 2000
    for phrase in ("Search first", "Inbox/", "unreviewed", "content_sha", "verbatim", "Obsidian",
                   "untrusted", "two steps", "archived"):
        assert phrase.lower() in text.lower()


def test_capture_then_approve_through_tools(api) -> None:
    captured = call("brain_capture", {
        "content_or_url": "text", "title": "T", "summary": "S", "tags": ["ui"],
        "connections": [{"relation": "uses", "target": "X"}],
    })
    assert captured["content_sha"] == "sha-new"

    with pytest.raises(Exception, match="approval"):
        call("brain_approve", {"note_id": "note-aaaaaaaaaaaa", "content_sha": "sha-1"})

    approved = call("brain_approve", {
        "note_id": "note-aaaaaaaaaaaa", "content_sha": "sha-1", "user_approved": True})
    assert approved["status"] == "approved"


def test_read_tools_return_json(api) -> None:
    assert call("brain_find", {"query": "q"})["results"][0]["title"] == "Hit"
    assert call("brain_inbox", {})["count"] == 1
    assert call("brain_list_tags", {})["tags"][0]["name"] == "ui"
    assert call("brain_open", {"path": "Notes/A.md"})["obsidian_uri"]
    assert call("brain_digest", {})["inbox_count"] == 1


def test_reader_mode_write_tool_returns_read_only_error(tmp_path: Path, monkeypatch) -> None:
    reader = BrainAPI(tmp_path, service=FakeService(tmp_path), index=FakeIndex(), mode="reader")
    monkeypatch.setattr(mcp_server, "_api", lambda: reader)
    assert call("brain_capture", {"content_or_url": "x"}) == {
        "error": "read-only: writes go through the brain service"
    }


def test_bearer_middleware_rejects_missing_or_wrong_token() -> None:
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = mcp_server.BearerTokenMiddleware(downstream, "secret")

    async def scenario() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return [
                (await client.post("/mcp")).status_code,
                (await client.post("/mcp", headers={"Authorization": "Bearer nope"})).status_code,
                (await client.post("/mcp", headers={"Authorization": "secret"})).status_code,
                (await client.post("/mcp", headers={"Authorization": "Bearer secret"})).status_code,
            ]

    assert anyio.run(scenario) == [401, 401, 401, 200]


def test_http_app_is_unwrapped_without_a_token() -> None:
    assert not isinstance(mcp_server.http_app(None), mcp_server.BearerTokenMiddleware)
    assert isinstance(mcp_server.http_app("t"), mcp_server.BearerTokenMiddleware)


def test_writer_startup_promotes_once_and_starts_worker(api, monkeypatch) -> None:
    started = []

    class Worker:
        def __init__(self, index, embedder):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr(mcp_server, "EmbeddingWorker", Worker)
    api.embedder = object()

    mcp_server._start_writer_background()

    assert api.service.names().count("promote_reviewed") == 1
    assert started == [True]


def test_reader_startup_does_nothing(tmp_path: Path, monkeypatch) -> None:
    reader = BrainAPI(tmp_path, service=FakeService(tmp_path), index=FakeIndex(), mode="reader")
    monkeypatch.setattr(mcp_server, "_api", lambda: reader)
    mcp_server._start_writer_background()
    assert reader.service.names() == []
