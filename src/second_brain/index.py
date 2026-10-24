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

from .models import DOMAIN_FOLDER, FOLDER_FOR_TYPE
from .notes import parse_note

SKIP_DIRS = {".git", ".obsidian", ".second-brain", "Inbox", "Reviews", "Templates"}
CONNECTION_RE = re.compile(
    r"(?m)^-\s+(about|created-by|contradicts|derived-from|requires|similar-to|supports|uses):\s+\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\](?:\s+—\s+(.+))?$",
    re.IGNORECASE,
)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
HEADING_RE = re.compile(r"(?m)^#{1,3}\s+(.+?)\s*$")
TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)
INDEX_VERSION = "3"
EMBEDDING_TIMEOUT_SECONDS = 5


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
    match_location: tuple[str, ...] = ()
    evidence: str = ""

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
            "match_location": list(self.match_location),
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
                    content_hash TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    ctime_ns INTEGER NOT NULL DEFAULT 0,
                    inode INTEGER NOT NULL DEFAULT 0
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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(nodes)")}
            if "mtime_ns" not in columns:
                db.execute("ALTER TABLE nodes ADD COLUMN mtime_ns INTEGER NOT NULL DEFAULT 0")
            if "file_size" not in columns:
                db.execute("ALTER TABLE nodes ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0")
            if "ctime_ns" not in columns:
                db.execute("ALTER TABLE nodes ADD COLUMN ctime_ns INTEGER NOT NULL DEFAULT 0")
            if "inode" not in columns:
                db.execute("ALTER TABLE nodes ADD COLUMN inode INTEGER NOT NULL DEFAULT 0")

    def reindex(self, *, embed_vectors: bool = True) -> dict[str, int | str | list[str]]:
        with self._connect() as db:
            existing = {
                row["path"]: dict(row)
                for row in db.execute(
                    "SELECT id,path,content_hash,mtime_ns,file_size,ctime_ns,inode FROM nodes"
                )
            }
            index_version_row = db.execute(
                "SELECT value FROM metadata WHERE key='index_version'"
            ).fetchone()
            validate_existing_files = (
                index_version_row is None or index_version_row["value"] != INDEX_VERSION
            )
        notes, present_paths = self._scan_notes(existing, force_read=validate_existing_files)
        embedded_chunks = 0
        indexed_notes = len(present_paths)
        warnings: list[str] = []
        with self._connect() as db:
            embedding_model = getattr(self.embedder, "model", "disabled")
            stored_model_row = db.execute(
                "SELECT value FROM metadata WHERE key='embedding_model'"
            ).fetchone()
            stored_model = stored_model_row["value"] if stored_model_row else ""
            refresh_vectors = (
                embed_vectors and self.embedder is not None and stored_model != embedding_model
            )
            current_paths = set(present_paths)
            deleted_paths = set(existing) - current_paths
            for path in deleted_paths:
                self._delete_node(db, existing[path]["id"])

            rebuild_paths = set(notes)
            if refresh_vectors:
                rebuild_paths.update(current_paths - set(notes))
            embedding_failed = False
            for path in sorted(rebuild_paths):
                note = notes.get(path)
                old = existing.get(path)
                if note is None and old:
                    row = db.execute("SELECT * FROM nodes WHERE path=?", (path,)).fetchone()
                    if row is None:
                        continue
                    note = {
                        "id": row["id"], "title": row["title"], "note_type": row["note_type"],
                        "content": row["content"], "summary": row["summary"],
                        "source_url": row["source_url"], "content_hash": row["content_hash"],
                        "mtime_ns": row["mtime_ns"], "file_size": row["file_size"],
                        "ctime_ns": row["ctime_ns"], "inode": row["inode"],
                    }
                if note is None:
                    continue
                if (
                    old
                    and old["content_hash"] == note["content_hash"]
                    and not refresh_vectors
                ):
                    db.execute(
                        "UPDATE nodes SET mtime_ns=?,file_size=?,ctime_ns=?,inode=? WHERE path=?",
                        (note["mtime_ns"], note["file_size"], note["ctime_ns"], note["inode"], path),
                    )
                    continue
                if old:
                    self._delete_node(db, old["id"])
                db.execute(
                    "INSERT INTO nodes"
                    "(id,path,title,note_type,content,summary,source_url,content_hash,"
                    "mtime_ns,file_size,ctime_ns,inode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        note["id"],
                        path,
                        note["title"],
                        note["note_type"],
                        note["content"],
                        note["summary"],
                        note["source_url"],
                        note["content_hash"],
                        note["mtime_ns"],
                        note["file_size"],
                        note["ctime_ns"],
                        note["inode"],
                    ),
                )
                for chunk_index, chunk in enumerate(_chunks(note["content"])):
                    vector = None
                    if self.embedder is not None and embed_vectors and not embedding_failed:
                        try:
                            vector = json.dumps(self.embedder.embed(f"{note['title']}\n{chunk}"))
                            embedded_chunks += 1
                        except Exception as error:  # lexical indexing must survive semantic outages
                            warnings.append(f"Embedding failed for {path}: {error}")
                            embedding_failed = True
                    cursor = db.execute(
                        "INSERT INTO chunks(node_id,chunk_index,text,vector) VALUES(?,?,?,?)",
                        (note["id"], chunk_index, chunk, vector),
                    )
                    db.execute(
                        "INSERT INTO chunks_fts(chunk_id,node_id,text) VALUES(?,?,?)",
                        (cursor.lastrowid, note["id"], chunk),
                    )

            if self.embedder is not None and embed_vectors and not embedding_failed:
                missing_vectors = db.execute(
                    "SELECT chunks.id,nodes.path,nodes.title,chunks.text FROM chunks "
                    "JOIN nodes ON nodes.id=chunks.node_id WHERE chunks.vector IS NULL"
                ).fetchall()
                for row in missing_vectors:
                    if row["path"] in rebuild_paths:
                        continue
                    try:
                        vector = json.dumps(self.embedder.embed(f"{row['title']}\n{row['text']}"))
                    except Exception as error:  # lexical indexing must survive semantic outages
                        warnings.append(f"Embedding retry failed for {row['path']}: {error}")
                        break
                    db.execute("UPDATE chunks SET vector=? WHERE id=?", (vector, row["id"]))
                    embedded_chunks += 1

            title_map: dict[str, str] = {}
            for row in db.execute("SELECT id,title,path FROM nodes"):
                title_map[_normalize_target(row["title"])] = row["id"]
                title_map[_normalize_target(Path(row["path"]).stem)] = row["id"]
            for path, note in notes.items():
                old = existing.get(path)
                if old and old["content_hash"] == note["content_hash"] and not refresh_vectors:
                    continue
                if db.execute("SELECT 1 FROM nodes WHERE id=?", (note["id"],)).fetchone() is None:
                    continue
                db.execute("DELETE FROM edges WHERE source_id=?", (note["id"],))
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
            # Re-resolve links when a note is renamed or added, while retaining their
            # original target title when the destination is not yet in the vault.
            for row in db.execute("SELECT rowid,target_title FROM edges").fetchall():
                target_id = title_map.get(_normalize_target(row["target_title"]))
                db.execute("UPDATE edges SET target_id=? WHERE rowid=?", (target_id, row["rowid"]))
            if embed_vectors:
                db.execute(
                    "INSERT INTO metadata(key,value) VALUES('embedding_model',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (embedding_model,),
                )
            db.execute(
                "INSERT INTO metadata(key,value) VALUES('index_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (INDEX_VERSION,),
            )
            db.commit()
        return {
            "indexed_notes": indexed_notes,
            "embedded_chunks": embedded_chunks,
            "deleted_notes": len(deleted_paths),
            "embedding_model": embedding_model,
            "warnings": warnings,
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        hops: int = 0,
        semantic_fallback: bool = True,
    ) -> list[SearchHit]:
        return self._search(
            query, limit=limit, hops=hops, semantic_fallback=semantic_fallback
        )[0]

    def _search(
        self, query: str, *, limit: int, hops: int, semantic_fallback: bool
    ) -> tuple[list[SearchHit], list[str]]:
        query = query.strip()
        if not query:
            return [], []
        limit = min(max(int(limit), 1), 20)
        with self._connect() as db:
            scores: dict[str, float] = {}
            evidence: dict[str, str] = {}
            warnings: list[str] = []

            # Lexical search is the normal path. Semantic embeddings are only used when
            # lexical search returns no results.
            lexical = self._lexical(db, query, limit=max(20, limit * 4))
            seen_lexical: set[str] = set()
            query_terms = _query_terms(query)
            for rank, row in enumerate(lexical, 1):
                node_id = row["node_id"]
                if node_id in seen_lexical:
                    continue
                matching_terms = sum(term in row["text"].casefold() for term in query_terms)
                if query_terms and matching_terms < min(2, len(query_terms)):
                    continue
                seen_lexical.add(node_id)
                scores[node_id] = 1.0 / (60 + rank)
                evidence[node_id] = row["text"]

            if semantic_fallback and self.embedder is not None and not seen_lexical:
                configured_model = str(getattr(self.embedder, "model", ""))
                stored_model_row = db.execute(
                    "SELECT value FROM metadata WHERE key='embedding_model'"
                ).fetchone()
                stored_model = stored_model_row["value"] if stored_model_row else ""
                vector_count = db.execute(
                    "SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL"
                ).fetchone()[0]
                missing_vector_count = db.execute(
                    "SELECT COUNT(*) FROM chunks WHERE vector IS NULL"
                ).fetchone()[0]
                if not vector_count:
                    warnings.append(
                        "Semantic fallback skipped: no embedded note vectors are available; "
                        "run an embedding-enabled reindex."
                    )
                elif stored_model != configured_model:
                    indexed_model = stored_model or "unknown"
                    warnings.append(
                        "Semantic fallback skipped because indexed vectors use embedding "
                        f"model {indexed_model!r}, while configured model is "
                        f"{configured_model or 'unknown'!r}; run an embedding-enabled reindex."
                    )
                else:
                    if missing_vector_count:
                        warnings.append(
                            "Semantic fallback has incomplete coverage: "
                            f"{missing_vector_count} note chunks have no embeddings; "
                            "run an embedding-enabled reindex."
                        )
                    try:
                        query_vector = self.embedder.embed(query)
                        semantic = sorted(
                            (
                                (_cosine(query_vector, json.loads(row["vector"])), row)
                                for row in db.execute(
                                    "SELECT node_id,text,vector FROM chunks WHERE vector IS NOT NULL"
                                )
                            ),
                            key=lambda item: item[0],
                            reverse=True,
                        )[: max(20, limit * 4)]
                        seen_semantic: set[str] = set()
                        for rank, (similarity, row) in enumerate(semantic, 1):
                            if similarity < 0.42:
                                continue
                            node_id = row["node_id"]
                            if node_id in seen_semantic:
                                continue
                            seen_semantic.add(node_id)
                            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (60 + rank)
                            evidence.setdefault(node_id, row["text"])
                    except Exception as error:  # lexical results remain usable during model outages
                        warnings.append(
                            f"Semantic fallback failed; lexical results were used: {error}"
                        )

            if not scores:
                return [], warnings

            nodes: dict[str, sqlite3.Row] = {}
            for node_id in scores:
                row = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
                if row is not None:
                    nodes[node_id] = row

            for node_id in list(scores):
                # Distilled notes are the answer surface; sources and maps are not
                # indexed into normal retrieval, but preserve this guard for old DBs.
                if nodes[node_id]["note_type"] in {"source", "map"}:
                    scores.pop(node_id, None)

            graph_meta: dict[str, tuple[str, str, str]] = {}
            if hops > 0 and scores:
                ranked_seeds = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
                direct_scores = dict(scores)
                neighbor_rows: list[tuple[str, sqlite3.Row]] = []
                for seed_id, _ in ranked_seeds:
                    seed = nodes[seed_id]
                    for edge in db.execute(
                        "SELECT source_id,target_id,relation,reason FROM edges "
                        "WHERE source_id=? OR target_id=?",
                        (seed_id, seed_id),
                    ):
                        neighbor = edge["target_id"] if edge["source_id"] == seed_id else edge["source_id"]
                        if not neighbor or neighbor == seed_id:
                            continue
                        node = db.execute("SELECT * FROM nodes WHERE id=?", (neighbor,)).fetchone()
                        if node is None or node["note_type"] in {"source", "map"}:
                            continue
                        neighbor_rows.append((neighbor, node))
                        graph_meta.setdefault(neighbor, (edge["relation"], seed["title"], edge["reason"]))
                        graph_score = direct_scores[seed_id] * 0.55
                        if graph_score > scores.get(neighbor, 0.0):
                            scores[neighbor] = graph_score
                            evidence.setdefault(neighbor, node["summary"] or node["content"])
                nodes.update({node_id: node for node_id, node in neighbor_rows})

            ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
            hits: list[SearchHit] = []
            for node_id, score in ranked:
                node = nodes[node_id]
                raw_evidence = evidence.get(node_id) or node["summary"] or node["content"]
                excerpt = _snippet(raw_evidence, query, limit=260)
                graph = graph_meta.get(node_id)
                locations = _match_locations(query, node["content"])
                if graph and not locations:
                    locations = (f"graph:{graph[0]}",)
                hits.append(
                    SearchHit(
                        id=node_id,
                        title=node["title"],
                        path=node["path"],
                        note_type=node["note_type"],
                        score=score,
                        excerpt=excerpt or _snippet(node["summary"] or node["content"], query, limit=260),
                        source_url=node["source_url"],
                        via_graph=graph is not None,
                        relation=graph[0] if graph else "",
                        related_from=graph[1] if graph else "",
                        match_location=locations,
                        evidence=_clean_excerpt(raw_evidence, limit=1200),
                    )
                )
            return hits, warnings

    def context(self, query: str, *, max_tokens: int = 4000, hops: int = 0, limit: int = 4) -> dict:
        if max_tokens < 32:
            raise ValueError("max_tokens must be at least 32")
        max_tokens = min(max_tokens, 4000)
        hits, warnings = self._search(
            query, limit=min(max(int(limit), 1), 4), hops=hops, semantic_fallback=True
        )
        char_budget = int(max_tokens * 3.5)
        blocks: list[str] = []
        included: list[SearchHit] = []
        used = 0
        truncated = False
        for index, hit in enumerate(hits, 1):
            relation = f"\nRelated through: {hit.relation} from {hit.related_from}" if hit.via_graph else ""
            source = f"\nOriginal URL: {hit.source_url}" if hit.source_url else ""
            block = (
                f"[{index}] {hit.title} ({hit.note_type})\n"
                f"Obsidian note: {hit.path}{source}{relation}\n"
                f"Match location: {', '.join(hit.match_location) or 'body'}\n"
                f"Evidence:\n{hit.evidence or hit.excerpt}\n"
            )
            remaining = char_budget - used
            if remaining <= 0:
                truncated = True
                break
            if len(block) > remaining:
                if not blocks:
                    marker = "\n[Evidence truncated to fit the context budget.]"
                    block = block[: max(0, remaining - len(marker))] + marker
                    included.append(hit)
                    blocks.append(block)
                    used += len(block)
                truncated = True
                break
            blocks.append(block)
            included.append(hit)
            used += len(block)
        if truncated:
            warnings.append("Evidence was truncated to fit the context budget.")
        if not hits:
            warnings.append("No matching approved note evidence found.")
        context = "\n".join(blocks)
        return {
            "query": query,
            "context": context,
            "estimated_tokens": math.ceil(len(context) / 3.5),
            "sources": [hit.as_dict() for hit in included],
            "truncated": truncated,
            "omitted_notes": max(0, len(hits) - len(included)),
            "warnings": warnings,
        }

    def read_note(self, note_id: str, *, section: str = "", max_chars: int = 12000) -> dict:
        """Read one exact approved note from a canonical content folder.

        The identifier is matched against frontmatter; it is never interpreted as a
        filesystem path. Maps, sources, reviews, and arbitrary vault paths are excluded.
        """
        note_id = str(note_id).strip()
        if not note_id:
            raise ValueError("note_id cannot be empty")
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        max_chars = min(int(max_chars), 30000)
        roots = [self.vault / f"{domain}/{folder}"
                 for domain in DOMAIN_FOLDER.values()
                 for folder in dict.fromkeys(FOLDER_FOR_TYPE.values())]
        matches: list[tuple[Path, dict, str]] = []
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            root_resolved = root.resolve()
            for path in root.rglob("*.md"):
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(self.vault)
                    resolved.relative_to(root_resolved)
                    metadata = _read_frontmatter(path)
                except (OSError, ValueError):
                    continue
                if metadata.get("status") != "approved" or not _canonical_metadata_matches(
                    metadata, path, self.vault
                ):
                    continue
                relative = path.relative_to(self.vault).as_posix()
                canonical_id = str(metadata.get("id") or _stable_id(relative))
                if canonical_id != note_id:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    full_metadata, body = parse_note(text)
                except (OSError, ValueError):
                    continue
                full_id = str(full_metadata.get("id") or _stable_id(relative))
                if (
                    full_id != note_id
                    or full_metadata.get("status") != "approved"
                    or not _canonical_metadata_matches(full_metadata, path, self.vault)
                ):
                    continue
                matches.append((path, full_metadata, body))
        if not matches:
            raise KeyError(f"Unknown approved canonical note ID: {note_id}")
        if len(matches) != 1:
            raise ValueError(f"Ambiguous approved canonical note ID {note_id!r}: found {len(matches)} notes")

        path, metadata, body = matches[0]
        title_match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
        title = str(metadata.get("title") or (title_match.group(1) if title_match else path.stem))
        selected = body
        selected_section = ""
        if section:
            selected, selected_section = _extract_section(body, section)
            if selected is None:
                raise KeyError(f"Section {section!r} was not found in note {note_id!r}")
        omitted_characters = max(0, len(selected) - max_chars)
        truncated = omitted_characters > 0
        headings = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", body))
        omitted_sections: list[str] = []
        if section:
            wanted = selected_section.casefold()
            selected_heading = next(
                (item for item in headings if item.group(2).strip().casefold() == wanted), None
            )
            if selected_heading:
                level = len(selected_heading.group(1))
                selected_end = next(
                    (item.start() for item in headings
                     if item.start() > selected_heading.start() and len(item.group(1)) <= level),
                    len(body),
                )
                omitted_sections = [
                    item.group(2).strip() for item in headings
                    if item.start() < selected_heading.start() or item.start() >= selected_end
                ]
        elif truncated:
            omitted_sections = [item.group(2).strip() for item in headings if item.start() >= max_chars]
        if truncated:
            marker = f"\n\n[Truncated after {max_chars} characters; {omitted_characters} characters omitted.]"
            selected = selected[:max_chars].rstrip() + marker
        return {
            "note_id": note_id,
            "title": title,
            "path": path.relative_to(self.vault).as_posix(),
            "note_type": str(metadata.get("type") or "note"),
            "section": selected_section or None,
            "body": selected,
            "truncated": truncated,
            "omitted_characters": omitted_characters,
            "omitted_sections": omitted_sections,
            "provenance": {
                key: metadata[key]
                for key in ("source_url", "source_note", "source_id", "source_creator", "source_kind")
                if metadata.get(key)
            },
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

    def _scan_notes(
        self, existing: dict[str, dict], *, force_read: bool = False
    ) -> tuple[dict[str, dict], set[str]]:
        """Read changed approved notes from canonical content folders only.

        Directory traversal still detects additions, moves, and deletions, but unchanged
        Markdown is not opened on every MCP request. File stat metadata is only a fast
        freshness hint; changed files are parsed and hashed before the index is updated.
        """
        notes: dict[str, dict] = {}
        present_paths: set[str] = set()
        roots = [self.vault / f"{domain}/{folder}"
                 for domain in DOMAIN_FOLDER.values()
                 for folder in dict.fromkeys(FOLDER_FOR_TYPE.values())]
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            root_resolved = root.resolve()
            for path in sorted(root.rglob("*.md")):
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(self.vault)
                    resolved.relative_to(root_resolved)
                    stat = path.stat()
                except (OSError, ValueError):
                    continue
                relative = path.relative_to(self.vault).as_posix()
                old = existing.get(relative)
                mtime_ns = stat.st_mtime_ns
                file_size = stat.st_size
                ctime_ns = stat.st_ctime_ns
                inode = stat.st_ino
                if (
                    old
                    and old["mtime_ns"] == mtime_ns
                    and old["file_size"] == file_size
                    and old["ctime_ns"] == ctime_ns
                    and old["inode"] == inode
                    and not force_read
                ):
                    present_paths.add(relative)
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                metadata, body = parse_note(text)
                if metadata.get("status") != "approved" or not _canonical_metadata_matches(
                    metadata, path, self.vault
                ):
                    continue
                title_match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
                title = str(metadata.get("title") or (title_match.group(1) if title_match else path.stem))
                note_id = str(metadata.get("id") or _stable_id(relative))
                summary = _section(body, "Summary")
                present_paths.add(relative)
                notes[relative] = {
                    "id": note_id,
                    "title": title,
                    "note_type": str(metadata.get("type") or "note"),
                    "content": text,
                    "summary": summary,
                    "source_url": str(metadata.get("source_url") or ""),
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "mtime_ns": mtime_ns,
                    "file_size": file_size,
                    "ctime_ns": ctime_ns,
                    "inode": inode,
                }
        return notes, present_paths

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


def _query_terms(query: str) -> list[str]:
    return [term.casefold() for term in TOKEN_RE.findall(query) if len(term) > 1]


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


def _extract_section(body: str, requested: str) -> tuple[str | None, str]:
    wanted = requested.strip().lstrip("#").strip().casefold()
    headings = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", body))
    for index, match in enumerate(headings):
        heading = match.group(2).strip()
        if heading.casefold() != wanted:
            continue
        level = len(match.group(1))
        end = len(body)
        for following in headings[index + 1 :]:
            if len(following.group(1)) <= level:
                end = following.start()
                break
        return body[match.start() : end].strip(), heading
    return None, ""


def _match_locations(query: str, content: str) -> tuple[str, ...]:
    metadata, body = parse_note(content)
    terms = [term.casefold() for term in TOKEN_RE.findall(query) if len(term) > 1]
    if not terms:
        return ()

    def contains(value: object) -> bool:
        values = value if isinstance(value, list) else [value]
        return any(any(term in str(item).casefold() for term in terms) for item in values if item)

    locations: list[str] = []
    if contains(metadata.get("title")):
        locations.append("title")
    if contains(metadata.get("aliases", [])):
        locations.append("alias")
    if contains(metadata.get("tags", [])):
        locations.append("tag")
    summary = _raw_section(body, "Summary")
    if contains(summary):
        locations.append("summary")
    for match in HEADING_RE.finditer(body):
        if contains(match.group(1)):
            locations.append(f"heading:{match.group(1).strip()}")
    if contains(body):
        locations.append("body")
    return tuple(dict.fromkeys(locations))


def _raw_section(body: str, heading: str) -> str:
    match = re.search(rf"(?ms)^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)", body)
    return match.group(1) if match else ""


def _snippet(text: str, query: str, *, limit: int) -> str:
    cleaned = re.sub(r"---.*?---", "", text, count=1, flags=re.DOTALL)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    terms = [term.casefold() for term in TOKEN_RE.findall(query) if len(term) > 1]
    lowered = cleaned.casefold()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, (min(positions) if positions else 0) - limit // 3)
    end = min(len(cleaned), start + limit)
    if end == len(cleaned):
        start = max(0, end - limit)
    snippet = cleaned[start:end]
    if start:
        snippet = "…" + snippet
    if end < len(cleaned):
        snippet += "…"
    return snippet[:limit]


def _clean_excerpt(text: str, limit: int = 700) -> str:
    cleaned = re.sub(r"---.*?---", "", text, count=1, flags=re.DOTALL)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _normalize_target(value: str) -> str:
    return Path(value).stem.casefold().strip()


def _stable_id(value: str) -> str:
    return "note-" + hashlib.sha256(value.encode()).hexdigest()[:16]


def _read_frontmatter(path: Path) -> dict:
    """Parse YAML metadata without loading the potentially large Markdown body."""
    with path.open(encoding="utf-8", errors="replace") as note_file:
        opening = note_file.readline()
        if opening.strip() != "---":
            return {}
        header = [opening]
        header_size = len(opening)
        for line in note_file:
            header.append(line)
            header_size += len(line)
            if line.strip() == "---":
                metadata, _ = parse_note("".join(header))
                return metadata
            if header_size > 256_000:
                return {}
    return {}


def _canonical_metadata_matches(metadata: dict, path: Path, vault: Path) -> bool:
    try:
        parts = path.relative_to(vault).parts
    except ValueError:
        return False
    if len(parts) < 3:
        return False
    expected_domain = next(
        (domain for domain, folder in DOMAIN_FOLDER.items() if folder == parts[0]), None
    )
    expected_type = next(
        (note_type for note_type, folder in FOLDER_FOR_TYPE.items() if folder == parts[1]), None
    )
    return (
        expected_domain is not None
        and expected_type is not None
        and str(metadata.get("domain") or "").strip().casefold() == expected_domain
        and str(metadata.get("type") or "").strip().casefold() == expected_type
    )


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0
