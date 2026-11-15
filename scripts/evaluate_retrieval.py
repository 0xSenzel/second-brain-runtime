"""Retrieval evaluation.

Two ways to run it:

* `--vault PATH --queries FILE --mode lexical|hybrid [--out FILE]` scores a real vault
  (schema 4: `Notes/` and `Inbox/`) against a YAML list of queries. Each query is
  `{query: str, expected: [note titles], kind: optional label}`; an empty `expected`
  list is a no-answer query that should return nothing. Matching is by title. Results
  and query files stay outside the repository.
* With no `--vault`, it seeds a tiny synthetic schema-4 vault and runs a built-in
  lexical fixture, so the script is repeatable anywhere.

Hybrid mode needs an embedder. `--embedder docker-ollama` embeds through
`docker exec -i CONTAINER python3` against the Ollama server inside that container (for
hosts with no Ollama of their own); it only reads and computes, and never writes in the
container. `--embedder ollama` uses `OllamaEmbedder` at `--ollama-url`.

The index is built in a scratch directory (`--index-path`), never inside the vault.
`SECOND_BRAIN_CODE_ROOT` points the script at another checkout, for baselines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CODE_ROOT = Path(os.environ.get("SECOND_BRAIN_CODE_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(CODE_ROOT / "src"))

import yaml  # noqa: E402

from second_brain.index import BrainIndex  # noqa: E402

TOP_K = 5
RANK_DEPTH = 10

FIXTURE_NOTES = [
    {
        "title": "UI Microinteractions",
        "aliases": ["微交互设计指南", "interface feedback patterns"],
        "summary": "A practical guide to feedback, transitions, and responsive interface states.",
        "details": (
            "Use small, immediate visual responses to show that an action was received. "
            "Keep transitions short, preserve keyboard focus, and respect reduced motion."
        ),
    },
    {
        "title": "Accessible Interface Review",
        "aliases": ["accessibility audit", "界面无障碍检查"],
        "summary": "Review hierarchy, contrast, keyboard flow, and responsive behavior.",
        "details": "Check focus order and visible focus indicators.",
    },
]

FIXTURE_QUERIES = [
    {"kind": "exact", "query": "UI Microinteractions", "expected": ["UI Microinteractions"]},
    {
        "kind": "paraphrase",
        "query": "quick visual response confirms an action happened",
        "expected": ["UI Microinteractions"],
    },
    {"kind": "chinese", "query": "微交互设计指南", "expected": ["UI Microinteractions"]},
    {
        "kind": "body",
        "query": "How long should transitions be and what motion setting should they respect?",
        "expected": ["UI Microinteractions"],
    },
    {"kind": "no-answer", "query": "quantum navigation protocols on Mars", "expected": []},
]


def _seed_fixture(vault: Path) -> None:
    (vault / "Notes").mkdir(parents=True)
    for note in FIXTURE_NOTES:
        frontmatter = {
            "id": "note-" + hashlib.sha256(note["title"].encode()).hexdigest()[:12],
            "status": "approved", "type": "concept", "domain": "tech", "title": note["title"],
            "tags": ["ui"], "aliases": note["aliases"],
        }
        header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        body = f"# {note['title']}\n\n## Summary\n\n{note['summary']}\n\n## Details\n\n{note['details']}\n"
        (vault / "Notes" / f"{note['title']}.md").write_text(f"---\n{header}---\n\n{body}", encoding="utf-8")


class DockerOllamaEmbedder:
    """Embeds through `docker exec` into a container that can reach Ollama. Read/compute only."""

    _CODE = (
        "import sys, json, urllib.request\n"
        "model, url = sys.argv[1], sys.argv[2]\n"
        "out = []\n"
        "for text in json.load(sys.stdin):\n"
        "    body = json.dumps({'model': model, 'prompt': text[:6000], 'keep_alive': '15m'}).encode()\n"
        "    request = urllib.request.Request(url + '/api/embeddings', data=body,\n"
        "        headers={'Content-Type': 'application/json'})\n"
        "    out.append(json.load(urllib.request.urlopen(request, timeout=170))['embedding'])\n"
        "json.dump(out, sys.stdout)\n"
    )

    def __init__(
        self,
        container: str = "hermes",
        url: str = "http://ollama:11434",
        model: str = "bge-m3",
        cache_path: Path | None = None,
    ):
        self.container, self.url, self.model = container, url, model
        self.cache_path = cache_path
        self.cache: dict[str, list[float]] = {}
        if cache_path and cache_path.exists():
            self.cache = json.loads(cache_path.read_text())

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text[:6000].encode()).hexdigest()

    def _run(self, texts: list[str]) -> list[list[float]]:
        result = subprocess.run(
            ["docker", "exec", "-i", self.container, "python3", "-c", self._CODE, self.model, self.url],
            input=json.dumps(texts), capture_output=True, text=True, timeout=900, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-300:] or "docker exec failed")
        return json.loads(result.stdout)

    def embed_many(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if self._key(t) not in self.cache]
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            for text, vector in zip(batch, self._run(batch), strict=True):
                self.cache[self._key(text)] = vector
        if missing and self.cache_path:
            self.cache_path.write_text(json.dumps(self.cache))
        return [self.cache[self._key(t)] for t in texts]

    def embed(self, text: str) -> list[float]:
        # Queries are never served from the cache: latency should include the real call.
        return self._run([text])[0]


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _make_index(vault: Path, embedder: Any, index_path: Path) -> BrainIndex:
    try:
        return BrainIndex(vault, embedder=embedder, index_path=index_path)
    except TypeError:  # schema-3 baseline code: no index_path, index lives in the vault
        return BrainIndex(vault, embedder=embedder)


def evaluate(
    vault: Path,
    queries: list[dict],
    *,
    mode: str,
    embedder: Any | None,
    index_path: Path,
    rounds: int = 3,
) -> dict[str, Any]:
    if mode == "hybrid" and embedder is None:
        raise SystemExit("hybrid mode needs --embedder")
    use_embedder = embedder if mode == "hybrid" else None
    index = _make_index(vault, use_embedder, index_path)
    started = time.perf_counter()
    index.reindex(embed_vectors=False)
    embed_seconds = 0.0
    if mode == "hybrid":
        embed_started = time.perf_counter()
        pending = index.pending_chunks()
        vectors = embedder.embed_many([text for _id, text in pending])
        for (chunk_id, _text), vector in zip(pending, vectors, strict=True):
            index.store_vector(chunk_id, vector, model=embedder.model)
        embed_seconds = time.perf_counter() - embed_started
    index_seconds = time.perf_counter() - started

    cases: list[dict[str, Any]] = []
    latencies: list[float] = []
    reciprocal: list[float] = []
    top1: list[int] = []
    recall: list[float] = []
    no_answer: list[int] = []
    for case in queries:
        expected = list(case.get("expected") or [])
        titles: list[str] = []
        warnings: list[str] = []
        for round_number in range(rounds):
            t0 = time.perf_counter()
            if hasattr(index, "_search") and hasattr(index, "vec_enabled"):
                hits, warnings = index._search(case["query"], limit=RANK_DEPTH, hops=0)
            else:
                hits = index.search(case["query"], limit=RANK_DEPTH, hops=0)
            latencies.append((time.perf_counter() - t0) * 1000)
            if round_number == 0:
                titles = [hit.title for hit in hits]
        if expected:
            ranks = [titles.index(t) + 1 for t in expected if t in titles]
            reciprocal.append(1 / min(ranks) if ranks else 0.0)
            top1.append(int(bool(titles) and titles[0] in expected))
            recall.append(len([t for t in expected if t in titles[:TOP_K]]) / len(expected))
        else:
            no_answer.append(int(not titles))
        cases.append(
            {
                "kind": case.get("kind", ""),
                "query": case["query"],
                "expected": expected,
                "top_results": titles[:TOP_K],
                "first_relevant_rank": (min(ranks) if expected and ranks else None),
                "warnings": warnings[:3],
            }
        )

    def mean(values: list[float]) -> float | None:
        return round(statistics.mean(values), 3) if values else None

    return {
        "mode": mode,
        "vault_notes": len(list((vault / "Notes").glob("*.md"))) if (vault / "Notes").is_dir() else None,
        "queries": len(queries),
        "answerable": len(top1),
        "no_answer_queries": len(no_answer),
        "top1": mean(top1),
        "recall_at_5": mean(recall),
        "mrr": mean(reciprocal),
        "no_answer_accuracy": mean(no_answer),
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(_percentile(latencies, 0.95), 2),
        "index_seconds": round(index_seconds, 2),
        "embed_seconds": round(embed_seconds, 2),
        "vector_backend": getattr(index, "vector_stats", lambda: {})().get("backend"),
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--vault", type=Path, help="vault to score; omit to run the built-in fixture")
    parser.add_argument("--queries", type=Path, help="YAML list of {query, expected, kind}")
    parser.add_argument("--mode", choices=["lexical", "hybrid"], default="lexical")
    parser.add_argument("--out", type=Path, help="write the JSON result here")
    parser.add_argument("--rounds", type=int, default=3, help="timing repetitions per query")
    parser.add_argument("--index-path", type=Path, help="scratch index file (default: temp dir)")
    parser.add_argument("--embedder", choices=["none", "ollama", "docker-ollama"], default="none")
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--container", default="hermes")
    parser.add_argument("--embed-cache", type=Path, help="JSON cache for index-time vectors")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    if args.vault and not args.queries:
        parser.error("--vault needs --queries")

    embedder: Any | None = None
    if args.mode == "hybrid":
        if args.embedder == "docker-ollama":
            embedder = DockerOllamaEmbedder(
                args.container, args.ollama_url or "http://ollama:11434", cache_path=args.embed_cache
            )
        elif args.embedder == "ollama":
            from second_brain.index import OllamaEmbedder

            embedder = OllamaEmbedder(args.ollama_url or "http://localhost:11434")
        else:
            parser.error("hybrid mode needs --embedder ollama|docker-ollama")

    with tempfile.TemporaryDirectory(prefix="second-brain-eval-") as scratch:
        index_path = args.index_path or Path(scratch) / "index.sqlite3"
        if args.vault:
            vault = args.vault.expanduser().resolve()
            queries = yaml.safe_load(args.queries.read_text(encoding="utf-8"))
        else:
            vault = Path(scratch) / "vault"
            _seed_fixture(vault)
            queries = FIXTURE_QUERIES
        result = evaluate(
            vault, queries, mode=args.mode, embedder=embedder, index_path=index_path, rounds=args.rounds
        )
    result["code_root"] = str(CODE_ROOT)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    summary = {k: v for k, v in result.items() if k != "cases"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
