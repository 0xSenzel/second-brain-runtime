from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Any

from .index import BrainIndex, OllamaEmbedder
from .service import BrainService

DEFAULT_VAULT = Path.home() / "Documents" / "second-brain"


class BrainAPI:
    def __init__(self, vault: str | Path | None = None, *, use_embeddings: bool = True):
        configured = vault or os.environ.get("SECOND_BRAIN_VAULT") or DEFAULT_VAULT
        self.vault = Path(configured).expanduser().resolve()
        self.service = BrainService(self.vault)
        embedder = OllamaEmbedder(
            url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            model=os.environ.get("SECOND_BRAIN_EMBED_MODEL", "bge-m3"),
        )
        self.embedder = embedder if use_embeddings and embedder.available() else None
        self.index = BrainIndex(self.vault, embedder=self.embedder)

    def prepare_save(
        self,
        *,
        content_or_url: str,
        note_type: str | None = None,
        domain: str | None = None,
        title: str | None = None,
        summary: str = "",
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        maps: list[str] | None = None,
        connections: list[dict[str, str]] | None = None,
        destination: str = "",
        source_creator: str = "",
    ) -> dict[str, Any]:
        proposal = self.service.prepare_input(
            content_or_url,
            note_type=note_type,
            domain=domain,
            title=title,
            summary=summary,
            tags=tags,
            aliases=aliases,
            maps=maps,
            connections=connections,
            destination=destination,
            source_creator=source_creator,
        )
        # Report the rule violations now, at proposal time, so they are visible in the
        # preview the user reads rather than surfacing as a failure after they approve.
        blocking = self.service.explain_blockers(proposal)
        return {
            "proposal_id": proposal.id,
            "status": proposal.status,
            "title": proposal.title,
            "note_type": proposal.note_type,
            "domain": proposal.domain,
            "destination": proposal.target_path,
            "source_destination": proposal.source_path,
            "summary": proposal.summary,
            "tags": proposal.tags,
            "aliases": proposal.aliases,
            "maps": proposal.maps,
            "source_kind": proposal.source_kind,
            "connections": [
                {"relation": item.relation, "target": item.target, "reason": item.reason}
                for item in proposal.connections
            ],
            "is_revision": proposal.is_revision,
            "review_note": f"Reviews/Pending/{proposal.id}.md",
            "approval_required": True,
            "blocking_problems": blocking,
            "commit_would_succeed": not blocking,
        }

    def register_tags(self, tags: list[str], meanings: dict[str, str] | None = None) -> dict[str, Any]:
        added = self.service.register_tags(tags, meanings=meanings)
        return {"added": added, "registered_tags": sorted(self.service.registered_tags())}

    def validate(self) -> dict[str, Any]:
        return self.service.validate()

    def commit_save(self, proposal_id: str, *, approved: bool) -> dict[str, Any]:
        if approved is not True:
            raise PermissionError("Explicit user approval is required before saving approved knowledge")
        saved = self.service.commit_save(proposal_id)
        index_result = self.index.reindex()
        return {
            "proposal_id": saved.proposal_id,
            "status": saved.status,
            "note_path": saved.note_path,
            "source_path": saved.source_path,
            "obsidian_uri": self._uri(saved.note_path),
            "index": index_result,
        }

    def reject_save(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.service.reject_save(proposal_id)
        return {"proposal_id": proposal.id, "status": proposal.status}

    def pending(self) -> list[dict[str, Any]]:
        return [
            {
                "proposal_id": proposal.id,
                "title": proposal.title,
                "note_type": proposal.note_type,
                "domain": proposal.domain,
                "destination": proposal.target_path,
                "review_note": f"Reviews/Pending/{proposal.id}.md",
            }
            for proposal in self.service.list_pending()
        ]

    def ask(self, query: str, *, max_tokens: int = 2500, hops: int = 1) -> dict[str, Any]:
        self.index.reindex()
        bundle = self.index.context(query, max_tokens=max_tokens, hops=hops)
        for source in bundle["sources"]:
            source["obsidian_uri"] = self._uri(source["path"])
        bundle["instruction"] = "Answer from the evidence only and cite the supplied Obsidian notes."
        return bundle

    def find(self, query: str, *, limit: int = 6, hops: int = 0) -> dict[str, Any]:
        self.index.reindex()
        results = [hit.as_dict() for hit in self.index.search(query, limit=limit, hops=hops)]
        for result in results:
            result["obsidian_uri"] = self._uri(result["path"])
        return {"query": query, "results": results}

    def neighbors(self, note_id: str) -> dict[str, Any]:
        return {"note_id": note_id, "neighbors": self.index.neighbors(note_id)}

    def open_note(self, path: str) -> dict[str, str]:
        return {"path": path, "obsidian_uri": self._uri(path)}

    def reindex(self) -> dict[str, Any]:
        return self.index.reindex()

    def doctor(self) -> dict[str, Any]:
        return {
            "vault": str(self.vault),
            "vault_exists": self.vault.is_dir(),
            "pending_proposals": len(self.service.list_pending()),
            "embeddings": "available" if self.embedder else "unavailable; lexical and graph fallback active",
            "embedding_model": getattr(self.embedder, "model", None),
            "database": str(self.index.db_path),
            "registered_tags": len(self.service.registered_tags()),
            "unmigrated_folders": self.service.unmigrated_folders(),
        }

    def _uri(self, path: str) -> str:
        vault_name = urllib.parse.quote(self.vault.name, safe="")
        file_path = urllib.parse.quote(path, safe="")
        return f"obsidian://open?vault={vault_name}&file={file_path}"
