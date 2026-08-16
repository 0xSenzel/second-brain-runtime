from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .notes import parse_note

SKIP_DIRS = {".git", ".obsidian", ".second-brain", "Inbox", "Reviews", "Templates"}
CONNECTION_RE = re.compile(
    r"(?m)^-\s+(about|created-by|contradicts|derived-from|requires|similar-to|supports|uses):\s+\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\](?:\s+—\s+(.+))?$",
    re.IGNORECASE,
)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
HEADING_RE = re.compile(r"(?m)^#{1,3}\s+(.+?)\s*$")
TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)


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
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read())
        vector = payload.get("embedding")
        if not vector:
            raise RuntimeError(f"Ollama returned no embedding for model {self.model!r}")
        return [float(value) for value in vector]


@dataclass
class SearchHit:
    id: str
    title: str
    path: str
    note_type: str
    score: float
    excerpt: str
    source_url: str = ""
    via_graph: bool = False
    relation: str = ""
    related_from: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "type": self.note_type,
            "score": round(self.score, 6),
            "excerpt": self.excerpt,
            "source_url": self.source_url,
            "via_graph": self.via_graph,
            "relation": self.relation,
            "related_from": self.related_from,
        }


class BrainIndex:
    def __init__(self, vault: str | Path, *, embedder: Embedder | None = None):
        self.vault = Path(vault).expanduser().resolve()
        self.db_path = self.vault / ".second-brain" / "index.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    note_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    vector TEXT,
                    UNIQUE(node_id, chunk_index)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    node_id UNINDEXED,
                    text,
                    tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS edges (
                    source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                    target_title TEXT NOT NULL,
                    target_id TEXT,
                    relation TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS edges_source ON edges(source_id);
                CREATE INDEX IF NOT EXISTS edges_target ON edges(target_id);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def reindex(self) -> dict[str, int | str]:
        notes = self._scan_notes()
        embedded_chunks = 0
        indexed_notes = len(notes)
        with self._connect() as db:
            embedding_model = getattr(self.embedder, "model", "disabled")
            stored_model_row = db.execute(
                "SELECT value FROM metadata WHERE key='embedding_model'"
            ).fetchone()
            stored_model = stored_model_row["value"] if stored_model_row else ""
            missing_vectors = db.execute(
                "SELECT 1 FROM chunks WHERE vector IS NULL LIMIT 1"
            ).fetchone()
            refresh_vectors = self.embedder is not None and (
                stored_model != embedding_model or missing_vectors is not None
            )
            existing = {
                row["path"]: (row["id"], row["content_hash"])
                for row in db.execute("SELECT id,path,content_hash FROM nodes")
            }
            current_paths = set(notes)
            deleted_paths = set(existing) - current_paths
            for path in deleted_paths:
                self._delete_node(db, existing[path][0])

            for path, note in notes.items():
                old = existing.get(path)
                if old and old[1] == note["content_hash"] and not refresh_vectors:
                    continue
                if old:
                    self._delete_node(db, old[0])
                db.execute(
                    "INSERT INTO nodes"
                    "(id,path,title,note_type,content,summary,source_url,content_hash) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        note["id"],
                        path,
                        note["title"],
                        note["note_type"],
                        note["content"],
                        note["summary"],
                        note["source_url"],
                        note["content_hash"],
                    ),
                )
                for chunk_index, chunk in enumerate(_chunks(note["content"])):
                    vector = None
                    if self.embedder is not None:
                        vector = json.dumps(self.embedder.embed(f"{note['title']}\n{chunk}"))
                        embedded_chunks += 1
                    cursor = db.execute(
                        "INSERT INTO chunks(node_id,chunk_index,text,vector) VALUES(?,?,?,?)",
                        (note["id"], chunk_index, chunk, vector),
                    )
                    db.execute(
                        "INSERT INTO chunks_fts(chunk_id,node_id,text) VALUES(?,?,?)",
                        (cursor.lastrowid, note["id"], chunk),
                    )

            db.execute("DELETE FROM edges")
            title_map: dict[str, str] = {}
            for row in db.execute("SELECT id,title,path FROM nodes"):
                title_map[_normalize_target(row["title"])] = row["id"]
                title_map[_normalize_target(Path(row["path"]).stem)] = row["id"]
            for note in notes.values():
                if db.execute("SELECT 1 FROM nodes WHERE id=?", (note["id"],)).fetchone() is None:
                    continue
                seen: set[tuple[str, str]] = set()
                for relation, target, reason in _connections(note["content"]):
                    key = (relation, _normalize_target(target))
                    if key in seen:
                        continue
                    seen.add(key)
                    db.execute(
                        "INSERT INTO edges"
                        "(source_id,target_title,target_id,relation,reason) "
                        "VALUES(?,?,?,?,?)",
                        (note["id"], target, title_map.get(key[1]), relation, reason),
                    )
            db.execute(
                "INSERT INTO metadata(key,value) VALUES('embedding_model',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (embedding_model,),
            )
            db.commit()
        return {
            "indexed_notes": indexed_notes,
            "embedded_chunks": embedded_chunks,
            "deleted_notes": len(deleted_paths),
            "embedding_model": embedding_model,
        }

    def search(self, query: str, *, limit: int = 6, hops: int = 1) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        with self._connect() as db:
            nodes = {row["id"]: row for row in db.execute("SELECT * FROM nodes")}
            chunk_rows = list(db.execute("SELECT id,node_id,text,vector FROM chunks"))
            scores: dict[str, float] = {}
            excerpts: dict[str, str] = {}

            lexical = self._lexical(db, query, limit=max(20, limit * 4))
            for rank, row in enumerate(lexical, 1):
                node_id = row["node_id"]
                scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (60 + rank)
                excerpts.setdefault(node_id, row["text"])

            if self.embedder is not None:
                query_vector = self.embedder.embed(query)
                semantic = sorted(
                    (
                        (_cosine(query_vector, json.loads(row["vector"])), row)
                        for row in chunk_rows
                        if row["vector"]
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )[: max(20, limit * 4)]
                for rank, (similarity, row) in enumerate(semantic, 1):
                    if similarity <= 0:
                        continue
                    node_id = row["node_id"]
                    scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (60 + rank)
                    excerpts.setdefault(node_id, row["text"])

            for node_id in list(scores):
                note_type = nodes[node_id]["note_type"]
                if note_type == "map":
                    # A map lists the title and summary of every note it indexes, so it
                    # matches almost any query about its subject and would crowd out the
                    # notes themselves. Keep it findable for orientation, below them.
                    scores[node_id] -= 0.03
                elif note_type != "source":
                    # Distilled notes are the default answer surface. Raw sources
                    # remain searchable and citable, but should not displace the
                    # reviewed summary when both match the same query.
                    scores[node_id] += 0.05

            graph_meta: dict[str, tuple[str, str, str]] = {}
            if hops > 0:
                ranked_seeds = sorted(
                    scores.items(), key=lambda item: item[1], reverse=True
                )[:limit]
                seed_ids = [item[0] for item in ranked_seeds]
                for seed_id in seed_ids:
                    seed_score = scores[seed_id]
                    for edge in db.execute(
                        "SELECT source_id,target_id,relation,reason FROM edges "
                        "WHERE source_id=? OR target_id=?",
                        (seed_id, seed_id),
                    ):
                        neighbor = edge["target_id"] if edge["source_id"] == seed_id else edge["source_id"]
                        if not neighbor or neighbor == seed_id or neighbor not in nodes:
                            continue
                        if nodes[neighbor]["note_type"] == "map":
                            # Every note on a map links to it, so expanding through one
                            # would make all of them look related to each other.
                            continue
                        graph_score = seed_score * 0.55
                        graph_meta.setdefault(
                            neighbor,
                            (edge["relation"], nodes[seed_id]["title"], edge["reason"]),
                        )
                        if graph_score > scores.get(neighbor, 0.0):
                            scores[neighbor] = graph_score
                            excerpts.setdefault(
                                neighbor,
                                nodes[neighbor]["summary"] or nodes[neighbor]["content"][:700],
                            )

            ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
            hits: list[SearchHit] = []
            for node_id, score in ranked:
                node = nodes[node_id]
                graph = graph_meta.get(node_id)
                hits.append(
                    SearchHit(
                        id=node_id,
                        title=node["title"],
                        path=node["path"],
                        note_type=node["note_type"],
                        score=score,
                        excerpt=_clean_excerpt(excerpts.get(node_id) or node["summary"] or node["content"]),
                        source_url=node["source_url"],
                        via_graph=graph is not None,
                        relation=graph[0] if graph else "",
                        related_from=graph[1] if graph else "",
                    )
                )
            return hits

    def context(self, query: str, *, max_tokens: int = 2500, hops: int = 1, limit: int = 8) -> dict:
        if max_tokens < 32:
            raise ValueError("max_tokens must be at least 32")
        hits = self.search(query, limit=limit, hops=hops)
        char_budget = int(max_tokens * 3.5)
        blocks: list[str] = []
        used = 0
        included: list[SearchHit] = []
        for index, hit in enumerate(hits, 1):
            relation = f"\nRelated through: {hit.relation} from {hit.related_from}" if hit.via_graph else ""
            source = f"\nOriginal URL: {hit.source_url}" if hit.source_url else ""
            block = (
                f"[{index}] {hit.title} ({hit.note_type})\n"
                f"Obsidian note: {hit.path}{source}{relation}\n"
                f"Excerpt: {hit.excerpt}\n"
            )
            remaining = char_budget - used
            if remaining <= 0:
                break
            if len(block) > remaining:
                if not blocks:
                    block = block[: max(0, remaining - 1)] + "…"
                else:
                    break
            blocks.append(block)
            included.append(hit)
            used += len(block)
        context = "\n".join(blocks)
        return {
            "query": query,
            "context": context,
            "estimated_tokens": math.ceil(len(context) / 3.5),
            "sources": [hit.as_dict() for hit in included],
            "warnings": []
            if self.embedder is not None
            else ["Semantic search unavailable; using lexical and graph retrieval."],
        }

    def neighbors(self, note_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT e.relation,e.reason,e.source_id,e.target_id,
                       s.title AS source_title,s.path AS source_path,
                       t.title AS target_title,t.path AS target_path
                FROM edges e
                JOIN nodes s ON s.id=e.source_id
                LEFT JOIN nodes t ON t.id=e.target_id
                WHERE e.source_id=? OR e.target_id=?
                """,
                (note_id, note_id),
            )
            return [dict(row) for row in rows]

    def _scan_notes(self) -> dict[str, dict]:
        notes: dict[str, dict] = {}
        for path in sorted(self.vault.rglob("*.md")):
            relative = path.relative_to(self.vault)
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            metadata, body = parse_note(text)
            if metadata.get("status") != "approved":
                continue
            title_match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
            title = str(metadata.get("title") or (title_match.group(1) if title_match else path.stem))
            note_id = str(metadata.get("id") or _stable_id(relative.as_posix()))
            summary = _section(body, "Summary")
            notes[relative.as_posix()] = {
                "id": note_id,
                "title": title,
                "note_type": str(metadata.get("type") or "note"),
                "content": text,
                "summary": summary,
                "source_url": str(metadata.get("source_url") or ""),
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
        return notes

    def _delete_node(self, db: sqlite3.Connection, node_id: str) -> None:
        chunk_ids = [row[0] for row in db.execute("SELECT id FROM chunks WHERE node_id=?", (node_id,))]
        for chunk_id in chunk_ids:
            db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,))
        db.execute("DELETE FROM nodes WHERE id=?", (node_id,))

    def _lexical(self, db: sqlite3.Connection, query: str, *, limit: int) -> list[sqlite3.Row]:
        tokens = [token for token in TOKEN_RE.findall(query.lower()) if len(token) > 1]
        if not tokens:
            return []
        expression = " OR ".join(f'"{token.replace(chr(34), "")}"*' for token in tokens[:12])
        return list(
            db.execute(
                "SELECT node_id,text,bm25(chunks_fts) AS rank "
                "FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                (expression, limit),
            )
        )


def _chunks(text: str, size: int = 1200) -> list[str]:
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(current) + len(paragraph) + 2 <= size:
            current = f"{current}\n\n{paragraph}".strip()
        else:
            if current:
                pieces.append(current)
            while len(paragraph) > size:
                pieces.append(paragraph[:size])
                paragraph = paragraph[size:]
            current = paragraph
    if current:
        pieces.append(current)
    return pieces


def _connections(text: str) -> list[tuple[str, str, str]]:
    typed: list[tuple[str, str, str]] = [
        (match.group(1).lower(), match.group(2).strip(), (match.group(3) or "").strip())
        for match in CONNECTION_RE.finditer(text)
    ]
    typed_targets = {_normalize_target(target) for _, target, _ in typed}
    generic = [
        ("related", target.strip(), "")
        for target in WIKILINK_RE.findall(text)
        if _normalize_target(target) not in typed_targets
    ]
    return typed + generic


def _section(body: str, heading: str) -> str:
    match = re.search(rf"(?ms)^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)", body)
    return _clean_excerpt(match.group(1)) if match else ""


def _clean_excerpt(text: str, limit: int = 700) -> str:
    cleaned = re.sub(r"---.*?---", "", text, count=1, flags=re.DOTALL)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _normalize_target(value: str) -> str:
    return Path(value).stem.casefold().strip()


def _stable_id(value: str) -> str:
    return "note-" + hashlib.sha256(value.encode()).hexdigest()[:16]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0
