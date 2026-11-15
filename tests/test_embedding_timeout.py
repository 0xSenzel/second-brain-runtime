from __future__ import annotations

import json
from io import BytesIO

from second_brain import index as index_module
from second_brain.index import BrainIndex, OllamaEmbedder
from second_brain.service import BrainService


def test_embedding_request_uses_sixty_second_timeout(monkeypatch) -> None:
    timeouts: list[int] = []

    def fake_urlopen(request, *, timeout: int):
        timeouts.append(timeout)
        return BytesIO(json.dumps({"embedding": [0.25, 0.5]}).encode())

    monkeypatch.setattr(index_module.urllib.request, "urlopen", fake_urlopen)

    vector = OllamaEmbedder(url="http://ollama:11434").embed("synthetic probe")

    assert vector == [0.25, 0.5]
    assert timeouts == [60]


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

    monkeypatch.setattr(index_module.urllib.request, "urlopen", fake_urlopen)

    assert OllamaEmbedder(url="http://ollama:11434").available()
    assert requests == [("http://ollama:11434/api/tags", 2)]


def test_lexical_and_graph_retrieval_work_after_embedding_timeout(tmp_path) -> None:
    class TinyEmbedder:
        model = "bge-m3"

        def embed(self, _text: str) -> list[float]:
            return [1.0] * 32

    class OfflineEmbedder:
        model = "bge-m3"

        def __init__(self) -> None:
            self.calls = 0

        def available(self) -> bool:
            return False

        def embed(self, _text: str) -> list[float]:
            self.calls += 1
            raise TimeoutError("simulated Ollama timeout")

    def save_note(service: BrainService, *, title: str, content: str, connections=()) -> None:
        proposal = service.prepare_save(
            note_type="concept",
            domain="tech",
            content=content,
            title=title,
            summary=content,
            tags=["testing", "fixture"],
            aliases=[title + " alt", title + " alias"],
            maps=["Test Map"],
            connections=list(connections),
        )
        service.commit_save(proposal.id)

    vault = tmp_path / "vault"
    service = BrainService(vault)
    save_note(
        service,
        title="UI Design",
        content="Visual hierarchy and accessibility improve interface clarity.",
    )
    save_note(
        service,
        title="Frontend Critique Skill",
        content="A repeatable interface review workflow.",
        connections=[{"relation": "uses", "target": "UI Design", "reason": "review reference"}],
    )

    index = BrainIndex(vault, embedder=TinyEmbedder())
    index.reindex()
    offline = OfflineEmbedder()
    index.embedder = offline

    failed_semantic = index.context("qzparaphrase-9271", hops=1)
    hits = index.search("visual hierarchy accessibility", limit=5, hops=1)

    assert offline.calls == 1
    assert any("Semantic fallback failed" in warning for warning in failed_semantic["warnings"])
    assert hits[0].title == "UI Design"
    assert any(hit.title == "Frontend Critique Skill" and hit.via_graph for hit in hits)
    assert offline.calls == 1
