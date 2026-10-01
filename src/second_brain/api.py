from __future__ import annotations

import os
import pathlib
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

from .index import BrainIndex, OllamaEmbedder
from .models import SCHEMA_VERSION
from .service import BrainService

DEFAULT_VAULT = Path.home() / "Documents" / "second-brain"


# Capture leaves scratch media outside the vault — the post's video plus the
# frame directory, contact sheet and audio the analysis step writes beside it.
# Once a proposal is decided that material has served its purpose: the note or
# the rejection is the durable outcome. Opt-in, because a knowledge vault should
# not delete files outside itself unless it was told where they are.
_CAPTURE_CACHE_ENV = "SECOND_BRAIN_CAPTURE_CACHE"


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
        body: str | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        maps: list[str] | None = None,
        connections: list[dict[str, str]] | None = None,
        destination: str = "",
        source_url: str = "",
        source_kind: str = "",
        source_creator: str = "",
    ) -> dict[str, Any]:
        proposal = self.service.prepare_input(
            content_or_url,
            note_type=note_type,
            domain=domain,
            title=title,
            summary=summary,
            body=body,
            tags=tags,
            aliases=aliases,
            maps=maps,
            connections=connections,
            destination=destination,
            source_url=source_url,
            source_kind=source_kind,
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
            "body": proposal.body,
            "tags": proposal.tags,
            "aliases": proposal.aliases,
            "maps": proposal.maps,
            "source_kind": proposal.source_kind,
            "source_url": proposal.source_url,
            "source_creator": proposal.source_creator,
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
        # Read before committing: the proposal moves out of Pending on success.
        proposal = self.service.get_proposal(proposal_id)
        saved = self.service.commit_save(proposal_id)
        index_result = self.index.reindex()
        return {
            "proposal_id": saved.proposal_id,
            "status": saved.status,
            "note_path": saved.note_path,
            "source_path": saved.source_path,
            "obsidian_uri": self._uri(saved.note_path),
            "index": index_result,
            "scratch_purged": _purge_capture_scratch(getattr(proposal, "source_url", "")),
        }

    def reject_save(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.service.reject_save(proposal_id)
        return {
            "proposal_id": proposal.id,
            "status": proposal.status,
            "scratch_purged": _purge_capture_scratch(getattr(proposal, "source_url", "")),
        }

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
            "schema_version": SCHEMA_VERSION,
            "schema_mismatch": self.service.schema_mismatch(),
        }

    def _uri(self, path: str) -> str:
        vault_name = urllib.parse.quote(self.vault.name, safe="")
        file_path = urllib.parse.quote(path, safe="")
        return f"obsidian://open?vault={vault_name}&file={file_path}"
