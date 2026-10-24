from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / "src"))


def _uv_command() -> str:
    configured = os.environ.get("SECOND_BRAIN_UV")
    if configured:
        return configured
    command = shutil.which("uv")
    if command:
        return command
    raise RuntimeError("uv was not found on PATH; set SECOND_BRAIN_UV to its executable path")


def _seed(vault: Path) -> None:
    from second_brain.service import BrainService

    service = BrainService(vault)
    proposal = service.prepare_save(
        content="A compact approved fixture for MCP transport measurement.",
        note_type="concept",
        domain="tech",
        title="UI Microinteractions",
        summary="A guide to interface feedback, transitions, and responsive states.",
        body=(
            "## Details\n\nUse immediate visual feedback for actions. Keep transitions short "
            "and respect reduced motion."
        ),
        tags=["ui", "design"],
        aliases=["microinteraction patterns", "微交互设计指南"],
        maps=["UI Design"],
    )
    service.commit_save(proposal.id)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


async def _measure(vault: Path, cache: Path, rounds: int) -> dict[str, Any]:
    server_env = dict(os.environ)
    server_env["SECOND_BRAIN_VAULT"] = str(vault)
    server_env["UV_CACHE_DIR"] = str(cache)
    parameters = StdioServerParameters(
        command=_uv_command(),
        args=["run", "--directory", str(CODE_ROOT), "second-brain-mcp"],
        env=server_env,
    )
    find_latencies: list[float] = []
    read_latencies: list[float] = []
    flow_latencies: list[float] = []
    tool_text_bytes: list[int] = []
    body_retrieved = False

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            for _ in range(rounds):
                flow_started = time.perf_counter()
                find_started = time.perf_counter()
                found_result = await session.call_tool(
                    "brain_find", {"query": "UI microinteractions feedback transitions", "limit": 5}
                )
                find_latencies.append((time.perf_counter() - find_started) * 1000)
                if found_result.isError:
                    raise RuntimeError(found_result.content[0].text)
                found = json.loads(found_result.content[0].text)
                hit = next(
                    (item for item in found["results"] if item["title"] == "UI Microinteractions"),
                    None,
                )
                if hit is None:
                    raise AssertionError("MCP find did not return the fixture note")

                read_started = time.perf_counter()
                read_result = await session.call_tool(
                    "brain_read", {"note_id": hit["id"], "max_chars": 12000}
                )
                read_latencies.append((time.perf_counter() - read_started) * 1000)
                if read_result.isError:
                    raise RuntimeError(read_result.content[0].text)
                note = json.loads(read_result.content[0].text)
                if note["path"] != hit["path"]:
                    raise AssertionError("MCP read path did not match the found canonical path")
                body_retrieved = bool(note["body"]) and note["truncated"] is False
                tool_text_bytes.append(
                    len(found_result.content[0].text.encode("utf-8"))
                    + len(read_result.content[0].text.encode("utf-8"))
                )
                flow_latencies.append((time.perf_counter() - flow_started) * 1000)

    def summary(values: list[float]) -> dict[str, float]:
        return {
            "p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(_percentile(values, 0.95), 2),
        }

    return {
        "launcher": "uv run --directory <checkout> second-brain-mcp",
        "fixture": "one approved canonical note",
        "rounds": rounds,
        "tool_calls_per_flow": 2,
        "find": summary(find_latencies),
        "read": summary(read_latencies),
        "find_then_read": summary(flow_latencies),
        "mean_tool_text_bytes_per_flow": round(statistics.mean(tool_text_bytes)),
        "body_retrieved_untruncated": body_retrieved,
    }


def evaluate(rounds: int = 10) -> dict[str, Any]:
    if rounds < 1:
        raise ValueError("rounds must be positive")
    with tempfile.TemporaryDirectory(prefix="second-brain-mcp-bench-") as temp_dir:
        root = Path(temp_dir)
        vault = root / "vault"
        _seed(vault)
        return anyio.run(_measure, vault, root / "uv-cache", rounds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure MCP find/read round trips locally.")
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    print(json.dumps(evaluate(args.rounds), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
