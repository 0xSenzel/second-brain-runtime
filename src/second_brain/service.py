from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import maps
from .models import (
    ARCHIVE_FOLDER,
    DOMAINS,
    INBOX_FOLDER,
    MAPS_FOLDER,
    NOTE_TYPES,
    NOTES_FOLDER,
    SCHEMA_VERSION,
    SOURCES_FOLDER,
    STATUS_APPROVED,
    STATUS_INBOX,
    TAG_REGISTRY_PATH,
    VAULT_SCHEMA_FILE,
    VIEWS_FOLDER,
    ApprovalMismatch,
    ApproveResult,
    ArchiveResult,
    CaptureResult,
    Connection,
    InboxItem,
    SchemaMismatch,
    ValidationError,
    normalize_tag,
    validate_domain,
    validate_note_type,
)
from .notes import (
    dump_note,
    parse_note,
    parse_sections,
    read_note,
    render_note,
    render_source,
    safe_filename,
)
from .validation import check_for_approval, lint_vault
from .vaultio import GitJournal, VaultLock, atomic_move, atomic_write

ARCHIVE_INBOX = f"{ARCHIVE_FOLDER}/Inbox"
ARCHIVE_NOTES = f"{ARCHIVE_FOLDER}/Notes"
ARCHIVE_SOURCES = f"{ARCHIVE_FOLDER}/Sources"
ARCHIVE_RECEIPTS = f"{ARCHIVE_FOLDER}/Receipts"

VAULT_FOLDERS = (
    INBOX_FOLDER,
    NOTES_FOLDER,
    SOURCES_FOLDER,
    MAPS_FOLDER,
    ARCHIVE_INBOX,
    ARCHIVE_NOTES,
    ARCHIVE_SOURCES,
    ARCHIVE_RECEIPTS,
    VIEWS_FOLDER,
    "Templates",
)

# Top-level folders schema 3 used. Reported by `doctor` when they still hold notes, so an
# unmigrated vault is visible; never removed, because they may hold the user's notes.
LEGACY_FOLDERS = ("Personal", "Tech", "Reviews")

TAG_REGISTRY_HEADING = "## Registered tags"
PROVENANCE_KEYS = ("source_url", "source_kind", "source_creator")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _link_stem(link: Any) -> str:
    return str(link).strip().removeprefix("[[").removesuffix("]]").split("|", 1)[0].strip()


def _merge(*groups: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for group in groups for item in group))


