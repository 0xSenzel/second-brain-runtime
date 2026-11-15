"""The structural rules, enforced rather than requested.

Prose in AGENTS.md is advice an agent may drop under context pressure. Everything in
here is checked before a note leaves the inbox, so a malformed note cannot become
approved knowledge. `check_for_approval` guards the write path; `lint_vault` applies the
same rules to notes edited by hand.

Capture is cheap by design: these rules are soft at capture time (reported as problems)
and hard only at approval.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .models import (
    DOMAINS,
    INBOX_FOLDER,
    MAX_SUMMARY,
    MAX_TAGS,
    MIN_TAGS,
    NOTE_TYPES,
    NOTES_FOLDER,
    SOURCES_FOLDER,
    STATUS_APPROVED,
    TAG_RE,
)
from .notes import parse_note, parse_sections


def _fields(item: Any) -> dict[str, Any]:
    """Normalise an InboxItem or a frontmatter mapping (plus `summary`) to one shape."""
    if isinstance(item, Mapping):
        return {
            "summary": str(item.get("summary") or ""),
            "tags": [str(tag) for tag in item.get("tags") or []],
            "note_type": str(item.get("note_type") or item.get("type") or ""),
            "domain": str(item.get("domain") or ""),
            "revises": str(item.get("revises") or ""),
        }
    return {
        "summary": item.summary,
        "tags": list(item.tags),
        "note_type": item.note_type,
        "domain": item.domain,
        "revises": item.revises,
    }


def _note_exists(vault: Path, note_id: str) -> bool:
    root = vault / NOTES_FOLDER
    if not root.is_dir():
        return False
    for path in root.rglob("*.md"):
        try:
            metadata, _ = parse_note(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if metadata.get("id") == note_id:
            return True
    return False


def check_for_approval(item: Any, *, registered_tags: set[str], vault: Path) -> list[str]:
    """Problems that block approval. An empty list means the note may be approved.

    Every rule answers one question: will this note still be findable in six months?
    Tags are the retrieval surface and the summary is what the index ranks on.
    """
    fields = _fields(item)
    problems: list[str] = []
    summary = fields["summary"]

    if fields["note_type"] not in NOTE_TYPES:
        problems.append(f"type {fields['note_type']!r} is not one of {sorted(NOTE_TYPES)}")
    if fields["domain"] not in DOMAINS:
        problems.append(f"domain {fields['domain']!r} is not one of {sorted(DOMAINS)}")

    if not summary.strip():
        problems.append(
            "summary cannot be empty: it is the text the search index ranks on, "
            "so a note without one is close to unfindable"
        )
    elif re.search(r"(?m)^#{1,6}\s+\S", summary):
        # A summary with headings is a whole document passed to the wrong field. It is
        # rendered under the note's own '## Summary' heading, so it would nest sections
        # inside it and leak Markdown into the map entry.
        problems.append(
            "summary contains Markdown headings, so it is a document rather than a "
            "summary. Keep the summary to plain prose and put the rest in the body"
        )
    elif len(summary) > MAX_SUMMARY:
        problems.append(
            f"summary is {len(summary)} characters, over the {MAX_SUMMARY} limit. "
            "A summary is what the index ranks on and what the map entry quotes; "
            "put the detail in the note body instead"
        )

    tags = fields["tags"]
    if len(tags) < MIN_TAGS:
        problems.append(f"needs at least {MIN_TAGS} tag for retrieval, got {len(tags)}")
    elif len(tags) > MAX_TAGS:
        problems.append(f"needs at most {MAX_TAGS} tags, got {len(tags)}")
    malformed = [tag for tag in tags if not TAG_RE.match(tag)]
    if malformed:
        problems.append(f"tags must be lowercase-kebab-case, got {malformed}")
    unknown = sorted({tag for tag in tags if TAG_RE.match(tag)} - registered_tags)
    if unknown:
        problems.append(
            f"tags {unknown} are not registered in Maps/Tag Registry.md. "
            "Register them first, with a line saying what each one means."
        )

    if fields["revises"] and not _note_exists(Path(vault), fields["revises"]):
        problems.append(f"revises {fields['revises']!r}, but no approved note has that id")

    return problems


def lint_note(metadata: dict[str, Any], *, summary: str, registered_tags: set[str], vault: Path) -> list[str]:
    """Structural problems in one already-written approved note. Empty means conforming."""
    note_type = str(metadata.get("type") or "")
    if note_type in {"source", "map", "review", "dashboard", "collection"}:
        return []
    problems = check_for_approval(
        {**metadata, "summary": summary}, registered_tags=registered_tags, vault=vault
    )
    if metadata.get("status") != STATUS_APPROVED:
        problems.append(
            f"status is {metadata.get('status')!r}, so it is not approved knowledge"
        )
    if "source_url" in metadata and metadata.get("source_url") is None:
        problems.append("source_url is null; omit the key entirely when there is no URL")
    return problems


def _lint_folder(
    vault: Path, folder: str, registered_tags: set[str]
) -> tuple[int, int, list[dict[str, Any]]]:
    """(checked, conforming, findings) for one folder."""
    findings: list[dict[str, Any]] = []
    checked = bad = 0
    root = vault / folder
    if not root.is_dir():
        return 0, 0, findings
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(vault).as_posix()
        try:
            metadata, body = parse_note(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            metadata, body = {}, ""
        if not metadata:
            findings.append(
                {"path": relative, "problems": ["no readable frontmatter, so it is invisible to the index"]}
            )
            continue
        checked += 1
        summary, _, _ = parse_sections(body)
        problems = lint_note(metadata, summary=summary, registered_tags=registered_tags, vault=vault)
        if folder == INBOX_FOLDER:
            # An inbox note is expected to be unapproved; only the approval rules matter.
            problems = [problem for problem in problems if not problem.startswith("status is")]
        if problems:
            bad += 1
            findings.append({"path": relative, "problems": problems})
    return checked, checked - bad, findings


def orphan_sources(vault: Path) -> list[str]:
    """Source files that no note in Notes/ or Inbox/ lists in `sources`."""
    referenced: set[str] = set()
    for folder in (NOTES_FOLDER, INBOX_FOLDER):
        root = vault / folder
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            try:
                metadata, _ = parse_note(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError):
                continue
            for link in metadata.get("sources") or []:
                referenced.add(str(link).removeprefix("[[").removesuffix("]]").split("|", 1)[0].strip())
    root = vault / SOURCES_FOLDER
    if not root.is_dir():
        return []
    return [
        path.relative_to(vault).as_posix()
        for path in sorted(root.rglob("*.md"))
        if path.stem not in referenced
    ]


def lint_vault(vault: Path, *, registered_tags: set[str]) -> dict:
    """Check approved notes against the rules the write path enforces.

    Inbox notes are reported apart: being incomplete is their normal state, so they must
    not make the approved notes look unhealthy.
    """
    vault = Path(vault)
    checked, conforming, findings = _lint_folder(vault, NOTES_FOLDER, registered_tags)
    inbox_checked, _, inbox_findings = _lint_folder(vault, INBOX_FOLDER, registered_tags)
    return {
        "checked_notes": checked,
        "conforming": conforming,
        "findings": findings,
        "inbox": {"checked": inbox_checked, "findings": inbox_findings},
        "orphan_sources": orphan_sources(vault),
    }
