from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CODE_ROOT = Path(os.environ.get("SECOND_BRAIN_CODE_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(CODE_ROOT / "src"))

from second_brain.api import BrainAPI  # noqa: E402
from second_brain.service import BrainService  # noqa: E402

CASES = [
    {
        "case": "known title",
        "query": "UI Microinteractions",
        "mode": "find",
        "expected_titles": ["UI Microinteractions"],
    },
    {
        "case": "paraphrase",
        "query": "quick visual response confirms an action happened",
        "mode": "find",
        "expected_titles": ["UI Microinteractions"],
    },
    {
        "case": "Chinese alias",
        "query": "微交互设计指南",
        "mode": "find",
        "expected_titles": ["UI Microinteractions"],
    },
    {
        "case": "ambiguous matching-items request",
        "query": "interface",
        "mode": "find",
        "expected_titles": ["UI Microinteractions", "Accessible Interface Review"],
    },
    {
        "case": "UI-note query + exact read",
        "query": "UI microinteractions feedback and transitions",
        "mode": "find_read",
        "expected_titles": ["UI Microinteractions"],
    },
    {
        "case": "body-only broad question",
        "query": "How long should transitions be and what motion setting should they respect?",
        "mode": "ask",
        "expected_titles": ["UI Microinteractions"],
        "expected_evidence": "Keep transitions short",
    },
    {
        "case": "no answer",
        "query": "quantum navigation protocols on Mars",
        "mode": "find",
        "expected_titles": [],
    },
]


def _seed(vault: Path) -> None:
    service = BrainService(vault)
    notes = [
        {
            "title": "UI Microinteractions",
            "note_type": "concept",
            "domain": "tech",
            "summary": "A practical guide to feedback, transitions, and responsive interface states.",
            "body": (
                "## Details\n\nUse small, immediate visual responses to show that an action was received. "
                "Keep transitions short, preserve keyboard focus, and respect reduced motion.\n\n"
                "## Examples\n\nButtons can briefly change state after activation; inline validation "
                "should explain how to recover."
            ),
            "content": "Captured UI microinteraction guidance.",
            "tags": ["ui", "design"],
            "aliases": ["微交互设计指南", "interface feedback patterns"],
            "maps": ["UI Design"],
            "source_url": "https://example.com/ui-notes",
        },
        {
            "title": "Accessible Interface Review",
            "note_type": "skill",
            "domain": "tech",
            "summary": "Review hierarchy, contrast, keyboard flow, and responsive behavior.",
            "body": "## Details\n\nCheck focus order and visible focus indicators.",
            "content": "Review checklist.",
            "tags": ["ui", "accessibility"],
            "aliases": ["accessibility audit", "界面无障碍检查"],
            "maps": ["UI Design"],
        },
    ]
    for note in notes:
        proposal = service.prepare_save(**note)
        service.commit_save(proposal.id)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def evaluate(rounds: int = 10) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="second-brain-retrieval-") as directory:
        vault = Path(directory) / "vault"
        _seed(vault)
        api = BrainAPI(vault, use_embeddings=False)
        api.reindex()
        cases: list[dict[str, Any]] = []
        all_latencies: list[float] = []
        exact_read_latencies: list[float] = []
        top1_correct = 0
        expected_retrieved = 0
        expected_total = 0
        no_answer_correct = 0
        no_answer_cases = 0
        answer_evidence_hits = 0
        answer_evidence_cases = 0

        for case in CASES:
            label = case["case"]
            query = case["query"]
            mode = case["mode"]
            expected_titles = case["expected_titles"]
            latencies: list[float] = []
            byte_counts: list[int] = []
            tool_calls = 2 if mode == "find_read" else 1
            top_result: str | None = None
            case_top1_correct = 0
            case_expected_retrieved = 0
            case_expected_total = len(expected_titles) * rounds
            case_no_answer_correct = 0
            case_answer_evidence_hits = 0
            for _ in range(rounds):
                started = time.perf_counter()
                if mode == "ask":
                    answer = api.ask(query, max_tokens=4000)
                    results = answer["sources"]
                    payloads: list[Any] = [answer]
                    evidence_text = answer["context"]
                else:
                    found = api.find(query, limit=5)
                    results = found["results"]
                    payloads = [found]
                    evidence_text = "\n".join(result["excerpt"] for result in results)
                titles = [result["title"] for result in results]
                top_result = titles[0] if titles else None
                expected = set(expected_titles)
                if expected:
                    case_top1_correct += int(top_result in expected)
                    case_expected_retrieved += len(expected.intersection(titles))
                    expected_retrieved += len(expected.intersection(titles))
                    expected_total += len(expected)
                else:
                    case_no_answer_correct += int(not results)
                    no_answer_correct += int(not results)
                    no_answer_cases += 1
                expected_evidence = case.get("expected_evidence")
                if expected_evidence:
                    answer_evidence_cases += 1
                    if expected_evidence in evidence_text:
                        case_answer_evidence_hits += 1
                        answer_evidence_hits += 1
                if mode == "find_read" and results:
                    read_started = time.perf_counter()
                    note = api.read(results[0]["id"])
                    read_elapsed_ms = (time.perf_counter() - read_started) * 1000
                    exact_read_latencies.append(read_elapsed_ms)
                    payloads.append(note)
                    body_retrieved = bool(note.get("body")) and note["truncated"] is False
                else:
                    body_retrieved = False
                elapsed_ms = (time.perf_counter() - started) * 1000
                latencies.append(elapsed_ms)
                byte_counts.append(len(json.dumps(payloads, ensure_ascii=False).encode("utf-8")))
            all_latencies.extend(latencies)
            if expected_titles:
                top1_correct += case_top1_correct
            cases.append(
                {
                    "case": label,
                    "query": query,
                    "p50_ms": round(statistics.median(latencies), 2),
                    "p95_ms": round(_percentile(latencies, 0.95), 2),
                    "tool_calls": tool_calls,
                    "mean_response_bytes": round(statistics.mean(byte_counts)),
                    "top_result": top_result,
                    "top1_accuracy": (
                        round(case_top1_correct / rounds, 3) if expected_titles else None
                    ),
                    "recall_at_5": (
                        round(case_expected_retrieved / case_expected_total, 3)
                        if case_expected_total
                        else None
                    ),
                    "answer_evidence_rate": (
                        round(case_answer_evidence_hits / rounds, 3)
                        if case.get("expected_evidence")
                        else None
                    ),
                    "no_answer_accuracy": (
                        round(case_no_answer_correct / rounds, 3)
                        if not expected_titles
                        else None
                    ),
                    "body_retrieved_untruncated": body_retrieved,
                }
            )
        return {
            "fixture": "two approved canonical notes; lexical-only mode",
            "rounds_per_case": rounds,
            "cases": cases,
            "overall_p50_ms": round(statistics.median(all_latencies), 2),
            "overall_p95_ms": round(_percentile(all_latencies, 0.95), 2),
            "exact_read_p50_ms": round(statistics.median(exact_read_latencies), 2)
            if exact_read_latencies
            else None,
            "exact_read_p95_ms": round(_percentile(exact_read_latencies, 0.95), 2)
            if exact_read_latencies
            else None,
            "top1_accuracy": round(
                top1_correct / (sum(bool(case["expected_titles"]) for case in CASES) * rounds),
                3,
            ),
            "recall_at_5": round(expected_retrieved / expected_total, 3)
            if expected_total
            else None,
            "answer_evidence_rate": round(answer_evidence_hits / answer_evidence_cases, 3)
            if answer_evidence_cases
            else None,
            "no_answer_accuracy": round(no_answer_correct / no_answer_cases, 3)
            if no_answer_cases
            else None,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the repeatable local retrieval fixture.")
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    print(json.dumps(evaluate(args.rounds), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
