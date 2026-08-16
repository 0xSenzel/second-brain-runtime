from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .ingest import SourceIngestor
from .models import Connection, Proposal, SavedItem
from .notes import read_note, render_knowledge, render_review, render_source, safe_filename

CANONICAL_FOLDERS = {
    "prompt": "Prompts",
    "skill": "Skills",
    "project": "Projects",
    "resource": "Resources",
    "ai-coding-resource": "Resources/AI Coding",
    "frontend-resource": "Knowledge/Frontend Resources",
}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class BrainService:
    def __init__(self, vault: str | Path):
        self.vault = Path(vault).expanduser().resolve()
        self.state_dir = self.vault / ".second-brain"
        self.proposal_dir = self.state_dir / "proposals"
        self._ensure_layout()

    def prepare_input(
        self,
        value: str,
        *,
        kind: str | None = None,
        title: str | None = None,
        summary: str = "",
        tags: Iterable[str] | None = None,
        connections: Iterable[Connection | dict] | None = None,
    ) -> Proposal:
        source = SourceIngestor().ingest(value, kind=kind)
        return self.prepare_save(
            kind=kind or source.kind,
            content=source.content,
            title=title or source.title,
            summary=summary,
            tags=tags,
            connections=connections,
            source_url=source.source_url,
        )

    def _ensure_layout(self) -> None:
        folders = [
            "Inbox",
            "Sources",
            "Knowledge",
            "Knowledge/Frontend Resources",
            "Prompts",
            "Resources",
            "Resources/AI Coding",
            "Skills",
            "Projects",
            "Reviews/Pending",
            "Reviews/Approved",
            "Reviews/Rejected",
            "Templates",
        ]
        for folder in folders:
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        self.proposal_dir.mkdir(parents=True, exist_ok=True)

    def prepare_save(
        self,
        *,
        kind: str,
        content: str,
        title: str,
        summary: str = "",
        tags: Iterable[str] | None = None,
        connections: Iterable[Connection | dict] | None = None,
        source_url: str = "",
    ) -> Proposal:
        if not content.strip():
            raise ValueError("Cannot save empty content")
        kind = kind.strip().lower() or "note"
        title = safe_filename(title)
        proposal_id = uuid.uuid4().hex[:12]
        created_at = utc_now()
        parsed_connections = [Connection.from_value(item) for item in (connections or [])]
        target_path, is_revision = self._target_for(kind, title, source_url)
        source_path = f"Sources/{safe_filename(kind.title())}/{proposal_id}-{title}.md"
        proposal = Proposal(
            id=proposal_id,
            kind=kind,
            title=title,
            summary=summary.strip(),
            content=content.rstrip(),
            tags=sorted({str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()}),
            connections=parsed_connections,
            source_url=source_url.strip(),
            source_path=source_path,
            target_path=target_path,
            created_at=created_at,
            is_revision=is_revision,
        )
        self._proposal_json_path(proposal.id).write_text(
            json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._review_path("Pending", proposal.id).write_text(render_review(proposal), encoding="utf-8")
        return proposal

    def commit_save(self, proposal_id: str) -> SavedItem:
        proposal = self._load_proposal(proposal_id)
        if proposal.status != "pending":
            raise ValueError(f"Proposal {proposal_id} is not pending")
        target = self._inside_vault(proposal.target_path)
        source = self._inside_vault(proposal.source_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.parent.mkdir(parents=True, exist_ok=True)

        note_id = f"note-{proposal.id}"
        if target.exists():
            metadata, _ = read_note(target)
            note_id = str(metadata.get("id") or note_id)

        source.write_text(render_source(proposal, note_id), encoding="utf-8")
        target.write_text(render_knowledge(proposal, note_id), encoding="utf-8")
        self._finish_review(proposal, "Approved")
        return SavedItem(
            proposal_id=proposal.id,
            note_path=proposal.target_path,
            source_path=proposal.source_path,
        )

    def reject_save(self, proposal_id: str) -> Proposal:
        proposal = self._load_proposal(proposal_id)
        self._finish_review(proposal, "Rejected")
        proposal.status = "rejected"
        return proposal

    def list_pending(self) -> list[Proposal]:
        proposals: list[Proposal] = []
        for path in sorted(self.proposal_dir.glob("*.json")):
            proposal = Proposal.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if proposal.status == "pending" and self._review_path("Pending", proposal.id).exists():
                proposals.append(proposal)
        return proposals

    def _target_for(self, kind: str, title: str, source_url: str) -> tuple[str, bool]:
        if source_url:
            existing = self._find_by_source_url(source_url)
            if existing:
                return existing, True
        folder = CANONICAL_FOLDERS.get(kind, "Knowledge")
        return f"{folder}/{title}.md", False

    def _find_by_source_url(self, source_url: str) -> str | None:
        for folder in ("Knowledge", "Prompts", "Resources", "Skills", "Projects"):
            for path in (self.vault / folder).rglob("*.md"):
                metadata, _ = read_note(path)
                if metadata.get("source_url") == source_url:
                    return path.relative_to(self.vault).as_posix()
        return None

    def _finish_review(self, proposal: Proposal, destination: str) -> None:
        pending = self._review_path("Pending", proposal.id)
        completed = self._review_path(destination, proposal.id)
        completed.parent.mkdir(parents=True, exist_ok=True)
        proposal.status = destination.lower()
        completed.write_text(render_review(proposal), encoding="utf-8")
        if pending.exists():
            pending.unlink()
        self._proposal_json_path(proposal.id).write_text(
            json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _load_proposal(self, proposal_id: str) -> Proposal:
        path = self._proposal_json_path(proposal_id)
        if not path.exists():
            raise KeyError(f"Unknown proposal: {proposal_id}")
        return Proposal.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _proposal_json_path(self, proposal_id: str) -> Path:
        if not proposal_id.isalnum():
            raise ValueError("Invalid proposal ID")
        return self.proposal_dir / f"{proposal_id}.json"

    def _review_path(self, status: str, proposal_id: str) -> Path:
        return self.vault / "Reviews" / status / f"{proposal_id}.md"

    def _inside_vault(self, relative_path: str) -> Path:
        path = (self.vault / relative_path).resolve()
        if not path.is_relative_to(self.vault):
            raise ValueError("Path escapes the vault")
        return path
