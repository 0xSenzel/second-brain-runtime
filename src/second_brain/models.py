from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

ALLOWED_RELATIONS = {
    "about",
    "created-by",
    "contradicts",
    "derived-from",
    "requires",
    "similar-to",
    "supports",
    "uses",
}

# A note's type decides its folder and nothing else. Topic belongs in tags, which are
# many-to-many; a closed set here is what stops a topic ever becoming a directory again.
NOTE_TYPES = {"concept", "prompt", "skill", "project", "essay"}

# The folder for each type. One entry per type, no exceptions and no nesting by subject.
FOLDER_FOR_TYPE = {
    "concept": "Notes",
    "prompt": "Prompts",
    "skill": "Skills",
    "project": "Projects",
    # Writing of your own, kept apart from material captured from someone else.
    "essay": "Essays",
}

# The second axis: which part of life the note belongs to. Closed like NOTE_TYPES and for
# the same reason — two orthogonal closed sets give a predictable grid, whereas one field
# meaning both things is what produced the folder sprawl this replaced. Adding a domain is
# a deliberate edit here, not something a save can do on its own.
DOMAINS = {"personal", "tech"}

DOMAIN_FOLDER = {
    "personal": "Personal",
    "tech": "Tech",
}


def folder_for(domain: str, note_type: str) -> str:
    return f"{DOMAIN_FOLDER[domain]}/{FOLDER_FOR_TYPE[note_type]}"

MAPS_FOLDER = "Maps"
SOURCES_FOLDER = "Sources"
TAG_REGISTRY_PATH = f"{MAPS_FOLDER}/Tag Registry.md"

# Folders earlier layouts created at the top level, before domain became the first path
# segment. Retired: removed at startup when empty, left alone when they still hold notes.
RETIRED_FOLDERS = ("Inbox", "Knowledge", "Resources", "Notes", "Prompts", "Skills", "Projects", "Essays")

MIN_TAGS = 2
MIN_ALIASES = 2
MIN_MAPS = 1

TAG_RE = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")

# Old `kind` values, which mixed note type and subject, mapped onto the type they meant.
# Kept so the proposals already on disk stay readable after the split.
LEGACY_KIND_TO_TYPE = {
    "prompt": "prompt",
    "skill": "skill",
    "project": "project",
    "note": "concept",
    "resource": "concept",
    "article": "concept",
    "youtube": "concept",
    "pdf": "concept",
    "ai-coding-resource": "concept",
    "frontend-resource": "concept",
    "reference-site": "concept",
}


class ValidationError(ValueError):
    """A structural rule was broken. Raised instead of writing a non-conforming note."""


def normalize_tag(value: str) -> str:
    return re.sub(r"\s+", "-", str(value).strip().lower())


def validate_note_type(value: str) -> str:
    note_type = str(value or "").strip().lower()
    if note_type not in NOTE_TYPES:
        raise ValidationError(
            f"Unsupported note_type {value!r}. "
            f"note_type must be one of {sorted(NOTE_TYPES)}. "
            "Subject matter belongs in tags, not in the type or the folder."
        )
    return note_type


def validate_domain(value: str) -> str:
    domain = str(value or "").strip().lower()
    if domain not in DOMAINS:
        raise ValidationError(
            f"Unsupported domain {value!r}. domain must be one of {sorted(DOMAINS)}. "
            "Adding a new one is an edit to models.py, not a choice made at save time."
        )
    return domain


def validate_destination(destination: str, note_type: str, domain: str) -> str:
    """A caller-chosen path is honoured only inside the folder its type owns."""
    candidate = str(destination or "").strip().replace("\\", "/").lstrip("/")
    if not candidate:
        raise ValidationError("destination cannot be empty")
    if not candidate.endswith(".md"):
        raise ValidationError(f"destination must end in .md, got {destination!r}")
    parts = [part for part in candidate.split("/") if part]
    if any(part in {"..", "."} for part in parts):
        raise ValidationError(f"destination must stay inside the vault, got {destination!r}")
    root = folder_for(domain, note_type)
    if "/".join(parts[:2]) != root:
        raise ValidationError(
            f"destination {destination!r} does not match domain {domain!r} and "
            f"note_type {note_type!r}, which live under {root}/."
        )
    return "/".join(parts)


@dataclass(frozen=True)
class Connection:
    relation: str
    target: str
    reason: str = ""

    @classmethod
    def from_value(cls, value: Connection | dict[str, Any]) -> Connection:
        if isinstance(value, cls):
            return value
        relation = str(value.get("relation", "")).strip().lower()
        target = str(value.get("target", "")).strip()
        reason = str(value.get("reason", "")).strip()
        if relation not in ALLOWED_RELATIONS:
            raise ValueError(f"Unsupported relation: {relation!r}")
        if not target:
            raise ValueError("Connection target cannot be empty")
        return cls(relation=relation, target=target, reason=reason)


@dataclass
class Proposal:
    id: str
    note_type: str
    domain: str
    title: str
    summary: str
    content: str
    tags: list[str]
    aliases: list[str]
    maps: list[str]
    connections: list[Connection]
    source_url: str
    source_kind: str
    source_creator: str
    source_path: str
    target_path: str
    created_at: str
    status: str = "pending"
    is_revision: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["connections"] = [asdict(connection) for connection in self.connections]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Proposal:
        data = dict(value)
        # Proposals written before type and source_kind were separated carry a single
        # `kind` that meant both. Read them as the type they intended, keeping the
        # original string as provenance.
        if "kind" in data:
            legacy_kind = str(data.pop("kind") or "note").strip().lower()
            data.setdefault("note_type", LEGACY_KIND_TO_TYPE.get(legacy_kind, "concept"))
            data.setdefault("source_kind", legacy_kind)
        data.setdefault("note_type", "concept")
        data.setdefault("domain", "tech")
        data.setdefault("source_kind", "")
        data.setdefault("source_creator", "")
        data.setdefault("aliases", [])
        data.setdefault("maps", [])
        data["connections"] = [Connection.from_value(item) for item in data.get("connections", [])]
        known = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: item for key, item in data.items() if key in known})


@dataclass(frozen=True)
class SavedItem:
    proposal_id: str
    note_path: str
    source_path: str
    status: str = "approved"
    warnings: list[str] = field(default_factory=list)
