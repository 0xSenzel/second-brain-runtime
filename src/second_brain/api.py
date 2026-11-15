from __future__ import annotations

import os
import pathlib
import re
import shutil
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .deletion import DeleteWorkflow
from .index import BrainIndex
from .ingest import SourceIngestor
from .models import SCHEMA_VERSION, TAG_REGISTRY_PATH, Connection
from .service import BrainService

try:  # the embedding module is owned by the retrieval stream
    from .embedding import EmbeddingWorker, OllamaEmbedder
except ImportError:  # pragma: no cover - schema-3 layout
    from .index import OllamaEmbedder

    EmbeddingWorker = None

DEFAULT_VAULT = Path.home() / "Documents" / "second-brain"


# Capture leaves scratch media outside the vault — the post's video plus the
# frame directory, contact sheet and audio the analysis step writes beside it.
# Once a proposal is decided that material has served its purpose: the note or
# the rejection is the durable outcome. Opt-in, because a knowledge vault should
# not delete files outside itself unless it was told where they are.
_CAPTURE_CACHE_ENV = "SECOND_BRAIN_CAPTURE_CACHE"

READ_ONLY_ERROR = {"error": "read-only: writes go through the brain service"}
MODES = ("writer", "reader")
APPROVAL_MODES = ("chat", "human")


def _purge_capture_scratch(source_url: str) -> int:
    """Delete scratch belonging to one captured post. Returns entries removed."""
    cache_dir = os.environ.get(_CAPTURE_CACHE_ENV, "").strip()
    if not cache_dir:
        return 0
    cache = pathlib.Path(cache_dir)
    if not cache.is_dir():
        return 0
    # Every artefact is named for the post, so the trailing id in the canonical
    # URL identifies the set. No id means nothing safe to match on.
    match = re.search(r"(\d{6,})", source_url or "")
    if not match:
        return 0
    post_id = match.group(1)
    removed = 0
    for entry in cache.glob(f"{post_id}*"):
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


