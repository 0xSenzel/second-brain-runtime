from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .ingest import SourceIngestor
from .models import (
    FOLDER_FOR_TYPE,
    MAPS_FOLDER,
    RETIRED_FOLDERS,
    SOURCES_FOLDER,
    TAG_REGISTRY_PATH,
    Connection,
    Proposal,
    SavedItem,
    ValidationError,
    normalize_tag,
    validate_destination,
    validate_note_type,
)
from .notes import (
    append_to_map,
    dump_note,
    read_note,
    render_knowledge,
    render_map,
    render_review,
    render_source,
    safe_filename,
)
from .validation import check_proposal, lint_vault

# Folders holding distilled notes. Anything that resolves a note by path or URL must
# search all of them; the old hardcoded list omitted folders and silently duplicated notes.
CONTENT_FOLDERS = tuple(dict.fromkeys(FOLDER_FOR_TYPE.values())) + (MAPS_FOLDER,)

VAULT_FOLDERS = (
    *CONTENT_FOLDERS,
    SOURCES_FOLDER,
    "Reviews/Pending",
    "Reviews/Approved",
    "Reviews/Rejected",
    "Templates",
)

TAG_REGISTRY_HEADING = "## Registered tags"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class BrainService:
    def __init__(self, vault: str | Path):
        self.vault = Path(vault).expanduser().resolve()
        self.state_dir = self.vault / ".second-brain"
        self.proposal_dir = self.state_dir / "proposals"
        self._ensure_layout()

    # -- layout ----------------------------------------------------------------

    def _ensure_layout(self) -> None:
        for folder in VAULT_FOLDERS:
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        self.proposal_dir.mkdir(parents=True, exist_ok=True)
        self._retire_old_folders()

    def _retire_old_folders(self) -> None:
        """Drop the folders the old kind-to-folder mapping created.

        Only when they hold no Markdown, so an unfinished migration is never deleted:
        a folder with notes still in it is left alone and reported by `doctor`.
        """
        for name in RETIRED_FOLDERS:
            folder = self.vault / name
            if not folder.is_dir():
                continue
            if any(folder.rglob("*.md")):
                continue
            for child in sorted(folder.rglob("*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
                else:
                    child.unlink()
            folder.rmdir()

    def unmigrated_folders(self) -> list[str]:
        return [
            name
            for name in RETIRED_FOLDERS
            if (self.vault / name).is_dir() and any((self.vault / name).rglob("*.md"))
        ]

    # -- tag registry ----------------------------------------------------------

    def registered_tags(self) -> set[str]:
        path = self.vault / TAG_REGISTRY_PATH
        if not path.exists():
            return set()
        _, body = read_note(path)
        return {
            normalize_tag(match.group(1))
            for match in re.finditer(r"(?m)^\s*-\s*`([^`]+)`", body)
        }

    def register_tags(self, tags: Iterable[str], *, meanings: dict[str, str] | None = None) -> list[str]:
        """Add tags to the registry. Returns the tags that were newly added."""
        meanings = meanings or {}
        path = self.vault / TAG_REGISTRY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.registered_tags()
        incoming = [normalize_tag(tag) for tag in tags if normalize_tag(tag)]
        added = [tag for tag in dict.fromkeys(incoming) if tag not in existing]
        if not path.exists():
            path.write_text(
                dump_note(
                    {
                        "id": "map-tag-registry",
                        "type": "map",
                        "status": "approved",
                        "created": utc_now(),
                        "updated": utc_now(),
                        "title": "Tag Registry",
                        "aliases": ["Tag vocabulary", "Controlled tag list"],
                        "tags": ["map", "vocabulary"],
                    },
                    "# Tag Registry\n\nThe controlled vocabulary. A note cannot be saved with a "
                    "tag that is not listed here, so adding a tag is a deliberate act.\n\n"
                    f"{TAG_REGISTRY_HEADING}\n",
                ),
                encoding="utf-8",
            )
        if not added:
            return []
        text = path.read_text(encoding="utf-8")
        lines = "\n".join(
            f"- `{tag}` — {meanings.get(tag, 'No description yet.')}" for tag in added
        )
        if TAG_REGISTRY_HEADING in text:
            head, _, tail = text.partition(TAG_REGISTRY_HEADING)
            body = tail.lstrip("\n").rstrip()
            text = f"{head}{TAG_REGISTRY_HEADING}\n\n{(body + chr(10)) if body else ''}{lines}\n"
        else:
            text = f"{text.rstrip()}\n\n{TAG_REGISTRY_HEADING}\n\n{lines}\n"
        path.write_text(text, encoding="utf-8")
        return added

    # -- capture ---------------------------------------------------------------

    def prepare_input(
        self,
        value: str,
        *,
        note_type: str | None = None,
        title: str | None = None,
        summary: str = "",
        tags: Iterable[str] | None = None,
        aliases: Iterable[str] | None = None,
        maps: Iterable[str] | None = None,
        connections: Iterable[Connection | dict] | None = None,
        destination: str = "",
        source_creator: str = "",
    ) -> Proposal:
        source = SourceIngestor().ingest(value)
        return self.prepare_save(
            note_type=note_type or "concept",
            content=source.content,
            title=title or source.title,
            summary=summary,
            tags=tags,
            aliases=aliases,
            maps=maps,
            connections=connections,
            source_url=source.source_url,
            source_kind=source.kind,
            source_creator=source_creator,
            destination=destination,
        )

    def prepare_save(
        self,
        *,
        note_type: str,
        content: str,
        title: str,
        summary: str = "",
        tags: Iterable[str] | None = None,
        aliases: Iterable[str] | None = None,
        maps: Iterable[str] | None = None,
        connections: Iterable[Connection | dict] | None = None,
        source_url: str = "",
        source_kind: str = "",
        source_creator: str = "",
        destination: str = "",
    ) -> Proposal:
        if not content.strip():
            raise ValidationError("Cannot save empty content")
        note_type = validate_note_type(note_type)
        title = safe_filename(title)
        proposal_id = uuid.uuid4().hex[:12]
        created_at = utc_now()
        parsed_connections = [Connection.from_value(item) for item in (connections or [])]
        target_path, is_revision = self._target_for(note_type, title, source_url, destination)
        proposal = Proposal(
            id=proposal_id,
            note_type=note_type,
            title=title,
            summary=summary.strip(),
            content=content.rstrip(),
            tags=sorted({normalize_tag(tag) for tag in (tags or []) if normalize_tag(tag)}),
            aliases=[str(alias).strip() for alias in (aliases or []) if str(alias).strip()],
            maps=[str(name).strip() for name in (maps or []) if str(name).strip()],
            connections=parsed_connections,
            source_url=source_url.strip(),
            source_kind=source_kind.strip().lower(),
            source_creator=source_creator.strip(),
            # Sources are addressed by ID, never browsed by subject, so this stays flat.
            source_path=f"{SOURCES_FOLDER}/{proposal_id}-{title}.md",
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
            raise ValidationError(f"Proposal {proposal_id} is not pending")

        # The gate. Checked before any file is opened, so a rejected proposal leaves
        # the vault exactly as it was and stays pending for the agent to correct.
        check_proposal(proposal, registered_tags=self.registered_tags())

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
        self._update_maps(proposal)
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

    def explain_blockers(self, proposal: Proposal) -> list[str]:
        """The rules this proposal currently fails, without raising.

        Lets the preview say what is missing while it can still be fixed, instead of
        letting the user approve something that will then refuse to commit.
        """
        try:
            check_proposal(proposal, registered_tags=self.registered_tags())
        except ValidationError as error:
            return [line.strip(" -") for line in str(error).splitlines()[1:] if line.strip()]
        return []

    def validate(self) -> dict:
        result = lint_vault(
            self.vault,
            registered_tags=self.registered_tags(),
            folders=CONTENT_FOLDERS,
        )
        result["unmigrated_folders"] = self.unmigrated_folders()
        return result

    # -- internals -------------------------------------------------------------

    def _update_maps(self, proposal: Proposal) -> None:
        """Write the note into each of its maps, creating any that do not exist yet."""
        for name in proposal.maps:
            path = self.vault / MAPS_FOLDER / f"{safe_filename(name)}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(render_map(name, proposal.created_at), encoding="utf-8")
            path.write_text(
                append_to_map(path.read_text(encoding="utf-8"), proposal.title, proposal.summary),
                encoding="utf-8",
            )

    def _target_for(
        self, note_type: str, title: str, source_url: str, destination: str
    ) -> tuple[str, bool]:
        if source_url:
            existing = self._find_by_source_url(source_url)
            if existing:
                return existing, True
        if destination:
            return validate_destination(destination, note_type), False
        return f"{FOLDER_FOR_TYPE[note_type]}/{title}.md", False

    def _find_by_source_url(self, source_url: str) -> str | None:
        for folder in CONTENT_FOLDERS:
            root = self.vault / folder
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.md")):
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
            raise ValidationError("Path escapes the vault")
        return path
