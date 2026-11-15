"""The stdio relay reaches a real HTTP writer and never opens a vault itself."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from second_brain.relay import read_token


def test_read_token_accepts_dotenv_raw_and_environment(tmp_path: Path, monkeypatch) -> None:
    dotenv = tmp_path / "a.env"
    dotenv.write_text("# comment\nOTHER=x\nSECOND_BRAIN_HTTP_TOKEN='abc'\n")
    raw = tmp_path / "raw"
    raw.write_text("rawtoken\n")
    monkeypatch.setenv("SECOND_BRAIN_HTTP_TOKEN", "fromenv")

    assert read_token(str(dotenv)) == "abc"
    assert read_token(str(raw)) == "rawtoken"
    assert read_token(None) == "fromenv"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def writer(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("SECOND_BRAIN_VAULT", str(vault))
    monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", str(tmp_path / "index.sqlite3"))
    from second_brain import mcp_server

    mcp_server._api_for.cache_clear()
    mcp_server._api().register_tags(["agents"], {"agents": "AI agents"})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mcp_server.http_app("s3cret"), port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield port, vault
    server.should_exit = True
    thread.join(timeout=5)
    mcp_server._api_for.cache_clear()


def test_relay_forwards_tool_calls_with_the_token(writer, tmp_path: Path) -> None:
    port, _vault = writer
    token_file = tmp_path / "token.env"
    token_file.write_text("SECOND_BRAIN_HTTP_TOKEN=s3cret\n")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "second_brain.mcp_server", "--connect", f"http://127.0.0.1:{port}/mcp",
              "--token-file", str(token_file)],
        # A vault path the relay must never create: proof it does not open a vault.
        env={"SECOND_BRAIN_VAULT": str(tmp_path / "must-not-exist"), "PATH": "/usr/bin:/bin"},
    )

    async def call() -> dict:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("brain_list_tags", {})
                return json.loads(result.content[0].text)

    tags = anyio.run(call)

    assert [tag["name"] for tag in tags["tags"]] == ["agents"]
    assert not (tmp_path / "must-not-exist").exists()
