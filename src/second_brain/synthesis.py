"""Read-only summaries of the vault: the inbox digest and the maintenance report.

Nothing here writes. The report reads approved notes straight from the vault folder
(`Notes/`, `Maps/`, `Sources/`) because the service contract has no "list approved
notes" method, and a report must not depend on index internals.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

DIGEST_LIMIT = 3500
INBOX_STALE_DAYS = 14
TAG_MAP_THRESHOLD = 5
VECTOR_DUPLICATE_COSINE = 0.92
TITLE_DUPLICATE_RATIO = 0.9
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)

SimilarPairs = Callable[[], Iterable[tuple[str, str, float]]]


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age_days(value: Any, now: datetime) -> int | None:
    parsed = _parse_time(value)
    return None if parsed is None else max(0, (now - parsed).days)


def digest(service: Any, index: Any = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Inbox count, oldest age, and one entry per item with its blocking problems."""
    now = now or datetime.now(UTC)
    items = []
    for item in service.list_inbox():
        items.append(
            {
                "note_id": item.note_id,
                "sha8": (item.content_sha or "")[:8],
                "title": item.title,
                "note_type": item.note_type,
                "age_days": _age_days(item.created, now),
                "problems": list(item.problems or []),
                "revises": item.revises or "",
                "note_path": item.note_path,
            }
        )
    ages = [entry["age_days"] for entry in items if entry["age_days"] is not None]
    return {
        "inbox_count": len(items),
        "oldest_age_days": max(ages) if ages else None,
        "items": items,
    }


def render_digest(data: dict[str, Any], *, limit: int = DIGEST_LIMIT) -> str:
    count = data["inbox_count"]
    if not count:
        return "Inbox is empty. Nothing to review."
    oldest = data.get("oldest_age_days")
    header = f"Inbox: {count} item{'s' if count != 1 else ''} to review"
    if oldest is not None:
        header += f"; oldest {oldest}d"
    lines = []
    for entry in data["items"]:
        age = entry.get("age_days")
        # The sha prefix lets a reviewer approve straight from the digest; an edit made
        # after the digest changes it, so the approval refuses instead of applying.
        sha8 = f" {entry['sha8']}" if entry.get("sha8") else ""
        line = f"- {entry['title']} [{entry['note_id']}{sha8}]"
        if age is not None:
            line += f" {age}d"
        if entry.get("revises"):
            line += f" (revises {entry['revises']})"
        if entry.get("problems"):
            line += " WARN: " + "; ".join(entry["problems"])
        lines.append(line)
    footer = "Approve in Obsidian (status: approved), with `brain approve`, or ask me to show an item."
    out = [header, *lines]
    text = "\n".join(out + [footer])
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = len(header) + len(footer) + 80  # room for the truncation line
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    hidden = len(lines) - len(kept)
    return "\n".join([header, *kept, f"... and {hidden} more (not shown)", footer])


# -- report ---------------------------------------------------------------------------


def _read_notes(folder: Path) -> list[dict[str, Any]]:
    notes = []
    if not folder.is_dir():
        return notes
    for path in sorted(folder.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = _FRONTMATTER_RE.match(text)
        meta: dict[str, Any] = {}
        body = text
        if match:
            try:
                loaded = yaml.safe_load(match.group(1))
                meta = loaded if isinstance(loaded, dict) else {}
            except yaml.YAMLError:
                meta = {}
            body = match.group(2)
        notes.append({"path": path, "stem": path.stem, "meta": meta, "body": body})
    return notes


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().casefold())


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)] if value else []


