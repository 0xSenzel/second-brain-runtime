"""Maps: generated lists of approved notes, one per set of tags.

A map is a human page with one machine-owned block. Regenerating rewrites only that block,
so the prose around it, which is the part worth writing by hand, is never touched.
Tags are the single source of truth, which is why a note cannot be filed on the wrong map.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import MAPS_FOLDER, NOTES_FOLDER, STATUS_APPROVED, VIEWS_FOLDER
from .notes import map_entry, parse_note, parse_sections
from .vaultio import atomic_write

START_MARKER = "<!-- brain:generated:start -->"
END_MARKER = "<!-- brain:generated:end -->"

REVIEW_BASE_PATH = f"{VIEWS_FOLDER}/Review.base"

_REVIEW_BASE: dict[str, Any] = {
    "filters": {"and": ['file.inFolder("Inbox")']},
    "views": [
        {
            "type": "table",
            "name": "Review",
            "order": [
                "file.name",
                "status",
                "type",
                "domain",
                "tags",
                "created",
                "review_problems",
            ],
        }
    ],
}


def write_review_base(vault: Path) -> bool:
    """Create Views/Review.base when missing. Returns True when it wrote the file.

    Never overwrites: the user may have reshaped the table in Obsidian.
    """
    path = Path(vault) / REVIEW_BASE_PATH
    if path.exists():
        return False
    atomic_write(path, yaml.safe_dump(_REVIEW_BASE, sort_keys=False, allow_unicode=True))
    return True


def _map_tags(metadata: dict[str, Any]) -> set[str]:
    raw = metadata.get("map_tags") or []
    return {str(tag) for tag in raw} if isinstance(raw, list) else set()


def _approved_notes(vault: Path) -> list[tuple[str, set[str], str]]:
    """(title, tags, first-sentence entry) for every approved note, sorted by title."""
    root = vault / NOTES_FOLDER
    found: list[tuple[str, set[str], str]] = []
    if not root.is_dir():
        return found
    for path in root.rglob("*.md"):
        try:
            metadata, body = parse_note(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if metadata.get("status") != STATUS_APPROVED or metadata.get("type") == "map":
            continue
        title = str(metadata.get("title") or path.stem)
        summary, _, _ = parse_sections(body)
        found.append((title, {str(tag) for tag in metadata.get("tags") or []}, map_entry(title, summary)))
    return sorted(found, key=lambda item: (item[0].casefold(), item[0]))


def _with_block(text: str, entries: list[str]) -> str:
    block = "\n".join([START_MARKER, *entries, END_MARKER])
    start = text.find(START_MARKER)
    end = text.find(END_MARKER, start + 1) if start != -1 else -1
    if start != -1 and end != -1:
        return text[:start] + block + text[end + len(END_MARKER) :]
    return f"{text.rstrip()}\n\n{block}\n"


def regenerate(vault: Path, *, only_tags: set[str] | None = None) -> list[Path]:
    """Rewrite the generated block of every map touched by `only_tags` (all when None).

    Returns the map files that actually changed, so the caller can journal exactly those.
    """
    vault = Path(vault)
    root = vault / MAPS_FOLDER
    if not root.is_dir():
        return []
    notes = _approved_notes(vault)
    changed: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            metadata, _ = parse_note(text)
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        tags = _map_tags(metadata)
        if not tags or (only_tags is not None and not tags & set(only_tags)):
            continue
        entries = [entry for title, note_tags, entry in notes if note_tags & tags]
        updated = _with_block(text, entries)
        if updated != text:
            atomic_write(path, updated)
            changed.append(path)
    return changed


def maps_listing(vault: Path, title: str) -> list[Path]:
    """Maps whose generated block currently links `title`. Used to preview a deletion."""
    root = Path(vault) / MAPS_FOLDER
    hits: list[Path] = []
    if not root.is_dir():
        return hits
    for path in sorted(root.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        start, end = text.find(START_MARKER), text.find(END_MARKER)
        if start != -1 and end != -1 and f"[[{title}]]" in text[start:end]:
            hits.append(path)
    return hits

