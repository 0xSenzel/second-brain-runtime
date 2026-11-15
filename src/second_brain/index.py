from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import sqlite3
import threading
import urllib.request  # noqa: F401  (tests patch index.urllib.request)
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .embedding import EMBEDDING_TIMEOUT_SECONDS, Embedder, OllamaEmbedder
from .models import INBOX_FOLDER, INDEXED_FOLDERS, STATUS_APPROVED, STATUS_INBOX
from .notes import parse_note

__all__ = [
    "EMBEDDING_TIMEOUT_SECONDS",
    "BrainIndex",
    "Embedder",
    "OllamaEmbedder",
    "SearchHit",
]

CONNECTION_RE = re.compile(
    r"(?m)^-\s+(about|created-by|contradicts|derived-from|requires|similar-to|supports|uses):\s+\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\](?:\s+—\s+(.+))?$",
    re.IGNORECASE,
)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
HEADING_RE = re.compile(r"(?m)^#{1,3}\s+(.+?)\s*$")
TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)
INDEX_VERSION = "4"
INDEX_PATH_ENV = "SECOND_BRAIN_INDEX_PATH"
DISABLE_VEC_ENV = "SECOND_BRAIN_DISABLE_SQLITE_VEC"

INBOX_SCORE_WEIGHT = 0.5
QUERY_EMBED_TIMEOUT_SECONDS = 3.0
RRF_K = 60
# A vector hit below this cosine similarity is noise, not a paraphrase.
VECTOR_MIN_SIMILARITY = 0.5

# Words that match nearly every chunk and so carry no ranking signal. Dropped from the
# lexical query only when something distinctive remains.
STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from how i if in is it its me my of on "
    "or so that the their there these this to was were what when where which who why will "
    "with you your about into than then them they not no".split()
)


class _Timeout(Exception):
    pass


def _call_with_timeout(fn, timeout: float):
    """Run fn on a daemon thread; give up after `timeout` seconds (the thread is left to finish)."""
    results: queue.Queue = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            results.put((True, fn()))
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread
            results.put((False, error))

    threading.Thread(target=run, name="query-embed", daemon=True).start()
    try:
        ok, value = results.get(timeout=timeout)
    except queue.Empty:
        raise _Timeout(f"embedding did not return within {timeout:g}s") from None
    if not ok:
        raise value
    return value


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
    status: str = STATUS_APPROVED

    @property
    def unreviewed(self) -> bool:
        return self.status == STATUS_INBOX

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "type": self.note_type,
            "status": self.status,
            "unreviewed": self.unreviewed,
            "score": round(self.score, 6),
            "excerpt": self.excerpt,
            "source_url": self.source_url,
            "via_graph": self.via_graph,
            "relation": self.relation,
            "related_from": self.related_from,
            "match_location": list(self.match_location),
        }


