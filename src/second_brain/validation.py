"""The structural rules, enforced rather than requested.

Prose in AGENTS.md is advice an agent may drop under context pressure. Everything in
here is checked before a note reaches the vault, so a malformed note cannot be written
in the first place. `check_proposal` guards the write path; `lint_vault` applies the
same rules to notes edited by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import (
    DOMAINS,
    MIN_ALIASES,
    MIN_MAPS,
    MIN_TAGS,
    NOTE_TYPES,
    TAG_RE,
    ValidationError,
    validate_domain,
    validate_note_type,
)
from .notes import read_note

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import Proposal


def check_proposal(proposal: Proposal, *, registered_tags: set[str]) -> None:
    """Raise ValidationError unless the proposal can become a well-formed note.

    Every rule here answers the same question: will this note still be findable in six
    months? Tags and maps are the retrieval surface, aliases are the alternate search
    phrasings, and the summary is what the index ranks on.
    """
    problems: list[str] = []

    try:
        validate_note_type(proposal.note_type)
    except ValidationError as error:
        problems.append(str(error))

    try:
        validate_domain(proposal.domain)
    except ValidationError as error:
        problems.append(str(error))

    if not proposal.title.strip():
        problems.append("title cannot be empty")

    if not proposal.summary.strip():
        problems.append(
            "summary cannot be empty: it is the text the search index ranks on, "
            "so a note without one is close to unfindable"
        )

    if len(proposal.tags) < MIN_TAGS:
        problems.append(
            f"needs at least {MIN_TAGS} tags for retrieval, got {len(proposal.tags)}"
        )

    malformed = [tag for tag in proposal.tags if not TAG_RE.match(tag)]
    if malformed:
        problems.append(f"tags must be lowercase-kebab-case, got {malformed}")

    if registered_tags:
        unknown = sorted(set(proposal.tags) - registered_tags)
        if unknown:
            problems.append(
                f"tags {unknown} are not registered in Maps/Tag Registry.md. "
                "Add them there in the same proposal, with a line saying what each one means."
            )

    if len(proposal.aliases) < MIN_ALIASES:
        problems.append(
            f"needs at least {MIN_ALIASES} aliases covering how this would be searched for "
            f"later, including the original language when the source is not English, "
            f"got {len(proposal.aliases)}"
        )

    if len(proposal.maps) < MIN_MAPS:
        problems.append(
            f"needs at least {MIN_MAPS} map. A note reachable from no map is unreachable "
            "by browsing. Name an existing map or propose a new one."
        )

    if problems:
        raise ValidationError(
            f"Proposal {proposal.id} does not satisfy the vault structure:\n"
            + "\n".join(f"  - {problem}" for problem in problems)
        )


def lint_note(metadata: dict[str, Any], *, registered_tags: set[str]) -> list[str]:
    """Structural problems in one already-written note. Empty list means conforming."""
    problems: list[str] = []
    note_type = str(metadata.get("type") or "")

    if note_type in {"source", "map", "review", "dashboard", "collection"}:
        return problems

    if note_type not in NOTE_TYPES:
        problems.append(f"type {note_type!r} is not one of {sorted(NOTE_TYPES)}")

    if str(metadata.get("domain") or "") not in DOMAINS:
        problems.append(
            f"domain {metadata.get('domain')!r} is not one of {sorted(DOMAINS)}"
        )

    tags = [str(tag) for tag in (metadata.get("tags") or [])]
    if len(tags) < MIN_TAGS:
        problems.append(f"has {len(tags)} tags, needs {MIN_TAGS}")
    malformed = [tag for tag in tags if not TAG_RE.match(tag)]
    if malformed:
        problems.append(f"tags not in lowercase-kebab-case: {malformed}")
    if registered_tags:
        unknown = sorted(set(tags) - registered_tags)
        if unknown:
            problems.append(f"unregistered tags: {unknown}")

    if len(metadata.get("aliases") or []) < MIN_ALIASES:
        problems.append(f"has {len(metadata.get('aliases') or [])} aliases, needs {MIN_ALIASES}")

    if len(metadata.get("maps") or []) < MIN_MAPS:
        problems.append("is not listed on any map, so it cannot be found by browsing")

    if metadata.get("status") != "approved":
        problems.append(
            f"status is {metadata.get('status')!r}, so it is excluded from every search answer"
        )

    if "source_url" in metadata and metadata.get("source_url") is None:
        problems.append("source_url is null; omit the key entirely when there is no URL")

    return problems


def lint_vault(vault: Path, *, registered_tags: set[str], folders: tuple[str, ...]) -> dict:
    """Check hand-edited notes against the same rules the write path enforces."""
    findings: list[dict[str, Any]] = []
    checked = 0
    for folder in folders:
        root = vault / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            metadata, _ = read_note(path)
            if not metadata:
                findings.append(
                    {
                        "path": path.relative_to(vault).as_posix(),
                        "problems": ["no frontmatter, so it is invisible to the index"],
                    }
                )
                continue
            checked += 1
            problems = lint_note(metadata, registered_tags=registered_tags)
            if problems:
                findings.append(
                    {"path": path.relative_to(vault).as_posix(), "problems": problems}
                )
    return {
        "checked_notes": checked,
        "conforming": checked - len(findings),
        "findings": findings,
    }
