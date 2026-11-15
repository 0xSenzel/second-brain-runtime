from __future__ import annotations

import json
import time
from io import BytesIO

from test_retrieval import TinyEmbedder, write_note

from second_brain import embedding as embedding_module
from second_brain import index as index_module
from second_brain.embedding import OllamaEmbedder
from second_brain.index import BrainIndex


def test_embedding_request_uses_sixty_second_timeout(monkeypatch) -> None:
    timeouts: list[int] = []

    def fake_urlopen(request, *, timeout: int):
        timeouts.append(timeout)
        return BytesIO(json.dumps({"embedding": [0.25, 0.5]}).encode())

    monkeypatch.setattr(embedding_module.urllib.request, "urlopen", fake_urlopen)

    vector = OllamaEmbedder(url="http://ollama:11434").embed("synthetic probe")

    assert vector == [0.25, 0.5]
    assert timeouts == [60]


def test_index_module_still_exports_the_embedder_names() -> None:
    assert index_module.OllamaEmbedder is OllamaEmbedder
    assert index_module.EMBEDDING_TIMEOUT_SECONDS == 60


def test_available_keeps_two_second_tags_probe(monkeypatch) -> None:
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, *, timeout: int):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(embedding_module.urllib.request, "urlopen", fake_urlopen)

    assert OllamaEmbedder(url="http://ollama:11434").available()
    assert requests == [("http://ollama:11434/api/tags", 2)]


def test_lexical_and_graph_retrieval_work_after_embedding_failure(tmp_path) -> None:
    class OfflineEmbedder(TinyEmbedder):
        calls = 0

        def embed(self, text: str) -> list[float]:
            self.calls += 1
            raise TimeoutError("simulated Ollama timeout")

    vault = tmp_path / "vault"
    write_note(vault, "UI Design", details="Visual hierarchy and accessibility improve interface clarity.")
    write_note(vault, "Frontend Critique Skill", details="A repeatable interface review workflow.",
               connections=(("uses", "UI Design"),))
    index = BrainIndex(vault, embedder=TinyEmbedder())
    index.reindex(embed_vectors=True)
    offline = OfflineEmbedder()
    index.embedder = offline

    bundle = index.context("visual hierarchy accessibility", hops=1)
    hits = index.search("visual hierarchy accessibility", limit=5, hops=1)

    assert offline.calls == 2
    assert any("Vector search failed" in warning for warning in bundle["warnings"])
    assert hits[0].title == "UI Design"
    assert any(hit.title == "Frontend Critique Skill" and hit.via_graph for hit in hits)


def test_slow_query_embedding_times_out_and_falls_back_to_lexical(tmp_path, monkeypatch) -> None:
    class SlowEmbedder(TinyEmbedder):
        slow = False

        def embed(self, text: str) -> list[float]:
            if self.slow:
                time.sleep(2)
            return super().embed(text)

    vault = tmp_path / "vault"
    write_note(vault, "Slow Path", details="lexical fallback marker")
    embedder = SlowEmbedder()
    index = BrainIndex(vault, embedder=embedder)
    index.reindex(embed_vectors=True)
    embedder.slow = True
    monkeypatch.setattr(index_module, "QUERY_EMBED_TIMEOUT_SECONDS", 0.2)

    started = time.perf_counter()
    hits, warnings = index._search("lexical fallback marker", limit=5, hops=0)
    elapsed = time.perf_counter() - started

    assert hits[0].title == "Slow Path"
    assert elapsed < 1.5
    assert any("did not return" in warning for warning in warnings)