def resolve_index_path(vault: Path) -> Path:
    override = os.environ.get(INDEX_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return vault / ".second-brain" / "index.sqlite3"


class BrainIndex:
    def __init__(
        self,
        vault: str | Path,
        *,
        embedder: Embedder | None = None,
        index_path: str | Path | None = None,
        use_sqlite_vec: bool | None = None,
    ):
        self.vault = Path(vault).expanduser().resolve()
        self.db_path = Path(index_path).expanduser() if index_path else resolve_index_path(self.vault)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        if use_sqlite_vec is None:
            use_sqlite_vec = os.environ.get(DISABLE_VEC_ENV, "") not in {"1", "true", "yes"}
        self.vec_enabled = bool(use_sqlite_vec) and self._probe_vec()
        self._initialize()

    # -- connection and schema ------------------------------------------------------

    @staticmethod
    def _load_vec(connection: sqlite3.Connection) -> bool:
        try:
            import sqlite_vec

            connection.enable_load_extension(True)
            try:
                sqlite_vec.load(connection)
            finally:
                connection.enable_load_extension(False)
            return True
        except (ImportError, AttributeError, sqlite3.Error, OSError):
            return False

    def _probe_vec(self) -> bool:
        connection = sqlite3.connect(":memory:")
        try:
            return self._load_vec(connection)
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if self.vec_enabled and not self._load_vec(connection):
            self.vec_enabled = False
        return connection

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, roll back on error, always close."""
        connection = self._open()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        # The index is derived data: when its layout version changes, rebuild it.
        if self.db_path.exists():
            try:
                probe = sqlite3.connect(self.db_path, timeout=30)
                try:
                    row = probe.execute(
                        "SELECT value FROM metadata WHERE key='index_version'"
                    ).fetchone()
                finally:
                    probe.close()
                stale = row is None or row[0] != INDEX_VERSION
            except sqlite3.Error:
                stale = True
            if stale:
                for suffix in ("", "-wal", "-shm"):
                    Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    note_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'approved',
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
                    title,
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

    @staticmethod
    def _meta(db: sqlite3.Connection, key: str) -> str:
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

    @staticmethod
    def _set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- vectors ---------------------------------------------------------------------
    # chunks.vector (JSON text) is the source of truth and what "missing vector" means.
    # When sqlite-vec loads, chunk_vec mirrors it for fast KNN; search re-syncs the mirror
    # if another process wrote vectors without the extension.

    def _vec_table_exists(self, db: sqlite3.Connection) -> bool:
        return (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='chunk_vec'").fetchone() is not None
        )

    def _vec_insert(self, db: sqlite3.Connection, chunk_id: int, vector: list[float]) -> None:
        if not self.vec_enabled:
            return
        dim = int(self._meta(db, "vec_dim") or 0)
        if not dim or not self._vec_table_exists(db):
            if self._vec_table_exists(db):
                db.execute("DROP TABLE chunk_vec")
            db.execute(
                f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{len(vector)}] "
                "distance_metric=cosine)"
            )
            self._set_meta(db, "vec_dim", str(len(vector)))
            dim = len(vector)
        if len(vector) != dim:
            return  # mismatched dimension: Python path ignores it too (cosine is 0)
        db.execute("DELETE FROM chunk_vec WHERE rowid=?", (chunk_id,))
        db.execute(
            "INSERT INTO chunk_vec(rowid,embedding) VALUES(?,?)", (chunk_id, json.dumps(vector))
        )

    def _vec_sync(self, db: sqlite3.Connection) -> None:
        """Make chunk_vec hold exactly the chunks that have a vector of the right size."""
        if not self.vec_enabled:
            return
        have = (
            db.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]
            if self._vec_table_exists(db)
            else -1
        )
        want_rows = db.execute("SELECT id,vector FROM chunks WHERE vector IS NOT NULL").fetchall()
        if have == len(want_rows) and have >= 0:
            return
        if self._vec_table_exists(db):
            db.execute("DROP TABLE chunk_vec")
        db.execute("DELETE FROM metadata WHERE key='vec_dim'")
        for row in want_rows:
            self._vec_insert(db, row["id"], json.loads(row["vector"]))

    def _clear_vectors(self, db: sqlite3.Connection) -> None:
        db.execute("UPDATE chunks SET vector=NULL")
        if self.vec_enabled and self._vec_table_exists(db):
            db.execute("DROP TABLE chunk_vec")
        db.execute("DELETE FROM metadata WHERE key='vec_dim'")
        db.execute("DELETE FROM metadata WHERE key='embedding_model'")

    def prepare_embedding(self, model: str) -> bool:
        """Drop vectors made by a different model. Returns True when they were dropped."""
        with self._connect() as db:
            stored = self._meta(db, "embedding_model")
            if stored and stored != model:
                self._clear_vectors(db)
                return True
        return False

    def pending_chunks(self, limit: int | None = None) -> list[tuple[int, str]]:
        """Chunks that still need a vector, as (chunk id, text to embed)."""
        sql = (
            "SELECT chunks.id,nodes.title,chunks.text FROM chunks "
            "JOIN nodes ON nodes.id=chunks.node_id WHERE chunks.vector IS NULL ORDER BY chunks.id"
        )
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self._connect() as db:
            return [(row["id"], f"{row['title']}\n{row['text']}") for row in db.execute(sql, params)]

    def store_vector(self, chunk_id: int, vector: list[float], *, model: str) -> bool:
        """Save one vector. Returns False when the chunk vanished or already has one."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE chunks SET vector=? WHERE id=? AND vector IS NULL",
                (json.dumps(vector), chunk_id),
            )
            if cursor.rowcount == 0:
                return False
            self._vec_insert(db, chunk_id, vector)
            self._set_meta(db, "embedding_model", model)
            return True

    def embed_pending(self, limit: int | None = None) -> tuple[int, list[str]]:
        """Embed pending chunks synchronously with self.embedder. Returns (count, warnings)."""
        if self.embedder is None:
            return 0, []
        model = str(getattr(self.embedder, "model", ""))
        self.prepare_embedding(model)
        stored = 0
        for chunk_id, text in self.pending_chunks(limit):
            try:
                vector = self.embedder.embed(text)
            except Exception as error:  # lexical indexing must survive semantic outages
                return stored, [f"Embedding failed after {stored} chunks: {error}"]
            if self.store_vector(chunk_id, vector, model=model):
                stored += 1
        return stored, []

    def vector_stats(self) -> dict[str, int | str]:
        with self._connect() as db:
            return {
                "chunks": db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                "embedded": db.execute(
                    "SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL"
                ).fetchone()[0],
                "model": self._meta(db, "embedding_model"),
                "backend": "sqlite-vec" if self.vec_enabled else "python",
            }

    def similar_pairs(self, *, threshold: float = 0.92) -> list[tuple[str, str, float]]:
        """Approved or inbox note pairs whose first chunks are near-identical in meaning.

        The weekly report uses this to flag likely duplicates. First chunks carry the title
        and summary, which is where two captures of one idea look alike; comparing every
        chunk would flag notes that merely quote the same source.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT node_id, vector FROM chunks WHERE chunk_index=0 AND vector IS NOT NULL"
            ).fetchall()
        vectors = [(row["node_id"], json.loads(row["vector"])) for row in rows]
        pairs: list[tuple[str, str, float]] = []
        for position, (left_id, left) in enumerate(vectors):
            for right_id, right in vectors[position + 1 :]:
                similarity = _cosine(left, right)
                if similarity >= threshold:
                    pairs.append((left_id, right_id, round(similarity, 4)))
        return sorted(pairs, key=lambda pair: pair[2], reverse=True)

    # -- indexing --------------------------------------------------------------------

    def reindex(self, *, embed_vectors: bool = False) -> dict[str, int | str | list[str]]:
        """Bring the index in line with Notes/ and Inbox/.

        Only touches the embedder when `embed_vectors` is true; otherwise new chunks are
        stored without vectors and EmbeddingWorker (or `embed_pending`) fills them in.
        """
        with self._connect() as db:
            existing = {
                row["path"]: dict(row)
                for row in db.execute(
                    "SELECT id,path,status,content_hash,mtime_ns,file_size,ctime_ns,inode FROM nodes"
                )
            }
        notes, present_paths = self._scan_notes(existing)
        warnings: list[str] = []
        embedding_model = getattr(self.embedder, "model", "disabled")
        with self._connect() as db:
            for path in set(existing) - present_paths:
                self._delete_node(db, existing[path]["id"])
            deleted = len(set(existing) - present_paths)
            for path in sorted(notes):
                note = notes[path]
                old = existing.get(path)
                if old and old["content_hash"] == note["content_hash"]:
                    db.execute(
                        "UPDATE nodes SET mtime_ns=?,file_size=?,ctime_ns=?,inode=? WHERE path=?",
                        (note["mtime_ns"], note["file_size"], note["ctime_ns"], note["inode"], path),
                    )
                    continue
                if old:
                    self._delete_node(db, old["id"])
                # An id can move between paths (approval moves Inbox/ -> Notes/).
                clash = db.execute("SELECT id FROM nodes WHERE id=?", (note["id"],)).fetchone()
                if clash:
                    self._delete_node(db, note["id"])
                db.execute(
                    "INSERT INTO nodes"
                    "(id,path,title,note_type,status,content,summary,source_url,content_hash,"
                    "mtime_ns,file_size,ctime_ns,inode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        note["id"], path, note["title"], note["note_type"], note["status"],
                        note["content"], note["summary"], note["source_url"],
                        note["content_hash"], note["mtime_ns"], note["file_size"],
                        note["ctime_ns"], note["inode"],
                    ),
                )
                for chunk_index, chunk in enumerate(_chunks(note["content"])):
                    cursor = db.execute(
                        "INSERT INTO chunks(node_id,chunk_index,text) VALUES(?,?,?)",
                        (note["id"], chunk_index, chunk),
                    )
                    db.execute(
                        "INSERT INTO chunks_fts(chunk_id,node_id,text,title) VALUES(?,?,?,?)",
                        (cursor.lastrowid, note["id"], chunk, note["title"]),
                    )

            title_map: dict[str, str] = {}
            for row in db.execute("SELECT id,title,path FROM nodes"):
                title_map[_normalize_target(row["title"])] = row["id"]
                title_map[_normalize_target(Path(row["path"]).stem)] = row["id"]
            for path, note in notes.items():
                old = existing.get(path)
                if old and old["content_hash"] == note["content_hash"]:
                    continue
                db.execute("DELETE FROM edges WHERE source_id=?", (note["id"],))
                seen: set[tuple[str, str]] = set()
                for relation, target, reason in _connections(note["content"]):
                    key = (relation, _normalize_target(target))
                    if key in seen:
                        continue
                    seen.add(key)
                    db.execute(
                        "INSERT INTO edges(source_id,target_title,target_id,relation,reason) "
                        "VALUES(?,?,?,?,?)",
                        (note["id"], target, title_map.get(key[1]), relation, reason),
                    )
            # Re-resolve links when a note is renamed or added, while retaining their
            # original target title when the destination is not yet in the vault.
            for row in db.execute("SELECT rowid,target_title FROM edges").fetchall():
                target_id = title_map.get(_normalize_target(row["target_title"]))
                db.execute("UPDATE edges SET target_id=? WHERE rowid=?", (target_id, row["rowid"]))
            self._set_meta(db, "index_version", INDEX_VERSION)
            counts = {
                status: db.execute("SELECT COUNT(*) FROM nodes WHERE status=?", (status,)).fetchone()[0]
                for status in (STATUS_APPROVED, STATUS_INBOX)
            }
        embedded = 0
        if embed_vectors and self.embedder is not None:
            embedded, embed_warnings = self.embed_pending()
            warnings.extend(embed_warnings)
        return {
            "indexed_notes": len(present_paths),
            "approved_notes": counts[STATUS_APPROVED],
            "inbox_notes": counts[STATUS_INBOX],
            "embedded_chunks": embedded,
            "deleted_notes": deleted,
            "embedding_model": embedding_model,
            "warnings": warnings,
        }

    # -- search ----------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        hops: int = 0,
        include_inbox: bool = True,
    ) -> list[SearchHit]:
        return self._search(query, limit=limit, hops=hops, include_inbox=include_inbox)[0]

    def _vector_ranking(
        self, db: sqlite3.Connection, query: str, candidates: int, warnings: list[str]
    ) -> list[tuple[str, str]] | None:
        """Node ids ranked by vector similarity with the best chunk text; None when unavailable."""
        if self.embedder is None:
            return None
        configured = str(getattr(self.embedder, "model", ""))
        stats = db.execute(
            "SELECT COUNT(*) AS total, COUNT(vector) AS embedded FROM chunks"
        ).fetchone()
        if not stats["embedded"]:
            warnings.append(
                "Vector search skipped: no note vectors yet; the embedding worker fills them in."
            )
            return None
        stored = self._meta(db, "embedding_model")
        if stored != configured:
            warnings.append(
                f"Vector search skipped: indexed vectors use model {stored or 'unknown'!r}, "
                f"configured model is {configured or 'unknown'!r}."
            )
            return None
        if stats["embedded"] < stats["total"]:
            warnings.append(
                f"Vector search covers {stats['embedded']} of {stats['total']} chunks; "
                "the rest are still being embedded."
            )
        try:
            query_vector = _call_with_timeout(
                lambda: self.embedder.embed(query), QUERY_EMBED_TIMEOUT_SECONDS
            )
        except Exception as error:  # lexical results remain usable during model outages
            warnings.append(f"Vector search failed; lexical results were used: {error}")
            return None
        scored: list[tuple[float, int]]
        if self.vec_enabled:
            self._vec_sync(db)
            dim = int(self._meta(db, "vec_dim") or 0)
            if dim != len(query_vector):
                return None
            rows = db.execute(
                "SELECT rowid,distance FROM chunk_vec WHERE embedding MATCH ? AND k=? "
                "ORDER BY distance",
                (json.dumps(query_vector), candidates),
            ).fetchall()
            scored = [(1.0 - row["distance"], row["rowid"]) for row in rows]
        else:
            scored = sorted(
                (
                    (_cosine(query_vector, json.loads(row["vector"])), row["id"])
                    for row in db.execute("SELECT id,vector FROM chunks WHERE vector IS NOT NULL")
                ),
                reverse=True,
            )[:candidates]
        ranking: list[tuple[str, str]] = []
        seen: set[str] = set()
        for similarity, chunk_id in scored:
            if similarity < VECTOR_MIN_SIMILARITY:
                break
            row = db.execute("SELECT node_id,text FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            if row is None or row["node_id"] in seen:
                continue
            seen.add(row["node_id"])
            ranking.append((row["node_id"], row["text"]))
        return ranking

    def _search(
        self, query: str, *, limit: int, hops: int, include_inbox: bool = True
    ) -> tuple[list[SearchHit], list[str]]:
        query = query.strip()
        if not query:
            return [], []
        limit = min(max(int(limit), 1), 20)
        candidates = max(40, limit * 8)
        with self._connect() as db:
            scores: dict[str, float] = {}
            evidence: dict[str, str] = {}
            warnings: list[str] = []

            vector_ranking = self._vector_ranking(db, query, candidates, warnings)
            query_tokens = _query_tokens(query)
            # With a working vector side, a lexical hit must contain a fair share of the
            # query terms: prefix matching otherwise lets one incidental word pull in
            # unrelated notes, and vectors already cover paraphrases. Lexical-only
            # search keeps every hit, because recall matters more than precision there.
            required = _required_terms(len(query_tokens)) if vector_ranking is not None else 1
            seen_lexical: set[str] = set()
            for row in self._lexical(db, query, limit=candidates):
                if row["node_id"] in seen_lexical:
                    continue
                if _matched_terms(query_tokens, row["text"]) < required:
                    continue
                seen_lexical.add(row["node_id"])
                scores[row["node_id"]] = 1.0 / (RRF_K + len(seen_lexical))
                evidence[row["node_id"]] = row["text"]

            for rank, (node_id, text) in enumerate(vector_ranking or [], 1):
                scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (RRF_K + rank)
                evidence.setdefault(node_id, text)

            if not scores:
                return [], warnings

            nodes: dict[str, sqlite3.Row] = {}
            for node_id in list(scores):
                row = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
                if row is None or (not include_inbox and row["status"] == STATUS_INBOX):
                    scores.pop(node_id)
                    continue
                nodes[node_id] = row
                if row["status"] == STATUS_INBOX:
                    scores[node_id] *= INBOX_SCORE_WEIGHT

            graph_meta: dict[str, tuple[str, str, str]] = {}
            if hops > 0 and scores:
                ranked_seeds = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
                direct_scores = dict(scores)
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
                        if node is None or (not include_inbox and node["status"] == STATUS_INBOX):
                            continue
                        weight = INBOX_SCORE_WEIGHT if node["status"] == STATUS_INBOX else 1.0
                        graph_score = direct_scores[seed_id] * 0.55 * weight
                        graph_meta.setdefault(neighbor, (edge["relation"], seed["title"], edge["reason"]))
                        if graph_score > scores.get(neighbor, 0.0):
                            scores[neighbor] = graph_score
                            evidence.setdefault(neighbor, node["summary"] or node["content"])
                            nodes[neighbor] = node

            ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
            hits: list[SearchHit] = []
            for node_id, score in ranked:
                node = nodes[node_id]
                raw_evidence = evidence.get(node_id) or node["summary"] or node["content"]
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
                        excerpt=_snippet(raw_evidence, query, limit=260)
                        or _snippet(node["summary"] or node["content"], query, limit=260),
                        source_url=node["source_url"],
                        via_graph=graph is not None,
                        relation=graph[0] if graph else "",
                        related_from=graph[1] if graph else "",
                        match_location=locations,
                        evidence=_clean_excerpt(raw_evidence, limit=1200),
                        status=node["status"],
                    )
                )
            return hits, warnings

    def context(
        self,
        query: str,
        *,
        max_tokens: int = 4000,
        hops: int = 0,
        limit: int = 4,
        include_inbox: bool = True,
    ) -> dict:
        if max_tokens < 32:
            raise ValueError("max_tokens must be at least 32")
        max_tokens = min(max_tokens, 4000)
        hits, warnings = self._search(
            query, limit=min(max(int(limit), 1), 4), hops=hops, include_inbox=include_inbox
        )
        char_budget = int(max_tokens * 3.5)
        blocks: list[str] = []
        included: list[SearchHit] = []
        used = 0
        truncated = False
        for index, hit in enumerate(hits, 1):
            relation = f"\nRelated through: {hit.relation} from {hit.related_from}" if hit.via_graph else ""
            source = f"\nOriginal URL: {hit.source_url}" if hit.source_url else ""
            label = " [unreviewed inbox note]" if hit.unreviewed else ""
            block = (
                f"[{index}] {hit.title} ({hit.note_type}){label}\n"
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
            warnings.append("No matching note evidence found.")
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

    def _note_files(self, include_inbox: bool = True) -> Iterator[tuple[Path, str]]:
        """Yield (path, status) for every Markdown file under the indexed folders."""
        for folder in INDEXED_FOLDERS:
            if folder == INBOX_FOLDER and not include_inbox:
                continue
            root = self.vault / folder
            if not root.is_dir() or root.is_symlink():
                continue
            root_resolved = root.resolve()
            status = STATUS_INBOX if folder == INBOX_FOLDER else STATUS_APPROVED
            for path in sorted(root.rglob("*.md")):
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(self.vault)
                    resolved.relative_to(root_resolved)
                except (OSError, ValueError):
                    continue
                yield path, status

    def read_note(
        self, note_id: str, *, section: str = "", max_chars: int = 12000, include_inbox: bool = True
    ) -> dict:
        """Read one exact note from Notes/ (or Inbox/) by its frontmatter ID.

        The identifier is matched against frontmatter; it is never interpreted as a
        filesystem path.
        """
        note_id = str(note_id).strip()
        if not note_id:
            raise ValueError("note_id cannot be empty")
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        max_chars = min(int(max_chars), 30000)
        matches: list[tuple[Path, dict, str, str]] = []
        for path, status in self._note_files(include_inbox):
            try:
                metadata = _read_frontmatter(path)
                relative = path.relative_to(self.vault).as_posix()
                if str(metadata.get("id") or _stable_id(relative)) != note_id:
                    continue
                if not _status_allowed(metadata, status):
                    continue
                full_metadata, body = parse_note(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            if str(full_metadata.get("id") or _stable_id(relative)) != note_id:
                continue
            matches.append((path, full_metadata, body, status))
        if not matches:
            raise KeyError(f"Unknown note ID: {note_id}")
        if len(matches) != 1:
            raise ValueError(f"Ambiguous note ID {note_id!r}: found {len(matches)} notes")

        path, metadata, body, status = matches[0]
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
            "status": status,
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

    def _scan_notes(self, existing: dict[str, dict]) -> tuple[dict[str, dict], set[str]]:
        """Read changed notes from Notes/ and Inbox/ only.

        Directory traversal still detects additions, moves, and deletions, but unchanged
        Markdown is not opened on every request. File stat metadata is only a fast
        freshness hint; changed files are parsed and hashed before the index is updated.
        The folder decides the status: Inbox/ is unreviewed whatever the frontmatter says.
        """
        notes: dict[str, dict] = {}
        present_paths: set[str] = set()
        for path, status in self._note_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(self.vault).as_posix()
            old = existing.get(relative)
            if (
                old
                and old["status"] == status
                and old["mtime_ns"] == stat.st_mtime_ns
                and old["file_size"] == stat.st_size
                and old["ctime_ns"] == stat.st_ctime_ns
                and old["inode"] == stat.st_ino
            ):
                present_paths.add(relative)
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                metadata, body = parse_note(text)
            except (OSError, ValueError):
                continue
            if not _status_allowed(metadata, status):
                continue
            title_match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
            title = str(metadata.get("title") or (title_match.group(1) if title_match else path.stem))
            present_paths.add(relative)
            notes[relative] = {
                "id": str(metadata.get("id") or _stable_id(relative)),
                "title": title,
                "note_type": str(metadata.get("type") or "note"),
                "status": status,
                "content": text,
                "summary": _section(body, "Summary"),
                "source_url": str(metadata.get("source_url") or ""),
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "mtime_ns": stat.st_mtime_ns,
                "file_size": stat.st_size,
                "ctime_ns": stat.st_ctime_ns,
                "inode": stat.st_ino,
            }
        return notes, present_paths

    def _delete_node(self, db: sqlite3.Connection, node_id: str) -> None:
        chunk_ids = [row[0] for row in db.execute("SELECT id FROM chunks WHERE node_id=?", (node_id,))]
        for chunk_id in chunk_ids:
            db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,))
            if self.vec_enabled and self._vec_table_exists(db):
                db.execute("DELETE FROM chunk_vec WHERE rowid=?", (chunk_id,))
        db.execute("DELETE FROM nodes WHERE id=?", (node_id,))

    def _lexical(self, db: sqlite3.Connection, query: str, *, limit: int) -> list[sqlite3.Row]:
        expression = _fts_expression(query)
        if not expression:
            return []
        return list(
            db.execute(
                "SELECT node_id,text,bm25(chunks_fts, 0.0, 0.0, 1.0, 3.0) AS rank "
                "FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                (expression, limit),
            )
        )


def _query_tokens(query: str) -> list[str]:
    tokens = [token for token in TOKEN_RE.findall(query.lower()) if len(token) > 1]
    distinctive = [token for token in tokens if token not in STOPWORDS]
    return list(dict.fromkeys((distinctive or tokens)[:12]))


def _required_terms(count: int) -> int:
    """How many distinct query terms one chunk must contain to count as a lexical hit."""
    return max(1, math.ceil(count / 2))


def _matched_terms(tokens: list[str], text: str) -> int:
    words = TOKEN_RE.findall(text.lower())
    return sum(any(word.startswith(token) for word in words) for token in tokens)


def _fts_expression(query: str) -> str:
    return " OR ".join(f'"{token.replace(chr(34), "")}"*' for token in _query_tokens(query))


def _status_allowed(metadata: dict, folder_status: str) -> bool:
    """Reject files whose frontmatter contradicts their folder (e.g. rejected notes)."""
    declared = str(metadata.get("status") or "").strip().casefold()
    if not declared:
        return True
    if folder_status == STATUS_APPROVED:
        return declared == STATUS_APPROVED
    return declared in {STATUS_INBOX, STATUS_APPROVED}


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


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0
