from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .service import BrainService

SKIP_PARTS = {".git", ".obsidian", ".second-brain", "Templates"}


@dataclass(frozen=True)
class MigrationCandidate:
    path: Path
    title: str
    kind: str
    source_url: str
    preview: str

    def as_dict(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "title": self.title,
            "kind": self.kind,
            "source_url": self.source_url,
            "preview": self.preview,
        }


class MigrationPlanner:
    def __init__(self, service: BrainService):
        self.service = service

    def plan(self, source_vault: str | Path, *, limit: int = 20) -> list[MigrationCandidate]:
        source = Path(source_vault).expanduser().resolve()
        if source == self.service.vault:
            raise ValueError("Source and destination vaults must be different")
        if not source.is_dir():
            raise ValueError(f"Source vault does not exist: {source}")
        paths = [
            path
            for path in source.rglob("*.md")
            if not any(part in SKIP_PARTS or part.startswith(".") for part in path.relative_to(source).parts)
        ]
        paths.sort(key=lambda path: (path.stat().st_mtime_ns, path.as_posix()), reverse=True)
        candidates: list[MigrationCandidate] = []
        for path in paths[: max(0, limit)]:
            content = path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"(?m)^#\s+(.+?)\s*$", content)
            title = match.group(1).strip() if match else path.stem
            preview = re.sub(r"\s+", " ", content).strip()[:400]
            candidates.append(
                MigrationCandidate(
                    path=path,
                    title=title,
                    kind="skill" if path.name.casefold() == "skill.md" else "note",
                    source_url=path.as_uri(),
                    preview=preview,
                )
            )
        return candidates

    def apply(self, candidates: list[MigrationCandidate]) -> dict:
        pending_urls = {
            proposal.source_url
            for proposal in self.service.list_pending()
            if proposal.source_url
        }
        created: list[str] = []
        skipped = 0
        for candidate in candidates:
            if candidate.source_url in pending_urls:
                skipped += 1
                continue
            proposal = self.service.prepare_input(
                str(candidate.path),
                kind=candidate.kind,
                title=candidate.title,
                summary=candidate.preview,
                tags=["migration-pilot"],
            )
            pending_urls.add(candidate.source_url)
            created.append(proposal.id)
        return {
            "created_proposals": len(created),
            "proposal_ids": created,
            "skipped_existing": skipped,
            "source_files_modified": 0,
        }