def report(
    service: Any,
    index: Any = None,
    *,
    stale_days: int = 180,
    similar_pairs: SimilarPairs | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Maintenance findings. Reports only; it changes nothing."""
    now = now or datetime.now(UTC)
    vault = Path(service.vault)
    notes = _read_notes(vault / "Notes")
    maps = _read_notes(vault / "Maps")
    maps = [item for item in maps if item["meta"].get("type") == "map"]
    by_id = {str(item["meta"].get("id", "")): item for item in notes if item["meta"].get("id")}

    # Likely duplicates.
    duplicates: list[dict[str, Any]] = []
    seen_pairs: set[frozenset[str]] = set()

    def add_duplicate(a: dict[str, Any], b: dict[str, Any], reason: str, score: float | None = None) -> None:
        key = frozenset((a["stem"], b["stem"]))
        if key in seen_pairs or a is b:
            return
        seen_pairs.add(key)
        entry: dict[str, Any] = {"notes": [a["stem"], b["stem"]], "reason": reason}
        if score is not None:
            entry["score"] = round(score, 3)
        duplicates.append(entry)

    by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in notes:
        url = str(item["meta"].get("source_url") or "").strip()
        if url:
            by_url[url].append(item)
    for url, group in by_url.items():
        for position, first in enumerate(group):
            for second in group[position + 1 :]:
                add_duplicate(first, second, f"same source_url {url}")
    titles = [(_norm(item["meta"].get("title") or item["stem"]), item) for item in notes]
    exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, item in titles:
        exact[key].append(item)
    for group in exact.values():
        for position, first in enumerate(group):
            for second in group[position + 1 :]:
                add_duplicate(first, second, "identical title")
    if len(titles) <= 2000:
        for position, (key_a, first) in enumerate(titles):
            for key_b, second in titles[position + 1 :]:
                if key_a == key_b or abs(len(key_a) - len(key_b)) > 6:
                    continue
                matcher = difflib.SequenceMatcher(None, key_a, key_b)
                if (
                    matcher.real_quick_ratio() >= TITLE_DUPLICATE_RATIO
                    and matcher.quick_ratio() >= TITLE_DUPLICATE_RATIO
                    and matcher.ratio() >= TITLE_DUPLICATE_RATIO
                ):
                    add_duplicate(first, second, "near-identical title", matcher.ratio())
    if similar_pairs is not None:
        try:
            for id_a, id_b, cosine in similar_pairs():
                if cosine >= VECTOR_DUPLICATE_COSINE and id_a in by_id and id_b in by_id:
                    add_duplicate(by_id[id_a], by_id[id_b], "vector similarity", cosine)
        except Exception:
            pass  # vectors are optional; the rest of the report stands

    # Links and maps. A note is an orphan when no other note or map links to it and none
    # of its tags belongs to a map (a map lists its tags' notes automatically).
    linkers: dict[str, set[str]] = defaultdict(set)
    for item in notes + maps:
        for target in _LINK_RE.findall(item["body"]):
            linkers[_norm(target)].add(str(item["path"]))
    map_tags = {_norm(tag) for item in maps for tag in _as_list(item["meta"].get("map_tags"))}
    orphans = []
    for item in notes:
        names = {_norm(item["stem"]), _norm(item["meta"].get("title") or "")}
        names |= {_norm(alias) for alias in _as_list(item["meta"].get("aliases"))}
        tags = {_norm(tag) for tag in _as_list(item["meta"].get("tags"))}
        inbound = any(linkers.get(name, set()) - {str(item["path"])} for name in names if name)
        if not inbound and not (tags & map_tags):
            orphans.append(item["stem"])

    tag_counts = Counter(_norm(tag) for item in notes for tag in _as_list(item["meta"].get("tags")))
    unmapped_tags = [
        {"tag": tag, "notes": count}
        for tag, count in sorted(tag_counts.items())
        if count >= TAG_MAP_THRESHOLD and tag not in map_tags
    ]

    stale_inbox = []
    for entry in digest(service, index, now=now)["items"]:
        if entry["age_days"] is not None and entry["age_days"] > INBOX_STALE_DAYS:
            stale_inbox.append(
                {"note_id": entry["note_id"], "title": entry["title"], "age_days": entry["age_days"]}
            )

    stale_notes = []
    for item in notes:
        age = _age_days(item["meta"].get("updated") or item["meta"].get("created"), now)
        if age is not None and age >= stale_days:
            stale_notes.append({"note": item["stem"], "age_days": age})

    referenced = set()
    for item in notes:
        for source in _as_list(item["meta"].get("sources")):
            referenced.add(_norm(source.strip("[]")))
    for item in service_inbox_sources(service):
        referenced.add(_norm(item))
    orphan_sources = [
        item["stem"]
        for item in _read_notes(vault / "Sources")
        if _norm(item["stem"]) not in referenced
    ]

    return {
        "likely_duplicates": duplicates,
        "notes_without_links_or_map": sorted(orphans),
        "tags_without_map": unmapped_tags,
        "stale_inbox": stale_inbox,
        "stale_notes": stale_notes,
        "orphan_sources": sorted(orphan_sources),
        "stale_days": stale_days,
    }


def service_inbox_sources(service: Any) -> list[str]:
    """Source stems named by inbox notes, so a fresh capture's source is not an orphan."""
    stems: list[str] = []
    vault = Path(service.vault)
    for item in _read_notes(vault / "Inbox"):
        stems.extend(source.strip("[]") for source in _as_list(item["meta"].get("sources")))
    return stems