class BrainAPI:
    """Surface-facing facade over the service (writes) and the index (reads).

    Collaborators can be injected so tests and the parent's integration run do not
    need a real vault. Reader mode never writes the vault.
    """

    def __init__(
        self,
        vault: str | Path | None = None,
        *,
        service: Any = None,
        index: Any = None,
        embedder: Any = None,
        mode: str | None = None,
        approval: str | None = None,
        use_embeddings: bool = True,
        deleter: Any = None,
        ingestor: Any = None,
    ):
        configured = vault or os.environ.get("SECOND_BRAIN_VAULT") or DEFAULT_VAULT
        self.vault = Path(configured).expanduser().resolve()
        self.mode = (mode or os.environ.get("SECOND_BRAIN_MODE") or "writer").strip().lower()
        if self.mode not in MODES:
            raise ValueError(f"SECOND_BRAIN_MODE must be one of {MODES}, got {self.mode!r}")
        self.approval = (approval or os.environ.get("SECOND_BRAIN_APPROVAL") or "chat").strip().lower()
        if self.approval not in APPROVAL_MODES:
            raise ValueError(f"SECOND_BRAIN_APPROVAL must be one of {APPROVAL_MODES}, got {self.approval!r}")
        self.ingestor = ingestor or SourceIngestor()
        self._deleter = deleter
        # Service construction may create vault folders, so a reader only builds one
        # for a vault that already exists (it needs the tag catalog) and never otherwise.
        if service is not None:
            self.service = service
        elif self.mode == "reader" and not self.vault.is_dir():
            self.service = None
        else:
            self.service = BrainService(self.vault)
        if embedder is None and index is None and use_embeddings:
            embedder = OllamaEmbedder(
                url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
                model=os.environ.get("SECOND_BRAIN_EMBED_MODEL", "bge-m3"),
            )
        # Connectivity is probed only when embeddings are actually requested.
        self.embedder = embedder if embedder is not None else getattr(index, "embedder", None)
        self.index = index if index is not None else BrainIndex(self.vault, embedder=self.embedder)

    # -- mode and approval ------------------------------------------------------------

    @property
    def read_only(self) -> bool:
        return self.mode == "reader"

    def _check_approval(self, approved: bool, what: str, *, out_of_band: bool = False) -> None:
        if approved is not True:
            raise PermissionError(f"Explicit user approval is required before {what}")
        if self.approval == "human" and not out_of_band:
            raise PermissionError(
                "Approval is human-only on this server (SECOND_BRAIN_APPROVAL=human). "
                "Ask the user to approve in Obsidian (set status: approved) or run "
                "`brain approve <note_id>` on the CLI."
            )

    def _refresh(self) -> list[str]:
        """Promote notes a human approved out of band. Never raises."""
        if self.read_only or self.service is None:
            return []
        try:
            promoted = self.service.promote_reviewed()
        except Exception as error:
            return [f"promote_reviewed failed: {error}"]
        warnings: list[str] = []
        for item in promoted or []:
            if isinstance(item, dict) and item.get("error"):
                warnings.append(f"promotion: {item['error']}")
        if promoted:
            try:
                self.index.reindex()
            except Exception as error:
                warnings.append(f"reindex after promotion failed: {error}")
        return warnings

    def _reindex_for_read(self) -> list[str]:
        try:
            result = self.index.reindex(embed_vectors=False)
        except TypeError:
            result = self.index.reindex()
        return [f"Indexing warning: {w}" for w in (result or {}).get("warnings", [])]

    # -- capture and review -----------------------------------------------------------

    def capture(
        self,
        content_or_url: str,
        *,
        title: str = "",
        summary: str = "",
        body: str = "",
        note_type: str = "concept",
        domain: str = "tech",
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        connections: list[dict[str, str]] | None = None,
        source_url: str = "",
        source_kind: str = "",
        source_creator: str = "",
        revises: str = "",
    ) -> dict[str, Any]:
        if self.read_only:
            return dict(READ_ONLY_ERROR)
        ingested = self.ingestor.ingest(content_or_url, kind=source_kind or None)
        result = self.service.capture(
            content=ingested.content,
            title=(title or ingested.title or "").strip(),
            summary=summary,
            body=body or "",
            note_type=note_type or "concept",
            domain=domain or "tech",
            tags=tuple(tags or ()),
            aliases=tuple(aliases or ()),
            connections=tuple(Connection.from_value(item) for item in (connections or ())),
            # Explicit provenance wins over what ingestion derived.
            source_url=source_url or ingested.source_url,
            source_kind=source_kind or ingested.kind,
            source_creator=source_creator,
            revises=revises,
        )
        return self._written(result, status="inbox")

    def update_inbox(self, note_id: str, **fields: Any) -> dict[str, Any]:
        if self.read_only:
            return dict(READ_ONLY_ERROR)
        allowed = {"title", "summary", "body", "note_type", "domain", "tags", "aliases", "connections"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown inbox fields: {sorted(unknown)}")
        changes = {key: value for key, value in fields.items() if value is not None}
        if changes.get("connections") is not None:
            changes["connections"] = [Connection.from_value(item) for item in changes["connections"]]
        return self._written(self.service.update_inbox(note_id, **changes), status="inbox")

    def inbox(self, note_id: str = "") -> Any:
        warnings = self._refresh()
        if note_id:
            item = asdict(self.service.get_inbox(note_id))
            item["obsidian_uri"] = self._uri(item["note_path"])
            return {"item": item, "warnings": warnings}
        items = []
        for item in self.service.list_inbox():
            entry = asdict(item)
            entry.pop("text", None)
            entry.pop("body", None)
            entry["obsidian_uri"] = self._uri(entry["note_path"])
            items.append(entry)
        return {"count": len(items), "items": items, "warnings": warnings}

    def approve(
        self, note_id: str, content_sha: str, approved: bool = False, *, out_of_band: bool = False
    ) -> dict[str, Any]:
        if self.read_only:
            return dict(READ_ONLY_ERROR)
        self._check_approval(approved, "approving a note", out_of_band=out_of_band)
        source_url = self._inbox_source_url(note_id)
        result = self.service.approve(note_id, content_sha=content_sha)
        payload = asdict(result)
        payload["status"] = "approved"
        payload["obsidian_uri"] = self._uri(result.note_path)
        payload["index"] = self.index.reindex()
        payload["scratch_purged"] = _purge_capture_scratch(source_url)
        return payload

    def discard(self, note_id: str, *, reason: str = "") -> dict[str, Any]:
        if self.read_only:
            return dict(READ_ONLY_ERROR)
        source_url = self._inbox_source_url(note_id)
        result = self.service.discard(note_id, reason=reason)
        payload = asdict(result)
        payload["index"] = self.index.reindex()
        payload["scratch_purged"] = _purge_capture_scratch(source_url)
        return payload

    def promote(self) -> dict[str, Any]:
        if self.read_only:
            return dict(READ_ONLY_ERROR)
        promoted = [asdict(item) if hasattr(item, "__dataclass_fields__") else item
                    for item in self.service.promote_reviewed() or []]
        return {"promoted": promoted, "index": self.index.reindex()}

    def _inbox_source_url(self, note_id: str) -> str:
        try:
            return str(self.service.get_inbox(note_id).source_url or "")
        except Exception:
            return ""

    def _written(self, result: Any, *, status: str) -> dict[str, Any]:
        payload = asdict(result)
        payload["status"] = status
        payload["obsidian_uri"] = self._uri(result.note_path)
        payload["index"] = self.index.reindex()
        return payload

    # -- tags -------------------------------------------------------------------------

    def register_tags(self, tags: list[str], meanings: dict[str, str] | None = None) -> dict[str, Any]:
        if self.read_only:
            return dict(READ_ONLY_ERROR)
        added = self.service.register_tags(tags, meanings=meanings)
        return {"added": added, "registered_tags": sorted(self.service.registered_tags())}

    def list_tags(self) -> dict[str, Any]:
        catalog = self.service.tag_catalog() if self.service is not None else {}
        return {
            "registry_path": TAG_REGISTRY_PATH,
            "tags": [{"name": name, "meaning": catalog[name]} for name in sorted(catalog)],
        }

    def validate(self) -> dict[str, Any]:
        warnings = self._refresh()
        result = dict(self.service.validate())
        if warnings:
            result["warnings"] = warnings
        return result

    # -- digest and report ------------------------------------------------------------

    def digest(self) -> dict[str, Any]:
        from . import synthesis

        warnings = self._refresh()
        data = synthesis.digest(self.service, self.index)
        data["text"] = synthesis.render_digest(data)
        if warnings:
            data["warnings"] = warnings
        return data

    def report(self, *, stale_days: int = 180) -> dict[str, Any]:
        from . import synthesis

        warnings = self._refresh()
        data = synthesis.report(
            self.service, self.index, stale_days=stale_days, similar_pairs=self._similar_pairs()
        )
        if warnings:
            data["warnings"] = warnings
        return data

    def _similar_pairs(self) -> Any:
        """A callable returning (id_a, id_b, cosine) tuples, or None without vectors."""
        finder = getattr(self.index, "similar_pairs", None)
        return finder if callable(finder) else None

    # -- deletion ---------------------------------------------------------------------

    def _delete_workflow(self) -> Any:
        if self._deleter is None:
            self._deleter = DeleteWorkflow(self.vault)
        return self._deleter

    def prepare_delete(self, note_id: str, *, reason: str = "") -> dict[str, Any]:
        """Stage a recoverable deletion review without changing canonical files."""
        if self.read_only:
            return dict(READ_ONLY_ERROR)
        return self._delete_workflow().prepare(note_id, reason=reason)

    def commit_delete(self, proposal_id: str, *, approved: bool) -> dict[str, Any]:
        """Archive a staged note only after explicit approval of its proposal ID."""
        if self.read_only:
            return dict(READ_ONLY_ERROR)
        self._check_approval(approved, "deleting approved knowledge")
        result = self._delete_workflow().commit(proposal_id)
        result["index"] = self.index.reindex()
        return result

    # -- retrieval --------------------------------------------------------------------

    def ask(self, query: str, *, max_tokens: int = 4000, hops: int = 0) -> dict[str, Any]:
        warnings = self._refresh()
        warnings += self._reindex_for_read()
        bundle = self.index.context(query, max_tokens=max_tokens, hops=hops, include_inbox=True)
        bundle.setdefault("warnings", [])
        bundle["warnings"].extend(warnings)
        for source in bundle.get("sources", []):
            source["obsidian_uri"] = self._uri(source["path"])
        bundle["instruction"] = (
            "Answer from the evidence only and cite the supplied Obsidian notes. "
            "Sources marked unreviewed are inbox captures that no one has approved."
        )
        return bundle

    def find(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        warnings = self._refresh()
        warnings += self._reindex_for_read()
        limit = min(max(int(limit), 1), 10)
        found = self.index.find(query, limit=limit, include_inbox=True)
        if isinstance(found, dict):
            found = found.get("results", [])
        results = [hit.as_dict() if hasattr(hit, "as_dict") else dict(hit) for hit in found]
        for result in results:
            result["obsidian_uri"] = self._uri(result["path"])
        return {"query": query, "results": results, "warnings": warnings}

    def read(self, note_id: str, *, section: str = "", max_chars: int = 12000) -> dict[str, Any]:
        warnings = self._refresh()
        result = self.index.read_note(note_id, section=section, max_chars=max_chars)
        if warnings:
            result = {**result, "warnings": warnings}
        return result

    def neighbors(self, note_id: str) -> dict[str, Any]:
        warnings = self._refresh()
        result = {"note_id": note_id, "neighbors": self.index.neighbors(note_id)}
        if warnings:
            result["warnings"] = warnings
        return result

    def open_note(self, path: str) -> dict[str, str]:
        return {"path": path, "obsidian_uri": self._uri(path)}

    # -- index maintenance ------------------------------------------------------------

    def reindex(self) -> dict[str, Any]:
        return self.index.reindex()

    def embed(self) -> dict[str, Any]:
        if self.read_only:
            return dict(READ_ONLY_ERROR)
        if EmbeddingWorker is None:
            raise RuntimeError("The embedding worker is not available in this build")
        self.index.reindex()
        embedded = EmbeddingWorker(self.index, self.embedder).run_once()
        return {"embedded_chunks": embedded}

    def doctor(self) -> dict[str, Any]:
        service = self.service
        embeddings_available = False
        if self.embedder is not None:
            try:
                embeddings_available = bool(self.embedder.available())
            except Exception:
                embeddings_available = False
        coverage = getattr(self.index, "embedding_coverage", None)
        try:
            coverage = coverage() if callable(coverage) else None
        except Exception as error:
            coverage = {"error": str(error)}
        schema_file = self.vault / ".brain-schema.json"
        return {
            "vault": str(self.vault),
            "vault_exists": self.vault.is_dir(),
            "mode": self.mode,
            "approval": self.approval,
            "schema_version": SCHEMA_VERSION,
            "schema_file": schema_file.read_text(encoding="utf-8").strip() if schema_file.is_file() else None,
            "schema_mismatch": service.schema_mismatch() if hasattr(service, "schema_mismatch") else None,
            "database": str(getattr(self.index, "db_path", "")),
            "registered_tags": len(service.registered_tags()) if service is not None else 0,
            "embeddings": (
                "available" if embeddings_available else "unavailable; lexical and graph fallback active"
            ),
            "ollama_reachable": embeddings_available,
            "embedding_model": getattr(self.embedder, "model", None),
            "embedding_coverage": coverage,
            "git_journal": {
                "git_installed": shutil.which("git") is not None,
                "vault_is_repository": (self.vault / ".git").exists(),
                "autocommit": os.environ.get("SECOND_BRAIN_GIT_AUTOCOMMIT", "1") != "0",
            },
        }

    def _uri(self, path: str) -> str:
        vault_name = urllib.parse.quote(self.vault.name, safe="")
        file_path = urllib.parse.quote(path, safe="")
        return f"obsidian://open?vault={vault_name}&file={file_path}"
