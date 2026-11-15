from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from . import maps
from .models import (
    INBOX_FOLDER,
    NOTES_FOLDER,
    SOURCES_FOLDER,
    STATUS_APPROVED,
    ValidationError,
)
from .notes import parse_note, read_note
from .service import ARCHIVE_NOTES, ARCHIVE_RECEIPTS, ARCHIVE_SOURCES, utc_now
from .vaultio import GitJournal, VaultLock, atomic_move, atomic_write

DELETE_ID_RE = re.compile(r"\Adelete-[0-9a-f]{32}\Z")
PROPOSAL_FOLDER = Path(".second-brain") / "delete-proposals"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _link_target(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().removeprefix("[[").removesuffix("]]").split("|", 1)[0].strip()


class DeleteWorkflow:
    """Approval-gated deletion: the note and its exclusive sources move to Archive/.

    Nothing is ever removed. Staging records the exact bytes and hashes the reviewer saw,
    and commit refuses to act if any of them changed since.
    """

    def __init__(self, vault: Path):
        self.vault = vault.resolve()
        self.proposal_dir = self.vault / PROPOSAL_FOLDER

    def prepare(self, note_id: str, *, reason: str = "") -> dict[str, Any]:
        note_id = str(note_id or "").strip()
        if not note_id:
            raise ValidationError("A stable frontmatter note ID is required")
        if note_id.casefold().startswith("map-"):
            raise ValidationError("Map notes are not valid deletion targets")

        note_path, metadata = self._find_canonical_note(note_id)
        note_bytes = note_path.read_bytes()
        title = str(metadata.get("title") or note_path.stem)
        proposal_id = f"delete-{uuid.uuid4().hex}"
        sources = self._owned_sources(metadata, note_path)
        note_archive = self._free_archive(ARCHIVE_NOTES, note_path.name)
        for source in sources["owned"]:
            if not source["missing"]:
                source["archive_path"] = self._relative(
                    self._free_archive(ARCHIVE_SOURCES, Path(source["path"]).name)
                )
        proposal: dict[str, Any] = {
            "proposal_id": proposal_id,
            "status": "pending",
            "created_at": utc_now(),
            "note_id": note_id,
            "title": title,
            "tags": [str(tag) for tag in metadata.get("tags") or []],
            "note_path": self._relative(note_path),
            "note_hash": _sha256(note_bytes),
            "note_base64": base64.b64encode(note_bytes).decode("ascii"),
            "note_archive_path": self._relative(note_archive),
            "sources": sources["owned"],
            "shared_sources": sources["shared"],
            "maps": self._affected_maps(note_path.stem),
            "reason": str(reason or "").strip(),
        }
        self._write_pending(proposal)
        return self._response(proposal)

    def commit(self, proposal_id: str) -> dict[str, Any]:
        with VaultLock(self.vault):
            return self._commit_locked(proposal_id)

    def _commit_locked(self, proposal_id: str) -> dict[str, Any]:
        proposal = self._load_pending(proposal_id)
        note_path = self._safe_existing_file(proposal["note_path"])
        self._verify_hash(note_path, proposal["note_hash"], "note")
        metadata, _ = self._read_bytes(note_path, note_path.read_bytes())

        current = self._owned_sources(metadata, note_path)
        keep = ("path", "missing", "hash")
        if [{key: item[key] for key in keep} for item in current["owned"]] != [
            {key: item[key] for key in keep} for item in proposal["sources"]
        ]:
            raise ValidationError("Linked sources changed since staging; proposal remains pending")
        for item in proposal["maps"]:
            self._verify_hash(self._safe_existing_file(item["path"]), item["hash"], "map")
        if {item["path"] for item in self._affected_maps(Path(proposal["note_path"]).stem)} != {
            item["path"] for item in proposal["maps"]
        }:
            raise ValidationError("Affected maps changed since staging; proposal remains pending")

        pairs: list[tuple[Path, Path]] = [(note_path, self._safe_path(proposal["note_archive_path"]))]
        for item in proposal["sources"]:
            if not item["missing"]:
                pairs.append((self._safe_existing_file(item["path"]), self._safe_path(item["archive_path"])))
        if any(dst.exists() for _, dst in pairs):
            raise ValidationError("Archive destination already exists; proposal remains pending")

        receipt_path = self._safe_path(f"{ARCHIVE_RECEIPTS}/{proposal_id}.json")
        receipt = {
            "proposal_id": proposal_id,
            "status": "deleted",
            "approved_at": utc_now(),
            "reason": proposal["reason"],
            "note_id": proposal["note_id"],
            "title": proposal["title"],
            "note": {
                "original_path": proposal["note_path"],
                "archived_path": proposal["note_archive_path"],
                "sha256": proposal["note_hash"],
            },
            "sources": [
                {
                    "original_path": item["path"],
                    "archived_path": item.get("archive_path"),
                    "sha256": item["hash"],
                    "missing_at_staging": item["missing"],
                    "reference": item["reference"],
                }
                for item in proposal["sources"]
            ],
            "kept_sources": proposal["shared_sources"],
            "maps": [
                {"path": item["path"], "removed_entries": item["removed_entries"]}
                for item in proposal["maps"]
            ],
        }

        moved: list[tuple[Path, Path]] = []
        review_path = self._review_path(proposal_id)
        pending_review = review_path.read_bytes()
        try:
            for src, dst in pairs:
                atomic_move(src, dst)
                moved.append((src, dst))
            atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
            changed_maps = maps.regenerate(self.vault, only_tags=set(proposal["tags"]))
            proposal["status"] = "deleted"
            proposal["deleted_at"] = receipt["approved_at"]
            atomic_write(
                self._proposal_path(proposal_id), json.dumps(proposal, ensure_ascii=False, indent=2) + "\n"
            )
            review_path.unlink()
        except Exception:
            for src, dst in reversed(moved):
                if dst.exists():
                    atomic_move(dst, src)
            receipt_path.unlink(missing_ok=True)
            maps.regenerate(self.vault, only_tags=set(proposal["tags"]))
            atomic_write(review_path, pending_review.decode("utf-8"))
            proposal["status"] = "pending"
            proposal.pop("deleted_at", None)
            atomic_write(
                self._proposal_path(proposal_id), json.dumps(proposal, ensure_ascii=False, indent=2) + "\n"
            )
            raise

        warnings = GitJournal(self.vault).commit(
            f"Archive {proposal['title']}",
            [receipt_path, *(item for pair in pairs for item in pair), *changed_maps],
        )
        return {
            "proposal_id": proposal_id,
            "status": "deleted",
            "note_path": proposal["note_path"],
            "source_path": next((i["path"] for i in proposal["sources"] if not i["missing"]), None),
            "source_paths": [i["path"] for i in proposal["sources"] if not i["missing"]],
            "archive_path": proposal["note_archive_path"],
            "archived": [self._relative(dst) for _, dst in pairs],
            "receipt_path": self._relative(receipt_path),
            "affected_maps": [item["path"] for item in proposal["maps"]],
            "warnings": warnings,
        }

    # -- lookups ---------------------------------------------------------------

    def _find_canonical_note(self, note_id: str) -> tuple[Path, dict[str, Any]]:
        matches: list[tuple[Path, dict[str, Any]]] = []
        root = self._safe_path(NOTES_FOLDER)
        if root.is_symlink():
            raise ValidationError(f"Unsafe canonical note folder: {NOTES_FOLDER}")
        if root.is_dir():
            for path in sorted(root.rglob("*.md")):
                relative = self._relative(path)
                try:
                    self._safe_existing_file(relative)
                except ValidationError as error:
                    raise ValidationError(f"Unsafe canonical note path: {relative}") from error
                try:
                    metadata, _ = read_note(path)
                except (OSError, UnicodeError, yaml.YAMLError):
                    continue
                if isinstance(metadata.get("id"), str) and metadata["id"] == note_id:
                    matches.append((path, metadata))
        if len(matches) > 1:
            raise ValidationError(f"Ambiguous note ID {note_id!r}: multiple canonical notes match")
        if not matches:
            raise ValidationError(f"Unknown canonical note ID: {note_id}")
        if matches[0][1].get("type") == "map":
            raise ValidationError("Map notes are not valid deletion targets")
        if matches[0][1].get("status") != STATUS_APPROVED:
            raise ValidationError(f"Note {note_id!r} is not approved")
        return matches[0]

    def _other_references(self, note_path: Path) -> set[str]:
        """Source stems that some other note in Notes/ or Inbox/ still lists."""
        listed: set[str] = set()
        for folder in (NOTES_FOLDER, INBOX_FOLDER):
            root = self._safe_path(folder)
            if not root.is_dir():
                continue
            for path in root.rglob("*.md"):
                if path == note_path:
                    continue
                try:
                    metadata, _ = read_note(path)
                except (OSError, UnicodeError, yaml.YAMLError):
                    continue
                listed.update(_link_target(link) for link in metadata.get("sources") or [])
        return listed

    def _owned_sources(self, metadata: dict[str, Any], note_path: Path) -> dict[str, list[dict[str, Any]]]:
        """Sources only this note lists (archived with it) and shared ones (left in place)."""
        elsewhere = self._other_references(note_path)
        root = self._safe_path(SOURCES_FOLDER)
        if root.is_symlink():
            raise ValidationError("Unsafe Sources folder")
        owned: list[dict[str, Any]] = []
        shared: list[str] = []
        for link in metadata.get("sources") or []:
            stem = _link_target(link)
            if not stem:
                continue
            if stem in elsewhere:
                shared.append(stem)
                continue
            relative = f"{SOURCES_FOLDER}/{stem}.md"
            path = self._safe_path(relative)
            if not path.is_file():
                owned.append(
                    {"path": relative, "missing": True, "hash": None, "base64": None, "reference": stem}
                )
                continue
            self._safe_existing_file(relative)
            content = path.read_bytes()
            owned.append(
                {
                    "path": relative,
                    "missing": False,
                    "hash": _sha256(content),
                    "base64": base64.b64encode(content).decode("ascii"),
                    "reference": stem,
                }
            )
        return {"owned": owned, "shared": shared}

    def _affected_maps(self, link: str) -> list[dict[str, Any]]:
        """Maps whose generated block lists the note, with the lines that will disappear."""
        result: list[dict[str, Any]] = []
        for path in maps.maps_listing(self.vault, link):
            relative = self._relative(path)
            self._safe_existing_file(relative)
            content = path.read_bytes()
            text = content.decode("utf-8")
            block = text[text.find(maps.START_MARKER) : text.find(maps.END_MARKER)]
            result.append(
                {
                    "path": relative,
                    "hash": _sha256(content),
                    "removed_entries": [line for line in block.splitlines() if f"[[{link}]]" in line],
                }
            )
        return result

    def _free_archive(self, folder: str, name: str) -> Path:
        stem, suffix = Path(name).stem, Path(name).suffix
        candidate = self._safe_path(f"{folder}/{name}")
        while candidate.exists():
            candidate = self._safe_path(f"{folder}/{stem}-{uuid.uuid4().hex[:6]}{suffix}")
        return candidate

    # -- proposal files --------------------------------------------------------

    def _write_pending(self, proposal: dict[str, Any]) -> None:
        proposal_path = self._proposal_path(proposal["proposal_id"])
        review_path = self._review_path(proposal["proposal_id"])
        if proposal_path.exists() or review_path.exists():
            raise ValidationError("Delete proposal ID collision")
        atomic_write(proposal_path, json.dumps(proposal, ensure_ascii=False, indent=2) + "\n")
        try:
            atomic_write(review_path, self._render_review(proposal))
        except Exception:
            proposal_path.unlink(missing_ok=True)
            raise

    def _render_review(self, proposal: dict[str, Any]) -> str:
        lines = [
            f"# Delete review: {proposal['title']}",
            "",
            "> [!warning] Nothing is archived or changed until this exact proposal is approved.",
            "",
            f"- Proposal ID: `{proposal['proposal_id']}`",
            f"- Stable note ID: `{proposal['note_id']}`",
            f"- Reason: {proposal['reason'] or 'No reason supplied.'}",
            f"- Note to archive: `{proposal['note_path']}` → `{proposal['note_archive_path']}`",
            f"- Note SHA-256: `{proposal['note_hash']}`",
        ]
        for item in proposal["sources"]:
            if item["missing"]:
                lines.append(f"- Linked source `{item['path']}` is missing; only the note is archived")
            else:
                lines.append(f"- Source to archive: `{item['path']}` → `{item['archive_path']}`")
        for stem in proposal["shared_sources"]:
            lines.append(f"- Source `{stem}` stays in place: another note lists it")
        lines += [
            "",
            "## Exact files to archive",
            "",
            f"### `{proposal['note_path']}`",
            "",
            self._code_block(base64.b64decode(proposal["note_base64"]).decode("utf-8")),
            "",
        ]
        for item in proposal["sources"]:
            if not item["missing"]:
                lines += [
                    f"### `{item['path']}`",
                    "",
                    self._code_block(base64.b64decode(item["base64"]).decode("utf-8")),
                    "",
                ]
        lines += ["## Map changes", "", "Maps are regenerated from tags after the note leaves Notes/.", ""]
        for item in proposal["maps"]:
            lines += [
                f"### `{item['path']}` (SHA-256 `{item['hash']}`)",
                "",
                "Entries that will disappear:",
                "",
                self._code_block("\n".join(item["removed_entries"])),
                "",
            ]
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _code_block(content: str) -> str:
        longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}markdown\n{content}\n{fence}"

    def _response(self, proposal: dict[str, Any]) -> dict[str, Any]:
        present = [item for item in proposal["sources"] if not item["missing"]]
        return {
            "proposal_id": proposal["proposal_id"],
            "status": proposal["status"],
            "title": proposal["title"],
            "note_id": proposal["note_id"],
            "note_path": proposal["note_path"],
            "source_path": present[0]["path"] if present else None,
            "source_paths": [item["path"] for item in present],
            "source_missing": any(item["missing"] for item in proposal["sources"]),
            "kept_sources": proposal["shared_sources"],
            "affected_maps": [item["path"] for item in proposal["maps"]],
            "map_changes": [
                {"path": item["path"], "removed_entries": item["removed_entries"]}
                for item in proposal["maps"]
            ],
            "archive_paths": [proposal["note_archive_path"], *(item["archive_path"] for item in present)],
            "reason": proposal["reason"],
            "approval_required": True,
            "review_path": self._relative(self._review_path(proposal["proposal_id"])),
            "review": self._render_review(proposal),
        }

    def _load_pending(self, proposal_id: str) -> dict[str, Any]:
        path = self._proposal_path(proposal_id)
        if not path.is_file():
            raise KeyError(f"Unknown delete proposal: {proposal_id}")
        proposal = json.loads(path.read_text(encoding="utf-8"))
        if proposal.get("proposal_id") != proposal_id or proposal.get("status") != "pending":
            raise ValidationError("Delete proposal is not pending")
        if not self._review_path(proposal_id).is_file():
            raise ValidationError("Pending delete review is missing")
        return proposal

    def _proposal_path(self, proposal_id: str) -> Path:
        if not DELETE_ID_RE.fullmatch(str(proposal_id)):
            raise ValueError("Invalid delete proposal ID")
        return self.proposal_dir / f"{proposal_id}.json"

    def _review_path(self, proposal_id: str) -> Path:
        self._proposal_path(proposal_id)
        return self.proposal_dir / f"{proposal_id}.md"

    # -- path safety -----------------------------------------------------------

    def _safe_path(self, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or any(part in {".", ".."} for part in candidate.parts):
            raise ValidationError(f"Unsafe path: {relative}")
        path = self.vault / candidate
        if not path.resolve(strict=False).is_relative_to(self.vault):
            raise ValidationError(f"Unsafe path: {relative}")
        return path

    def _safe_existing_file(self, relative: str) -> Path:
        path = self._safe_path(relative)
        current = self.vault
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ValidationError(f"Unsafe path: {relative}")
        if not path.is_file():
            raise ValidationError(f"Required file is missing: {relative}")
        return path

    def _verify_hash(self, path: Path, expected: str, label: str) -> bytes:
        content = path.read_bytes()
        if _sha256(content) != expected:
            raise ValidationError(f"The {label} changed since staging; proposal remains pending")
        return content

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.vault).as_posix()

    @staticmethod
    def _read_bytes(path: Path, content: bytes) -> tuple[dict[str, Any], str]:
        try:
            return parse_note(content.decode("utf-8"))
        except (UnicodeError, yaml.YAMLError) as error:
            raise ValidationError(f"Invalid canonical note frontmatter at {path}") from error
