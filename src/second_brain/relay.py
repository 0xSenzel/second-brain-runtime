"""Stdio-to-HTTP relay, so a stdio-only MCP client can reach the single vault writer.

Claude Code and Codex start MCP servers as child processes. Pointing them at the HTTP
writer directly would put the bearer token in their configuration files or require it in
the environment of a GUI app. The relay keeps the token in one file (mode 600) and gives
the clients a plain command line instead. It forwards protocol messages unchanged; it
never opens the vault, so it cannot become a second writer.
"""

from __future__ import annotations

import os
from pathlib import Path

import anyio
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

TOKEN_VARIABLE = "SECOND_BRAIN_HTTP_TOKEN"


def read_token(token_file: str | None) -> str | None:
    """The bearer token from a dotenv-style or raw file, else the environment."""
    if token_file:
        for line in Path(token_file).expanduser().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            if not sep:
                return line
            if name.strip() == TOKEN_VARIABLE:
                return value.strip().strip("\"'") or None
        return None
    return os.environ.get(TOKEN_VARIABLE, "").strip() or None


async def relay(url: str, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with stdio_server() as (from_client, to_client):
        async with streamablehttp_client(url, headers=headers) as (from_server, to_server, _):
            async with anyio.create_task_group() as group:

                async def pump(source, sink) -> None:
                    async with sink:
                        async for message in source:
                            # Transport errors arrive as exceptions in the stream; the
                            # peer sees the failed request time out rather than a crash.
                            if isinstance(message, Exception):
                                continue
                            await sink.send(message)
                    group.cancel_scope.cancel()

                group.start_soon(pump, from_client, to_server)
                group.start_soon(pump, from_server, to_client)


def run(url: str, token_file: str | None) -> None:
    anyio.run(relay, url, read_token(token_file))
