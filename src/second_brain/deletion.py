from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import yaml

from .models import DOMAIN_FOLDER, FOLDER_FOR_TYPE, MAPS_FOLDER, SOURCES_FOLDER, ValidationError
from .notes import read_note, safe_filename
from .service import utc_now

CANONICAL_NOTE_FOLDERS = tuple(
    f"{domain_folder}/{type_folder}"
    for domain_folder in DOMAIN_FOLDER.values()
    for type_folder in dict.fromkeys(FOLDER_FOR_TYPE.values())
)
DELETE_ID_RE = re.compile(r"\Adelete-[0-9a-f]{32}\Z")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _link_target(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().removeprefix("[[").removesuffix("]]").split("|", 1)[0].strip()


def _link_key(value: str) -> str:
    return value.replace("\\", "/").removesuffix(".md").strip().casefold()


class DeleteWorkflow:
    """Approval-gated deletion proposals with archived bytes and map recovery data."""

    def __init__(self, vault: Path):
        self.vault = vault.resolve()
        self.proposal_dir = self.vault / ".second-brain" / "delete-proposals"

    def prepare(self, note_id: str, *, reason: str = "") -> dict[str, Any]:
        note_id = str(note_id or "").strip()
        if not note_id:
            raise ValidationError("A stable frontmatter note ID is required")
        if note_id.casefold().startswith("map-"):
            raise ValidationError("Map notes are not valid deletion targets")

        note_path, metadata = self._find_canonical_note(note_id)
        note_bytes = note_path.read_bytes()
        note_metadata, _ = self._read_bytes(note_path, note_bytes)
        title = str(note_metadata.get("title") or note_path.stem)
        source = self._resolve_source(note_metadata)
        maps = self._prepare_maps(note_metadata, title, note_path)
        proposal_id = f"delete-{uuid.uuid4().hex}"
        archive_root = f"Reviews/Deleted/{proposal_id}"
        proposal: dict[str, Any] = {
            "proposal_id": proposal_id,
            "status": "pending",
            "created_at": utc_now(),
            "note_id": note_id,
            "title": title,
            "note_path": self._relative(note_path),
            "note_hash": _sha256(note_bytes),
            "note_base64": base64.b64encode(note_bytes).decode("ascii"),
            "source_path": source["path"],
            "source_missing": bool(source["missing"]),
            "source_reference": source["reference"],
            "source_hash": source["hash"],
            "source_base64": source["content_base64"],
            "maps": maps,
            "reason": str(reason or "").strip(),
            "archive_root": archive_root,
        }
        self._write_pending(proposal)
        return self._response(proposal)

    def commit(self, proposal_id: str) -> dict[str, Any]:
        proposal = self._load_pending(proposal_id)
        note_path = self._safe_existing_file(proposal["note_path"])
        note_bytes = self._verify_hash(note_path, proposal["note_hash"], "note")
        metadata, _ = self._read_bytes(note_path, note_bytes)

        current_source = self._resolve_source(metadata)
        expected_source = {
            "path": proposal["source_path"],
            "missing": proposal["source_missing"],
            "hash": proposal["source_hash"],
        }
        if any(current_source[key] != value for key, value in expected_source.items()):
            raise ValidationError("Linked source changed since staging; proposal remains pending")
        source_path: Path | None = None
        if proposal["source_path"]:
            if proposal["source_missing"]:
                if self._safe_path(proposal["source_path"]).exists():
                    raise ValidationError("Linked source changed since staging; proposal remains pending")
            else:
                source_path = self._safe_existing_file(proposal["source_path"])
                self._verify_hash(source_path, proposal["source_hash"], "source")

        current_maps = self._prepare_maps(metadata, proposal["title"], note_path)
        staged_map_paths = {item["path"] for item in proposal["maps"]}
        if {item["path"] for item in current_maps} != staged_map_paths:
            raise ValidationError("Affected maps changed since staging; proposal remains pending")
        map_bytes: dict[str, bytes] = {}
        for item in proposal["maps"]:
            path = self._safe_existing_file(item["path"])
            content = self._verify_hash(path, item["hash"], "map")
            map_bytes[item["path"]] = content

        archive_root = self._safe_path(proposal["archive_root"])
        if archive_root.exists():
            raise ValidationError("Archive destination already exists; proposal remains pending")
        note_archive = self._safe_path(f"{proposal['archive_root']}/{proposal['note_path']}")
        source_archive = (
            self._safe_path(f"{proposal['archive_root']}/{proposal['source_path']}")
            if source_path is not None
            else None
        )
        receipt_path = archive_root / "receipt.json"
        review_path = self._review_path(proposal_id)
        pending_review_bytes = review_path.read_bytes()
        proposal_path = self._proposal_path(proposal_id)
        pending_proposal_bytes = proposal_path.read_bytes()

        receipt = {
            "proposal_id": proposal["proposal_id"],
            "status": "deleted",
            "approved_at": utc_now(),
            "reason": proposal["reason"],
            "note_id": proposal["note_id"],
            "title": proposal["title"],
            "note": {
                "original_path": proposal["note_path"],
                "archived_path": self._relative(note_archive),
                "sha256": proposal["note_hash"],
            },
            "source": {
                "original_path": proposal["source_path"],
                "archived_path": self._relative(source_archive) if source_archive else None,
                "sha256": proposal["source_hash"],
                "missing_at_staging": proposal["source_missing"],
            },
            "maps": [
                {
                    "path": item["path"],
                    "before_sha256": item["hash"],
                    "after_sha256": _sha256(base64.b64decode(item["after_base64"])),
                    "removed_entries": item["removed_entries"],
                    "original_base64": item["before_base64"],
                }
                for item in proposal["maps"]
            ],
        }

        moved: list[tuple[Path, Path]] = []
        changed_maps: list[tuple[Path, bytes]] = []
        try:
            archive_root.mkdir(parents=True)
            note_archive.parent.mkdir(parents=True, exist_ok=True)
            note_path.replace(note_archive)
            moved.append((note_archive, note_path))
            if source_path is not None and source_archive is not None:
                source_archive.parent.mkdir(parents=True, exist_ok=True)
                source_path.replace(source_archive)
                moved.append((source_archive, source_path))
            for item in proposal["maps"]:
                path = self._safe_existing_file(item["path"])
                old_bytes = map_bytes[item["path"]]
                changed_maps.append((path, old_bytes))
                path.write_bytes(base64.b64decode(item["after_base64"]))
            receipt_path.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            review_path.unlink()
            proposal["status"] = "deleted"
            proposal["deleted_at"] = receipt["approved_at"]
            proposal_path.write_text(
                json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception:
            for path, content in reversed(changed_maps):
                path.write_bytes(content)
            for archived, original in reversed(moved):
                original.parent.mkdir(parents=True, exist_ok=True)
                if archived.exists():
                    archived.replace(original)
            if archive_root.exists():
                shutil.rmtree(archive_root, ignore_errors=True)
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_bytes(pending_review_bytes)
            proposal_path.write_bytes(pending_proposal_bytes)
            raise

        return {
            "proposal_id": proposal_id,
            "status": "deleted",
            "note_path": proposal["note_path"],
            "source_path": proposal["source_path"],
            "archive_path": proposal["archive_root"],
            "receipt_path": f"{proposal['archive_root']}/receipt.json",
            "affected_maps": [item["path"] for item in proposal["maps"]],
        }

    def _find_canonical_note(self, note_id: str) -> tuple[Path, dict[str, Any]]:
        matches: list[tuple[Path, dict[str, Any]]] = []
        for folder in CANONICAL_NOTE_FOLDERS:
            root = self._safe_path(folder)
            if root.is_symlink():
                raise ValidationError(f"Unsafe canonical note folder: {folder}")
            if not root.is_dir():
                continue
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
                if not isinstance(metadata.get("id"), str) or metadata["id"] != note_id:
                    continue
                matches.append((path, metadata))
        if len(matches) > 1:
            raise ValidationError(f"Ambiguous note ID {note_id!r}: multiple canonical notes match")
        if not matches:
            raise ValidationError(f"Unknown canonical note ID: {note_id}")
        if matches[0][1].get("type") == "map":
            raise ValidationError("Map notes are not valid deletion targets")
        if matches[0][1].get("status") != "approved":
            raise ValidationError(f"Note {note_id!r} is not approved")
        return matches[0]

    def _resolve_source(self, metadata: dict[str, Any]) -> dict[str, Any]:
        source_id = str(metadata.get("source_id") or "").strip()
        source_note = _link_target(metadata.get("source_note"))
        reference = {"source_id": source_id or None, "source_note": source_note or None}
        if not source_id and not source_note:
            return {
                "path": None,
                "missing": False,
                "hash": None,
                "content_base64": None,
                "reference": reference,
            }

        root = self._safe_path(SOURCES_FOLDER)
        matches: list[Path] = []
        if root.is_symlink():
            raise ValidationError("Unsafe Sources folder")
        if root.is_dir():
            for path in sorted(root.rglob("*.md")):
                relative = self._relative(path)
                try:
                    self._safe_existing_file(relative)
                except ValidationError as error:
                    raise ValidationError(f"Unsafe source path: {relative}") from error
                try:
                    source_metadata, _ = read_note(path)
                except (OSError, UnicodeError, yaml.YAMLError):
                    continue
                id_match = bool(source_id and str(source_metadata.get("id", "")) == source_id)
                path_match = bool(source_note and path.stem.casefold() == source_note.casefold())
                if id_match or path_match:
                    matches.append(path)
        if len(matches) > 1:
            raise ValidationError("Multiple source records match this note; refusing to guess")
        if not matches:
            return {
                "path": None,
                "missing": True,
                "hash": None,
                "content_base64": None,
                "reference": reference,
            }
        path = matches[0]
        content = path.read_bytes()
        return {
            "path": self._relative(path),
            "missing": False,
            "hash": _sha256(content),
            "content_base64": base64.b64encode(content).decode("ascii"),
            "reference": reference,
        }

    def _prepare_maps(self, metadata: dict[str, Any], title: str, note_path: Path) -> list[dict[str, Any]]:
        note_links = {
            _link_key(title),
            _link_key(note_path.stem),
            _link_key(self._relative(note_path)),
        }
        declared_names = metadata.get("maps") or []
        if not isinstance(declared_names, list):
            raise ValidationError("Canonical note has invalid map metadata")
        paths: set[str] = set()
        for name in declared_names:
            if not isinstance(name, str) or not name.strip():
                raise ValidationError("Canonical note has invalid map metadata")
            relative = f"{MAPS_FOLDER}/{safe_filename(name)}.md"
            path = self._safe_path(relative)
            if path.is_symlink() or not path.is_file():
                raise ValidationError(f"Affected map is missing or unsafe: {relative}")
            paths.add(relative)

        maps_root = self._safe_path(MAPS_FOLDER)
        if maps_root.is_symlink():
            raise ValidationError("Unsafe Maps folder")
        if maps_root.is_dir():
            for path in sorted(maps_root.rglob("*.md")):
                relative = self._relative(path)
                try:
                    self._safe_existing_file(relative)
                except ValidationError as error:
                    raise ValidationError(f"Unsafe map path: {relative}") from error
                text = path.read_text(encoding="utf-8")
                if self._entry_indexes_note(text, note_links):
                    paths.add(self._relative(path))

        result: list[dict[str, Any]] = []
        for relative in sorted(paths):
            path = self._safe_existing_file(relative)
            before = path.read_bytes()
            text = before.decode("utf-8")
            after_text, removed_entries = self._remove_note_entries(text, note_links)
            after = after_text.encode("utf-8")
            result.append(
                {
                    "path": relative,
                    "hash": _sha256(before),
                    "before_base64": base64.b64encode(before).decode("ascii"),
                    "after_base64": base64.b64encode(after).decode("ascii"),
                    "removed_entries": removed_entries,
                }
            )
        return result

    @staticmethod
    def _entry_indexes_note(text: str, note_links: set[str]) -> bool:
        for line in text.splitlines():
            if not re.match(r"^\s*[-*+]\s+", line):
                continue
            if any(_link_key(link) in note_links for link in WIKILINK_RE.findall(line)):
                return True
        return False

    @classmethod
    def _remove_note_entries(cls, text: str, note_links: set[str]) -> tuple[str, list[str]]:
        kept: list[str] = []
        removed: list[str] = []
        for line in text.splitlines(keepends=True):
            bare = line.rstrip("\r\n")
            if (
                re.match(r"^\s*[-*+]\s+", bare)
                and any(_link_key(link) in note_links for link in WIKILINK_RE.findall(bare))
            ):
                removed.append(bare)
            else:
                kept.append(line)
        return "".join(kept), removed

    def _write_pending(self, proposal: dict[str, Any]) -> None:
        proposal_path = self._proposal_path(proposal["proposal_id"])
        review_path = self._review_path(proposal["proposal_id"])
        if proposal_path.exists() or review_path.exists():
            raise ValidationError("Delete proposal ID collision")
        self.proposal_dir.mkdir(parents=True, exist_ok=True)
        review_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            review_path.write_text(self._render_review(proposal), encoding="utf-8")
        except Exception:
            proposal_path.unlink(missing_ok=True)
            raise

    def _render_review(self, proposal: dict[str, Any]) -> str:
        note_archive = f"{proposal['archive_root']}/{proposal['note_path']}"
        source = proposal["source_path"] or "No matching source file"
        source_archive = (
            f" → `{proposal['archive_root']}/{proposal['source_path']}`"
            if proposal["source_path"] and not proposal["source_missing"]
            else ""
        )
        if proposal["source_missing"]:
            reference = proposal["source_reference"]
            details = ", ".join(
                f"{key} `{value}`" for key, value in reference.items() if value
            )
            source += f" (missing; {details or 'linked source'}; only the note will be archived)"
        lines = [
            f"# Delete review: {proposal['title']}",
            "",
            "> [!warning] Nothing is archived or changed until this exact proposal is approved.",
            "",
            f"- Proposal ID: `{proposal['proposal_id']}`",
            f"- Stable note ID: `{proposal['note_id']}`",
            f"- Reason: {proposal['reason'] or 'No reason supplied.'}",
            f"- Note to archive: `{proposal['note_path']}` → `{note_archive}`",
            f"- Note SHA-256: `{proposal['note_hash']}`",
            f"- Linked source: `{source}`{source_archive}",
            "",
            "## Exact files to archive",
            "",
            f"### `{proposal['note_path']}`",
            "",
            self._code_block(base64.b64decode(proposal["note_base64"]).decode("utf-8")),
            "",
        ]
        if proposal["source_path"] and not proposal["source_missing"]:
            lines.extend(
                [
                    f"### `{proposal['source_path']}`",
                    "",
                    self._code_block(base64.b64decode(proposal["source_base64"]).decode("utf-8")),
                    "",
                ]
            )
        lines.extend([
            "## Exact map changes",
            "",
        ])
        for item in proposal["maps"]:
            after_text = base64.b64decode(item["after_base64"]).decode("utf-8")
            lines.extend(
                [
                    (
                        f"### `{item['path']}` (SHA-256 `{item['hash']}` → "
                        f"`{_sha256(base64.b64decode(item['after_base64']))}`)"
                    ),
                    "",
                    "Entries to remove:",
                    "",
                    self._code_block(
                        "\n".join(
                            item["removed_entries"]
                            or ["(none; map is tracked because the note declares it)"]
                        )
                    ),
                    "",
                    "Resulting map content:",
                    "",
                    self._code_block(after_text.rstrip("\n")),
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _code_block(content: str) -> str:
        longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}markdown\n{content}\n{fence}"

    def _response(self, proposal: dict[str, Any]) -> dict[str, Any]:
        return {
            "proposal_id": proposal["proposal_id"],
            "status": proposal["status"],
            "title": proposal["title"],
            "note_id": proposal["note_id"],
            "note_path": proposal["note_path"],
            "source_path": proposal["source_path"],
            "source_missing": proposal["source_missing"],
            "source_reference": proposal["source_reference"],
            "affected_maps": [item["path"] for item in proposal["maps"]],
            "map_changes": [
                {"path": item["path"], "removed_entries": item["removed_entries"]}
                for item in proposal["maps"]
            ],
            "archive_paths": [
                f"{proposal['archive_root']}/{proposal['note_path']}",
                *(
                    [f"{proposal['archive_root']}/{proposal['source_path']}"]
                    if proposal["source_path"] and not proposal["source_missing"]
                    else []
                ),
            ],
            "reason": proposal["reason"],
            "approval_required": True,
            "review_path": f"Reviews/Pending/{proposal['proposal_id']}.md",
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
        return self.vault / "Reviews" / "Pending" / f"{proposal_id}.md"

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
        from .notes import parse_note

        try:
            return parse_note(content.decode("utf-8"))
        except (UnicodeError, yaml.YAMLError) as error:
            raise ValidationError(f"Invalid canonical note frontmatter at {path}") from error
