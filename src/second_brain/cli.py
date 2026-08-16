from __future__ import annotations

import argparse
import json
import sys

from .api import BrainAPI
from .migration import MigrationPlanner


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="brain", description="Local Obsidian second brain")
    root.add_argument("--vault", help="Path to the Obsidian vault")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Create the vault layout")
    prepare = commands.add_parser("prepare", help="Prepare a capture for review")
    prepare.add_argument("input", help="Text, URL, or local file path")
    prepare.add_argument("--kind")
    prepare.add_argument("--title")
    prepare.add_argument("--summary", default="")
    prepare.add_argument("--tag", action="append", default=[])
    prepare.add_argument("--connections", default="[]", help="JSON array of typed connections")

    approve = commands.add_parser("approve", help="Approve and save a pending proposal")
    approve.add_argument("proposal_id")
    reject = commands.add_parser("reject", help="Reject a pending proposal")
    reject.add_argument("proposal_id")
    commands.add_parser("pending", help="List pending proposals")

    ask = commands.add_parser("ask", help="Retrieve a compact evidence bundle")
    ask.add_argument("query")
    ask.add_argument("--max-tokens", type=int, default=2500)
    ask.add_argument("--hops", type=int, default=1)
    find = commands.add_parser("find", help="Find matching saved items")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=6)

    commands.add_parser("reindex", help="Refresh the local graph and search index")
    commands.add_parser("doctor", help="Check the local runtime")
    migrate = commands.add_parser("migrate", help="Prepare a copy-only pilot migration")
    migrate.add_argument("source_vault")
    migrate.add_argument("--limit", type=int, default=20)
    migrate.add_argument("--apply", action="store_true", help="Create pending review proposals")
    return root


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    api = BrainAPI(args.vault)
    try:
        if args.command == "init":
            result = api.doctor()
        elif args.command == "prepare":
            result = api.prepare_save(
                content_or_url=args.input,
                kind=args.kind,
                title=args.title,
                summary=args.summary,
                tags=args.tag,
                connections=json.loads(args.connections),
            )
        elif args.command == "approve":
            result = api.commit_save(args.proposal_id, approved=True)
        elif args.command == "reject":
            result = api.reject_save(args.proposal_id)
        elif args.command == "pending":
            result = api.pending()
        elif args.command == "ask":
            result = api.ask(args.query, max_tokens=args.max_tokens, hops=args.hops)
        elif args.command == "find":
            result = api.find(args.query, limit=args.limit)
        elif args.command == "reindex":
            result = api.reindex()
        elif args.command == "doctor":
            result = api.doctor()
        elif args.command == "migrate":
            planner = MigrationPlanner(api.service)
            candidates = planner.plan(args.source_vault, limit=args.limit)
            result = planner.apply(candidates) if args.apply else {
                "dry_run": True,
                "count": len(candidates),
                "candidates": [candidate.as_dict() for candidate in candidates],
            }
        else:
            raise AssertionError(f"Unhandled command: {args.command}")
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
