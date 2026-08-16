from __future__ import annotations

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
    kind: str
    title: str
    summary: str
    content: str
    tags: list[str]
    connections: list[Connection]
    source_url: str
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
        data["connections"] = [Connection.from_value(item) for item in data.get("connections", [])]
        return cls(**data)


@dataclass(frozen=True)
class SavedItem:
    proposal_id: str
    note_path: str
    source_path: str
    status: str = "approved"
    warnings: list[str] = field(default_factory=list)
