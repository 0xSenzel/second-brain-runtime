"""Embedding clients and the background worker that fills in missing vectors."""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .index import BrainIndex

EMBEDDING_TIMEOUT_SECONDS = 60

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    model: str

    def embed(self, text: str) -> list[float]: ...


class OllamaEmbedder:
    def __init__(self, url: str = "http://localhost:11434", model: str = "bge-m3"):
        self.url = url.rstrip("/")
        self.model = model

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=2) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"model": self.model, "prompt": text[:6000], "keep_alive": "15m"}).encode()
        request = urllib.request.Request(
            f"{self.url}/api/embeddings", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=EMBEDDING_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
        vector = payload.get("embedding")
        if not vector:
            raise RuntimeError(f"Ollama returned no embedding for model {self.model!r}")
        return [float(value) for value in vector]


class EmbeddingWorker:
    """Embeds chunks that have no vector, a few at a time, on a daemon thread.

    Failures never propagate: the worker records the error, backs off exponentially
    (interval * 2**failures, capped), and tries again. Lexical search never waits on it.
    """

    def __init__(
        self,
        index: BrainIndex,
        embedder: Embedder,
        interval: float = 30.0,
        *,
        batch_size: int = 8,
        max_backoff: float = 900.0,
    ):
        self.index = index
        self.embedder = embedder
        self.interval = interval
        self.batch_size = batch_size
        self.max_backoff = max_backoff
        self.failures = 0
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> int:
        """Embed one batch of pending chunks. Returns how many were stored."""
        self.index.prepare_embedding(self.embedder.model)
        stored = 0
        pending = self.index.pending_chunks(self.batch_size)
        if not pending:
            self.failures = 0
            return 0
        for chunk_id, text in pending:
            try:
                vector = self.embedder.embed(text)
            except Exception as error:  # semantic outages must not stop the worker
                self.failures += 1
                self.last_error = str(error)
                logger.warning("embedding failed: %s", error)
                return stored
            if self.index.store_vector(chunk_id, vector, model=self.embedder.model):
                stored += 1
        self.failures = 0
        self.last_error = ""
        return stored

    def next_delay(self) -> float:
        if self.failures:
            return min(self.interval * (2 ** min(self.failures, 20)), self.max_backoff)
        return self.interval

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                stored = self.run_once()
            except Exception as error:  # e.g. the database is briefly locked
                self.failures += 1
                self.last_error = str(error)
                stored = 0
            # More work waiting and no failure: keep going without sleeping.
            delay = 0.0 if stored and not self.failures else self.next_delay()
            self._stop.wait(delay)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="embedding-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