class BrainService:
    def __init__(self, vault: str | Path):
        self.vault = Path(vault).expanduser().resolve()
        self.state_dir = self.vault / ".second-brain"
        self._journal = GitJournal(self.vault)
        self._ensure_layout()

    # -- layout ----------------------------------------------------------------

    def _ensure_layout(self) -> None:
        """Bring the vault to the layout this code expects, if it is ours to change.

        The schema is read first and nothing is created when it disagrees. Folders are only
        ever created here, never removed: the old startup cleanup of "retired" folders
        listed `Notes`, which schema 4 uses for approved notes.
        """
        self.vault.mkdir(parents=True, exist_ok=True)
        self._check_schema()
        if self._schema_mismatch:
            return
        for folder in VAULT_FOLDERS:
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        maps.write_review_base(self.vault)

    def _check_schema(self) -> None:
        """Compare the vault's stamp with this code, writing it only if absent.

        Deliberately does not raise: a mismatch must not take retrieval down with it.
        Writes guard themselves via _require_matching_schema.
        """
        self._schema_mismatch: str | None = None
        path = self.vault / VAULT_SCHEMA_FILE
        if not path.exists():
            self._write_schema()
            return
        try:
            stamp = json.loads(path.read_text(encoding="utf-8"))
            raw = stamp["schema_version"]
        except (OSError, ValueError, KeyError, TypeError):
            # OSError covers the cases this message actually names: an unreadable
            # file, or a directory sitting where the stamp belongs.
            self._schema_mismatch = (
                f"{VAULT_SCHEMA_FILE} is unreadable, so the vault's layout cannot be "
                "confirmed. Delete it to re-stamp if the layout is known good."
            )
            return
        # bool is an int subclass, and int() would truncate 2.9 to a false match.
        if isinstance(raw, bool) or not isinstance(raw, int):
            self._schema_mismatch = (
                f"{VAULT_SCHEMA_FILE} records schema_version {raw!r}, which is not a "
                "whole number, so the vault's layout cannot be confirmed."
            )
            return
        if raw > SCHEMA_VERSION:
            self._schema_mismatch = (
                f"This vault was written by a newer runtime (schema {raw}); this code "
                f"produces schema {SCHEMA_VERSION}. Update the runtime before saving, or "
                "notes will be written where neither version looks for them."
            )
            return
        if raw < SCHEMA_VERSION:
            self._schema_mismatch = (
                f"This vault is on an older schema ({raw}); this code produces "
                f"{SCHEMA_VERSION}. Run `brain migrate-v4` before saving, so new notes do "
                "not land in a layout the existing ones do not use."
            )
            return
        # Same version, different axes: the stamp is evidence of disagreement, so it is
        # reported rather than overwritten by whatever this code happens to believe.
        for name, expected in (("domains", sorted(DOMAINS)), ("note_types", sorted(NOTE_TYPES))):
            found = stamp.get(name)
            if found is not None and sorted(found) != expected:
                self._schema_mismatch = (
                    f"{VAULT_SCHEMA_FILE} claims schema {raw} but lists {name} "
                    f"{sorted(found)}, while this code uses {expected}."
                )
                return

    def _write_schema(self) -> None:
        """Write the stamp, but only when it would actually change.

        It is a tracked file in the notes repository. Rewriting it on every open would
        dirty the working tree on every read, which buries real changes.
        """
        path = self.vault / VAULT_SCHEMA_FILE
        payload = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "domains": sorted(DOMAINS),
                "note_types": sorted(NOTE_TYPES),
            },
            indent=2,
        ) + "\n"
        if path.exists() and path.read_text(encoding="utf-8") == payload:
            return
        atomic_write(path, payload)

    def schema_mismatch(self) -> str | None:
        return self._schema_mismatch

    def _require_matching_schema(self) -> None:
        if self._schema_mismatch:
            raise SchemaMismatch(self._schema_mismatch)

    def unmigrated_folders(self) -> list[str]:
        return [
            name
            for name in LEGACY_FOLDERS
            if (self.vault / name).is_dir() and any((self.vault / name).rglob("*.md"))
        ]

    # -- shared helpers --------------------------------------------------------

    def _lock(self) -> VaultLock:
        return VaultLock(self.vault)

    def _commit(self, message: str, paths: Iterable[Path]) -> list[str]:
        return self._journal.commit(message, paths)

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.vault).as_posix()

    def _free_path(self, folder: str, stem: str) -> Path:
        """`<stem>.md` in `folder`, or `<stem> (2).md` and so on when taken."""
        root = self.vault / folder
        candidate = root / f"{stem}.md"
        count = 2
        while candidate.exists():
            candidate = root / f"{stem} ({count}).md"
            count += 1
        return candidate

    def _notes_in(self, folder: str) -> list[tuple[Path, dict[str, Any], str]]:
        found: list[tuple[Path, dict[str, Any], str]] = []
        root = self.vault / folder
        if not root.is_dir():
            return found
        for path in sorted(root.rglob("*.md")):
            try:
                metadata, body = read_note(path)
            except (OSError, UnicodeError, yaml.YAMLError):
                continue
            found.append((path, metadata, body))
        return found

    def _find_by_id(self, folder: str, note_id: str) -> tuple[Path, dict[str, Any], str]:
        for path, metadata, body in self._notes_in(folder):
            if metadata.get("id") == note_id:
                return path, metadata, body
        raise KeyError(f"Unknown note id in {folder}/: {note_id}")

    def _listed_sources(self, *, excluding: Path | None = None) -> set[str]:
        """Source stems any note in Notes/ or Inbox/ still lists."""
        listed: set[str] = set()
        for folder in (NOTES_FOLDER, INBOX_FOLDER):
            for path, metadata, _ in self._notes_in(folder):
                if excluding is not None and path == excluding:
                    continue
                listed.update(_link_stem(link) for link in metadata.get("sources") or [])
        return listed

    # -- tag registry ----------------------------------------------------------

    def registered_tags(self) -> set[str]:
        return set(self.tag_catalog())

    def tag_catalog(self) -> dict[str, str]:
        """Return registered tags and their meanings without changing the vault."""
        path = self.vault / TAG_REGISTRY_PATH
        if not path.exists():
            return {}
        _, body = read_note(path)
        return {
            normalize_tag(match.group(1)): (match.group(2) or "").strip()
            for match in re.finditer(r"(?m)^\s*-\s*`([^`]+)`(?:\s*—\s*(.*))?$", body)
        }

    def register_tags(self, tags: Iterable[str], *, meanings: dict[str, str] | None = None) -> list[str]:
        """Add tags to the registry. Returns the tags that were newly added."""
        self._require_matching_schema()
        meanings = meanings or {}
        path = self.vault / TAG_REGISTRY_PATH
        with self._lock():
            existing = self.registered_tags()
            incoming = [normalize_tag(tag) for tag in tags if normalize_tag(tag)]
            added = [tag for tag in dict.fromkeys(incoming) if tag not in existing]
            if not added:
                return []
            if path.exists():
                text = path.read_text(encoding="utf-8")
            else:
                text = dump_note(
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
                    "# Tag Registry\n\nThe controlled vocabulary. A note cannot be approved with a "
                    "tag that is not listed here, so adding a tag is a deliberate act.\n\n"
                    f"{TAG_REGISTRY_HEADING}\n",
                )
            lines = "\n".join(
                f"- `{tag}` — {meanings.get(tag, 'No description yet.')}" for tag in added
            )
            if TAG_REGISTRY_HEADING in text:
                head, _, tail = text.partition(TAG_REGISTRY_HEADING)
                body = tail.lstrip("\n").rstrip()
                text = f"{head}{TAG_REGISTRY_HEADING}\n\n{(body + chr(10)) if body else ''}{lines}\n"
            else:
                text = f"{text.rstrip()}\n\n{TAG_REGISTRY_HEADING}\n\n{lines}\n"
            atomic_write(path, text)
            self._commit(f"Register tags: {', '.join(added)}", [path])
            return added

    # -- inbox reads -----------------------------------------------------------

    def _item_from_text(self, path: Path, text: str, registered: set[str]) -> InboxItem:
        metadata, body = parse_note(text)
        summary, details, _ = parse_sections(body)
        item = InboxItem(
            note_id=str(metadata.get("id") or ""),
            note_path=self._rel(path),
            title=str(metadata.get("title") or path.stem),
            note_type=str(metadata.get("type") or ""),
            domain=str(metadata.get("domain") or ""),
            summary=summary,
            body=details,
            tags=[str(tag) for tag in metadata.get("tags") or []],
            aliases=[str(alias) for alias in metadata.get("aliases") or []],
            source_url=str(metadata.get("source_url") or ""),
            created=str(metadata.get("created") or ""),
            content_sha=_sha(text),
            text=text,
            revises=str(metadata.get("revises") or ""),
        )
        item.problems = check_for_approval(item, registered_tags=registered, vault=self.vault)
        return item

    def _inbox_paths(self) -> list[Path]:
        root = self.vault / INBOX_FOLDER
        return sorted(root.glob("*.md")) if root.is_dir() else []

    def list_inbox(self) -> list[InboxItem]:
        registered = self.registered_tags()
        items: list[InboxItem] = []
        for path in self._inbox_paths():
            try:
                items.append(self._item_from_text(path, path.read_text(encoding="utf-8"), registered))
            except (OSError, UnicodeError, yaml.YAMLError):
                continue
        return sorted(items, key=lambda item: (item.created, item.note_path))

    def _inbox_path(self, note_id: str) -> Path:
        for path in self._inbox_paths():
            try:
                metadata, _ = read_note(path)
            except (OSError, UnicodeError, yaml.YAMLError):
                continue
            if metadata.get("id") == note_id:
                return path
        raise KeyError(f"Unknown inbox note: {note_id}")

    def get_inbox(self, note_id: str) -> InboxItem:
        path = self._inbox_path(note_id)
        return self._item_from_text(path, path.read_text(encoding="utf-8"), self.registered_tags())

    # -- capture ---------------------------------------------------------------

    def _inbox_meta(self, **fields: Any) -> dict[str, Any]:
        """Frontmatter in the documented key order; optional keys are omitted when empty."""
        meta: dict[str, Any] = {
            "id": fields["note_id"],
            "status": STATUS_INBOX,
            "type": fields["note_type"],
            "domain": fields["domain"],
            "title": fields["title"],
            "created": fields["created"],
            "updated": fields["updated"],
            "tags": fields["tags"],
        }
        if fields["aliases"]:
            meta["aliases"] = fields["aliases"]
        meta["sources"] = fields["sources"]
        for key in ("source_url", "source_kind", "source_creator", "revises"):
            if fields.get(key):
                meta[key] = fields[key]
        return meta

    def _soft_problems(self, meta: dict[str, Any], summary: str) -> list[str]:
        return check_for_approval(
            {**meta, "summary": summary}, registered_tags=self.registered_tags(), vault=self.vault
        )

    def capture(
        self,
        *,
        content: str,
        title: str,
        summary: str,
        body: str = "",
        note_type: str = "concept",
        domain: str = "tech",
        tags: Iterable[str] = (),
        aliases: Iterable[str] = (),
        connections: Iterable[Connection | dict] = (),
        source_url: str = "",
        source_kind: str = "",
        source_creator: str = "",
        revises: str = "",
    ) -> CaptureResult:
        """Write an unreviewed note and its source at once. Quality is checked at approval.

        Only input that cannot become any note is refused here; everything else comes back
        as soft `problems`, so a capture is never lost to a missing tag.
        """
        self._require_matching_schema()
        note_type = validate_note_type(note_type)
        domain = validate_domain(domain)
        if not content.strip():
            raise ValidationError("Cannot capture empty content")
        if not str(title).strip():
            raise ValidationError("Cannot capture a note without a title")
        title = safe_filename(title)
        source_url, revises = source_url.strip(), revises.strip()
        tag_list = sorted({normalize_tag(tag) for tag in tags if normalize_tag(tag)})
        alias_list = [str(alias).strip() for alias in aliases if str(alias).strip()]
        parsed = [Connection.from_value(item) for item in connections]
        now = utc_now()

        with self._lock():
            existing = self._find_revision_item(source_url, revises)
            if existing is not None:
                path, old_meta = existing
                note_id, created = str(old_meta["id"]), str(old_meta.get("created") or now)
                source_stem = _link_stem((old_meta.get("sources") or [""])[-1])
                note_title = str(old_meta.get("title") or title)
                updated_existing = True
            else:
                path = self._free_path(INBOX_FOLDER, title)
                note_id, created = f"note-{uuid.uuid4().hex[:12]}", now
                source_stem = f"{uuid.uuid4().hex[:12]}-{title}"
                note_title = title
                updated_existing = False
            source_path = self.vault / SOURCES_FOLDER / f"{source_stem}.md"
            meta = self._inbox_meta(
                note_id=note_id,
                note_type=note_type,
                domain=domain,
                title=note_title,
                created=created,
                updated=now,
                tags=tag_list,
                aliases=alias_list,
                sources=[f"[[{source_stem}]]"],
                source_url=source_url,
                source_kind=source_kind.strip().lower(),
                source_creator=source_creator.strip(),
                revises=revises,
            )
            text = render_note(meta, summary=summary.strip(), body=body, connections=parsed)
            source_text = render_source(
                source_id=f"source-{source_stem.split('-', 1)[0]}",
                note_id=note_id,
                title=note_title,
                content=content.rstrip(),
                captured=now,
                source_kind=meta.get("source_kind", ""),
                source_creator=meta.get("source_creator", ""),
                source_url=source_url,
            )
            atomic_write(source_path, source_text)
            atomic_write(path, text)
            warnings = self._commit(f"Capture {note_title}", [source_path, path])
            return CaptureResult(
                note_id=note_id,
                note_path=self._rel(path),
                source_path=self._rel(source_path),
                content_sha=_sha(text),
                problems=self._soft_problems(meta, summary.strip()),
                updated_existing=updated_existing,
                revises=revises,
                warnings=warnings,
            )

    def _find_revision_item(self, source_url: str, revises: str) -> tuple[Path, dict[str, Any]] | None:
        """An inbox note already proposing this revision of this URL, to update in place."""
        if not source_url or not revises:
            return None
        for path, metadata, _ in self._notes_in(INBOX_FOLDER):
            if metadata.get("source_url") == source_url and metadata.get("revises") == revises:
                return path, metadata
        return None

    def update_inbox(
        self,
        note_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        body: str | None = None,
        note_type: str | None = None,
        domain: str | None = None,
        tags: Iterable[str] | None = None,
        aliases: Iterable[str] | None = None,
        connections: Iterable[Connection | dict] | None = None,
    ) -> CaptureResult:
        self._require_matching_schema()
        with self._lock():
            path = self._inbox_path(note_id)
            meta, old_body = read_note(path)
            old_summary, old_details, old_connections = parse_sections(old_body)
            if note_type is not None:
                meta["type"] = validate_note_type(note_type)
            if domain is not None:
                meta["domain"] = validate_domain(domain)
            if title is not None:
                if not title.strip():
                    raise ValidationError("title cannot be empty")
                meta["title"] = safe_filename(title)
            if tags is not None:
                meta["tags"] = sorted({normalize_tag(tag) for tag in tags if normalize_tag(tag)})
            if aliases is not None:
                cleaned = [str(alias).strip() for alias in aliases if str(alias).strip()]
                if cleaned:
                    meta["aliases"] = cleaned
                else:
                    meta.pop("aliases", None)
            new_summary = old_summary if summary is None else summary.strip()
            new_body = old_details if body is None else body
            new_connections = (
                old_connections
                if connections is None
                else [Connection.from_value(item) for item in connections]
            )
            meta["updated"] = utc_now()
            # An edit answers the reasons the last promotion attempt failed.
            meta.pop("review_problems", None)
            text = render_note(meta, summary=new_summary, body=new_body, connections=new_connections)
            target = path
            if meta["title"] != path.stem:
                target = self._free_path(INBOX_FOLDER, meta["title"])
            atomic_write(target, text)
            touched = [target]
            if target != path:
                path.unlink()
                touched.append(path)
            sources = meta.get("sources") or []
            source_path = self.vault / SOURCES_FOLDER / f"{_link_stem(sources[-1])}.md" if sources else target
            warnings = self._commit(f"Update inbox note {meta['title']}", touched)
            return CaptureResult(
                note_id=str(meta["id"]),
                note_path=self._rel(target),
                source_path=self._rel(source_path),
                content_sha=_sha(text),
                problems=self._soft_problems(meta, new_summary),
                updated_existing=True,
                revises=str(meta.get("revises") or ""),
                warnings=warnings,
            )

    # -- approval --------------------------------------------------------------

    def approve(self, note_id: str, *, content_sha: str) -> ApproveResult:
        self._require_matching_schema()
        with self._lock():
            path = self._inbox_path(note_id)
            text = path.read_text(encoding="utf-8")
            if _sha(text) != content_sha:
                raise ApprovalMismatch(
                    f"{note_id} changed after it was reviewed. Read it again and approve "
                    "the version you saw."
                )
            item = self._item_from_text(path, text, self.registered_tags())
            if item.problems:
                raise ValidationError(
                    f"Note {note_id} cannot be approved:\n"
                    + "\n".join(f"  - {problem}" for problem in item.problems)
                )
            return self._promote(path, text)

    def _promote(self, path: Path, text: str) -> ApproveResult:
        """Move a validated inbox note into Notes/. The caller holds the lock."""
        meta, body = parse_note(text)
        summary, details, connections = parse_sections(body)
        now = utc_now()
        inbox_sources = [str(link) for link in meta.get("sources") or []]
        tags = [str(tag) for tag in meta.get("tags") or []]
        aliases = [str(alias) for alias in meta.get("aliases") or []]
        old_tags: set[str] = set()
        revises = str(meta.get("revises") or "")

        if revises:
            try:
                dest, old, _ = self._find_by_id(NOTES_FOLDER, revises)
            except KeyError as error:
                raise ValidationError(f"revises {revises!r}, but no approved note has that id") from error
            old_tags = {str(tag) for tag in old.get("tags") or []}
            note_id, title, created = str(old["id"]), str(old.get("title") or dest.stem), old.get("created")
            sources = _merge((str(link) for link in old.get("sources") or []), inbox_sources)
            aliases = _merge((str(alias) for alias in old.get("aliases") or []), aliases)
            carried = {key: meta.get(key) or old.get(key) for key in PROVENANCE_KEYS}
        else:
            dest = self._free_path(NOTES_FOLDER, safe_filename(str(meta.get("title") or path.stem)))
            # The title follows the final file name so `[[title]]` links resolve.
            note_id, title, created = str(meta["id"]), dest.stem, meta.get("created") or now
            sources = inbox_sources
            carried = {key: meta.get(key) for key in PROVENANCE_KEYS}

        final = {
            "id": note_id,
            "status": STATUS_APPROVED,
            "type": meta["type"],
            "domain": meta["domain"],
            "title": title,
            "created": created,
            "updated": now,
            "reviewed": now,
            "tags": tags,
        }
        if aliases:
            final["aliases"] = aliases
        final["sources"] = sources
        final.update({key: value for key, value in carried.items() if value})

        touched = [dest, path]
        for link in inbox_sources:
            source_path = self.vault / SOURCES_FOLDER / f"{_link_stem(link)}.md"
            if not source_path.is_file():
                continue
            source_meta, source_body = read_note(source_path)
            source_meta["note_id"] = note_id
            source_meta["distilled_note"] = f"[[{dest.stem}]]"
            atomic_write(source_path, dump_note(source_meta, source_body))
            touched.append(source_path)

        atomic_write(dest, render_note(final, summary=summary, body=details, connections=connections))
        path.unlink()
        changed_maps = maps.regenerate(self.vault, only_tags=set(tags) | old_tags)
        warnings = self._commit(f"Approve {title}", [*touched, *changed_maps])
        return ApproveResult(
            note_id=note_id,
            note_path=self._rel(dest),
            revised=bool(revises),
            maps_updated=[map_path.stem for map_path in changed_maps],
            warnings=warnings,
        )

    def promote_reviewed(self) -> list[ApproveResult | dict]:
        """Promote inbox notes a human marked `status: approved`, e.g. in Obsidian.

        The edit is the approval, so no hash is needed. A note that fails the gate goes back
        to `status: inbox` with the reasons, so the reviewer sees why in the same table.
        """
        if self._schema_mismatch:
            return []
        results: list[ApproveResult | dict] = []
        with self._lock():
            registered = self.registered_tags()
            reverted: list[Path] = []
            for path in self._inbox_paths():
                try:
                    text = path.read_text(encoding="utf-8")
                    meta, body = parse_note(text)
                except (OSError, UnicodeError, yaml.YAMLError):
                    continue
                if meta.get("status") != STATUS_APPROVED:
                    continue
                summary, details, connections = parse_sections(body)
                problems = check_for_approval(
                    {**meta, "summary": summary}, registered_tags=registered, vault=self.vault
                )
                if not meta.get("id"):
                    problems.append("id is missing from the frontmatter")
                if problems:
                    meta["status"] = STATUS_INBOX
                    meta["review_problems"] = problems
                    meta.setdefault("title", path.stem)
                    atomic_write(
                        path, render_note(meta, summary=summary, body=details, connections=connections)
                    )
                    reverted.append(path)
                    results.append(
                        {
                            "note_id": str(meta.get("id") or ""),
                            "note_path": self._rel(path),
                            "problems": problems,
                        }
                    )
                else:
                    results.append(self._promote(path, text))
            if reverted:
                self._commit("Return unapproved inbox notes to review", reverted)
        return results

    def discard(self, note_id: str, *, reason: str = "") -> ArchiveResult:
        """Archive an inbox note, plus its sources when no other note lists them."""
        self._require_matching_schema()
        with self._lock():
            path = self._inbox_path(note_id)
            meta, _ = read_note(path)
            still_listed = self._listed_sources(excluding=path)
            pairs: list[tuple[Path, Path]] = [(path, self._free_archive(ARCHIVE_INBOX, path.name))]
            for link in meta.get("sources") or []:
                stem = _link_stem(link)
                source = self.vault / SOURCES_FOLDER / f"{stem}.md"
                if stem not in still_listed and source.is_file():
                    pairs.append((source, self._free_archive(ARCHIVE_SOURCES, source.name)))
            receipt = self.vault / ARCHIVE_RECEIPTS / f"discard-{note_id}-{uuid.uuid4().hex[:6]}.json"
            for src, dst in pairs:
                atomic_move(src, dst)
            atomic_write(
                receipt,
                json.dumps(
                    {
                        "action": "discard",
                        "note_id": note_id,
                        "reason": reason.strip(),
                        "at": utc_now(),
                        "moved": [{"from": self._rel(src), "to": self._rel(dst)} for src, dst in pairs],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            touched = [receipt, *(item for pair in pairs for item in pair)]
            warnings = self._commit(f"Discard {meta.get('title', note_id)}", touched)
            return ArchiveResult(
                note_id=note_id,
                archived=[self._rel(dst) for _, dst in pairs],
                receipt_path=self._rel(receipt),
                warnings=warnings,
            )

    def _free_archive(self, folder: str, name: str) -> Path:
        stem, suffix = Path(name).stem, Path(name).suffix
        candidate = self.vault / folder / name
        while candidate.exists():
            candidate = self.vault / folder / f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"
        return candidate

    # -- validation ------------------------------------------------------------

    def validate(self) -> dict:
        result = lint_vault(self.vault, registered_tags=self.registered_tags())
        result["unmigrated_folders"] = self.unmigrated_folders()
        result["schema_version"] = SCHEMA_VERSION
        result["schema_mismatch"] = self._schema_mismatch
        return result
