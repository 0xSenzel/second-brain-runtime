from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_mcp_exposes_review_first_workflow(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = dict(os.environ)
        env["SECOND_BRAIN_VAULT"] = str(tmp_path / "vault")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "second_brain.mcp_server"],
            env=env,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {
                    "brain_prepare_save", "brain_commit_save", "brain_ask", "brain_open", "brain_list_tags"
                } <= names

                registry_path = tmp_path / "vault" / "Maps" / "Tag Registry.md"
                empty_tags = await session.call_tool("brain_list_tags", {})
                assert json.loads(empty_tags.content[0].text)["tags"] == []
                await session.call_tool(
                    "brain_register_tags",
                    {
                        "tags_json": '["hypertrophy", "training-intensity"]',
                        "meanings_json": json.dumps({
                            "hypertrophy": "Muscle growth.",
                            "training-intensity": "Training effort.",
                        }),
                    },
                )
                before = registry_path.read_text(encoding="utf-8")
                listed = json.loads((await session.call_tool("brain_list_tags", {})).content[0].text)
                assert listed["tags"] == [
                    {"name": "hypertrophy", "meaning": "Muscle growth."},
                    {"name": "training-intensity", "meaning": "Training effort."},
                ]
                assert registry_path.read_text(encoding="utf-8") == before

                prepared = await session.call_tool(
                    "brain_prepare_save",
                    {
                        "content_or_url": "A prompt for testing interfaces.",
                        "note_type": "prompt",
                        "body": "## Prompt\n\nReview the interface.",
                        "title": "Interface Test",
                        "summary": "Checks interface behavior.",
                    },
                )
                payload = json.loads(prepared.content[0].text)
                assert payload["approval_required"] is True
                assert payload["body"] == "## Prompt\n\nReview the interface."
                assert payload["note_type"] == "prompt"
                assert not (tmp_path / "vault" / "Prompts" / "Interface Test.md").exists()

    anyio.run(scenario)
