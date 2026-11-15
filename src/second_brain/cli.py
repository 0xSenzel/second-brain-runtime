from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .api import DEFAULT_VAULT, BrainAPI
from .models import DOMAINS, NOTE_TYPES


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="brain", description="Local Obsidian second brain")
    root.add_argument("--vault", help="Path to the Obsidian vault")
    commands = root.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture", help="Write an unreviewed note to Inbox/")
    capture.add_argument("input", help="Text, URL, or local file path")
    capture.add_argument("--type", dest="note_type", default="concept", choices=sorted(NOTE_TYPES))
    capture.add_argument("--domain", default="tech", choices=sorted(DOMAINS))
    capture.add_argument("--title", default="")
    capture.add_argument("--summary", default="")
    capture.add_argument("--body-file", type=Path, help="Markdown body to keep in the note")
    capture.add_argument("--tag", action="append", default=[])
    capture.add_argument("--alias", action="append", default=[])
    capture.add_argument("--creator", default="", help="Source creator, kept as provenance")
    capture.add_argument("--source-url", default="")
    capture.add_argument("--source-kind", default="")
    capture.add_argument("--revises", default="", help="Note ID this capture proposes to revise")
    capture.add_argument("--connections", default="[]", help="JSON array of typed connections")

    inbox = commands.add_parser("inbox", help="List inbox items, or show one in full")
    inbox.add_argument("note_id", nargs="?", default="")
    approve = commands.add_parser("approve", help="Approve one inbox note (a human, out of band)")
    approve.add_argument("note_id")
    approve.add_argument("--sha", default="", help="content_sha; defaults to the note's current one")
    discard = commands.add_parser("discard", help="Archive an inbox note")
    discard.add_argument("note_id")
    discard.add_argument("--reason", default="")
    commands.add_parser("promote", help="Promote notes approved by editing status in Obsidian")

    digest = commands.add_parser("digest", help="Short inbox digest")
    digest.add_argument("--json", action="store_true", help="Print JSON instead of text")
    report = commands.add_parser("report", help="Maintenance report")
    report.add_argument("--stale-days", type=int, default=180)

    find = commands.add_parser("find", help="Find matching notes")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=6)
    ask = commands.add_parser("ask", help="Retrieve a compact evidence bundle")
    ask.add_argument("query")
    ask.add_argument("--max-tokens", type=int, default=2500)
    ask.add_argument("--hops", type=int, default=1)

    commands.add_parser("reindex", help="Refresh the search index (lexical)")
    commands.add_parser("embed", help="Embed chunks that have no vector yet")
    commands.add_parser("validate", help="Check notes against the structural rules")
    commands.add_parser("doctor", help="Check the local runtime")
    tags = commands.add_parser("register-tags", help="Add tags to the controlled vocabulary")
    tags.add_argument("tag", nargs="+")

    migrate = commands.add_parser("migrate-v4", help="Migrate a schema-3 vault to schema 4")
    mode = migrate.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the plan only (default)")
    mode.add_argument("--apply", action="store_true", help="Apply the plan")
    migrate.add_argument("--tag-map", type=Path, help="JSON file {old_tag: new_tag | null}")
    migrate.add_argument("--map-tags", type=Path, help="JSON file {map name: [tags]}")
    return root


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    for name in ("as_dict", "to_dict"):
        method = getattr(value, name, None)
        if callable(method):
            return method()
    return value


def _migrate_v4(args: argparse.Namespace) -> Any:
    # Imported lazily: the module only matters for this command, and it must not
    # construct a BrainAPI (that would touch a schema-3 vault).
    migrate_v4 = importlib.import_module("second_brain.migrate_v4")

    vault = Path(args.vault or os.environ.get("SECOND_BRAIN_VAULT") or DEFAULT_VAULT).expanduser().resolve()
    plan = migrate_v4.plan(vault, tag_map=args.tag_map, map_tags=args.map_tags)
    if args.apply:
        return migrate_v4.apply(plan)
    return {"dry_run": True, "plan": _jsonable(plan)}


def run(args: argparse.Namespace) -> Any:
    if args.command == "migrate-v4":
        return _migrate_v4(args)
    api = BrainAPI(args.vault)
    command = args.command
    if command == "capture":
        return api.capture(
            args.input,
            title=args.title,
            summary=args.summary,
            body=args.body_file.read_text(encoding="utf-8") if args.body_file else "",
            note_type=args.note_type,
            domain=args.domain,
            tags=args.tag,
            aliases=args.alias,
            connections=json.loads(args.connections),
            source_url=args.source_url,
            source_kind=args.source_kind,
            source_creator=args.creator,
            revises=args.revises,
        )
    if command == "inbox":
        return api.inbox(args.note_id)
    if command == "approve":
        sha = args.sha
        if not sha:
            sha = api.inbox(args.note_id)["item"]["content_sha"]
        # The command line is the human channel, so it is exempt from approval=human.
        return api.approve(args.note_id, sha, approved=True, out_of_band=True)
    if command == "discard":
        return api.discard(args.note_id, reason=args.reason)
    if command == "promote":
        return api.promote()
    if command == "digest":
        data = api.digest()
        return data if args.json else data["text"]
    if command == "report":
        return api.report(stale_days=args.stale_days)
    if command == "find":
        return api.find(args.query, limit=args.limit)
    if command == "ask":
        return api.ask(args.query, max_tokens=args.max_tokens, hops=args.hops)
    if command == "reindex":
        return api.reindex()
    if command == "embed":
        return api.embed()
    if command == "validate":
        return api.validate()
    if command == "doctor":
        return api.doctor()
    if command == "register-tags":
        return api.register_tags(args.tag)
    raise AssertionError(f"Unhandled command: {command}")


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1) from error
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and "error" in result and len(result) == 1:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
