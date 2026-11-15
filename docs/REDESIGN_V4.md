# Schema 4 redesign: implementation contract

This file is the shared contract for the schema 4 work. Every work stream builds against
it. If a stream needs to change a contract, it reports the change instead of silently
diverging.

## Goals

1. Capture is cheap: a save request writes an unreviewed note to `Inbox/` at once.
2. Review is batched and can happen outside the model (Obsidian, CLI) or in chat.
3. One process writes the vault. Writes are locked, atomic, and journaled in git.
4. Retrieval always fuses lexical and vector ranking when vectors exist.
5. Folders express lifecycle, not taxonomy. Type and domain live in frontmatter.
6. Maps are generated from tags. Aliases are optional.
7. Revisions keep note identity, `created`, and every source.

## Vault layout (schema 4)

```
Inbox/            unreviewed notes, status: inbox
Notes/            approved notes, status: approved (flat)
Sources/          raw captured material, flat, <source-id>-<title>.md (unchanged)
Maps/             generated maps + Tag Registry.md
Archive/          discarded inbox notes, deleted notes, retired reviews, receipts
  Archive/Inbox/      discarded inbox notes
  Archive/Notes/      deleted approved notes
  Archive/Sources/    sources archived with them
  Archive/Reviews/    schema-3 Reviews/* moved here by migration
  Archive/Receipts/   one JSON receipt per archive action
Views/            Obsidian Bases files (Review.base)
Templates/
.brain-schema.json   {"schema_version": 4, "domains": [...], "note_types": [...]}
.second-brain/        gitignored runtime state (index, locks)
```

Constants live in `models.py` (already written for schema 4 by the parent; use them,
do not redefine them).

## Note format

Frontmatter of an inbox or approved note:

```yaml
id: note-<12 hex>            # stable forever, including across revisions
status: inbox | approved
type: concept | prompt | skill | project | essay
domain: personal | tech
title: <title>
created: <ISO UTC>           # first capture; never rewritten
updated: <ISO UTC>
reviewed: <ISO UTC>          # set on approval; absent in inbox
tags: [kebab-case, ...]
aliases: [...]               # optional
sources: ["[[<source stem>]]", ...]   # every source ever merged, oldest first
source_url: <latest canonical URL>    # optional; omit key when empty
source_kind: <kind>                   # optional
source_creator: <creator>             # optional
revises: note-<id>           # inbox only: this item proposes a revision of that note
review_problems: [...]       # inbox only: why the last promotion attempt failed
```

Body (rendered by `notes.render_note`):

```markdown
# <title>

## Summary

<summary prose>

## Details

<body markdown>          # section omitted when body is empty

## Connections

- <relation>: [[<target>]] — <reason>

## Source

- Original: [[<source stem>]]   # one line per source
- URL: <source_url>
- Creator: <source_creator>
```

`content_sha` is the SHA-256 hex digest of the full file text of an inbox note. It binds an
approval to the exact version the reviewer saw.

Source notes keep the schema-3 format, except `distilled_note` points at the note's
title and the note id is recorded as `note_id`.

## Write path (service layer)

All writes go through `BrainService`. Each public write method:

1. takes the vault write lock (`vaultio.VaultLock`),
2. computes every new file content first,
3. writes each file with `vaultio.atomic_write`,
4. regenerates affected maps (`maps.regenerate`),
5. records a git commit through `vaultio.GitJournal` (no-op when the vault is not a git
   repository or `SECOND_BRAIN_GIT_AUTOCOMMIT=0`).

Public service API (names are a contract; keyword-only args unless noted):

| Method | Result | Behaviour |
|---|---|---|
| `capture(*, content, title, summary, body="", note_type="concept", domain="tech", tags=(), aliases=(), connections=(), source_url="", source_kind="", source_creator="", revises="")` | `CaptureResult` | Writes a source note and an inbox note. If an inbox item with the same non-empty `source_url` and `revises` exists, it is updated in place (same id) and its source rewritten. Hard-fails only on invalid type/domain, empty content, or empty title. Returns soft `problems` from `validation.check_for_approval`. |
| `update_inbox(note_id, *, title=None, summary=None, body=None, note_type=None, domain=None, tags=None, aliases=None, connections=None)` | `CaptureResult` | Edits fields of one inbox note. |
| `list_inbox()` | `list[InboxItem]` | Every inbox note, oldest first. |
| `get_inbox(note_id)` | `InboxItem` | One inbox note with full text and `content_sha`. |
| `approve(note_id, *, content_sha)` | `ApproveResult` | Fails with `ApprovalMismatch` if `content_sha` differs from the current file. Runs `check_for_approval`; raises `ValidationError` on problems and leaves the file untouched. A plain item moves to `Notes/<title>.md` (suffix ` (2)` etc. on a title clash). A `revises` item rewrites the target note in place: id and `created` preserved, `sources` merged, `updated` and `reviewed` set; the inbox file is removed. |
| `discard(note_id, *, reason="")` | `ArchiveResult` | Moves the inbox note, and its source when no other note lists it, to `Archive/`, with a receipt. |
| `promote_reviewed()` | `list[ApproveResult \| dict]` | Out-of-band approval: every inbox note whose frontmatter a human changed to `status: approved` is validated and promoted. Failures go back to `status: inbox` with `review_problems`. Called on server start and before every read tool. |
| `register_tags(tags, *, meanings=None)` | `list[str]` | Unchanged behaviour, new write path. |
| `tag_catalog()` / `registered_tags()` | as today | Read-only. |
| `validate()` | `dict` | Lints `Notes/` and `Inbox/`; reports schema state and orphan sources. |

Result dataclasses live in `models.py`: `CaptureResult`, `InboxItem`, `ApproveResult`,
`ArchiveResult`.

Deletion of an approved note keeps its two-step review (`DeleteWorkflow.prepare` /
`commit`) but archives into `Archive/Notes/` and `Archive/Sources/`, regenerates maps,
and journals the change in git. Delete proposals stay under `.second-brain/`.

## Approval gate (`validation.check_for_approval`)

Blocking problems:

- summary empty, contains Markdown headings, or longer than `MAX_SUMMARY`;
- fewer than `MIN_TAGS` (1) or more than `MAX_TAGS` (5) tags;
- a tag that is malformed or not in `Maps/Tag Registry.md`;
- invalid type or domain;
- `revises` names a note that does not exist.

Aliases and maps are no longer required. `lint_vault` applies the same rules to approved
notes and reports inbox notes separately.

## Maps (`maps.py`)

A map is `Maps/<Name>.md` with frontmatter `type: map`, `map_tags: [..]`. Its body holds
human text plus one generated block:

```markdown
<!-- brain:generated:start -->
- [[Note title]] — first sentence of summary
<!-- brain:generated:end -->
```

`maps.regenerate(vault, *, only_tags=None)` rewrites the block of every map whose
`map_tags` intersect `only_tags` (all maps when `None`) from approved notes carrying any
of those tags, sorted by title. Text outside the markers is never changed. A map with no
markers gets the block appended. Inbox notes never appear on maps.

`Views/Review.base` is an Obsidian Bases table over `Inbox/` showing title, status, type,
domain, tags, created, and review_problems, so the user can approve by editing `status`.

## Vault I/O (`vaultio.py`)

- `atomic_write(path: Path, text: str) -> None`: temp file in the same directory, fsync,
  `os.replace`.
- `atomic_move(src: Path, dst: Path) -> None`: creates parents; refuses to overwrite.
- `class VaultLock(vault: Path, timeout: float = 30)`: context manager using `fcntl.flock`
  on `.second-brain/write.lock`, re-entrant within one thread.
- `class GitJournal(vault: Path)`: `.commit(message: str, paths: Iterable[Path])` stages
  exactly those paths (including deletions) and commits with
  `git -c safe.directory=<vault> -c user.name=second-brain -c user.email=second-brain@localhost`.
  Never pushes. Never commits unrelated dirty files. Errors are returned as warnings,
  never raised, so a write still succeeds when git is unavailable.

## Index (`index.py`, `embedding.py`)

- Indexes `Notes/` and `Inbox/` (`models.INDEXED_FOLDERS`). Nodes gain `status`.
- Index path: `SECOND_BRAIN_INDEX_PATH` env or `<vault>/.second-brain/index.sqlite3`.
- `reindex(*, embed_vectors=False)` never calls the embedder unless asked. Write paths
  call it with the default.
- `embedding.EmbeddingWorker(index, embedder, interval=30)`: background daemon thread
  that embeds chunks whose vector is missing, with backoff on failure; `start()`,
  `stop()`, `run_once() -> int`.
- Vectors are stored in a `sqlite-vec` `vec0` table when the extension loads; otherwise
  JSON text with Python cosine (current behaviour). Same results either way.
- `search` / `context`: always run FTS. When vectors exist and a query embedding returns
  within `QUERY_EMBED_TIMEOUT_SECONDS` (3), also run vector search and fuse with
  reciprocal rank fusion (k=60). Inbox hits are multiplied by `INBOX_SCORE_WEIGHT` (0.5)
  and returned with `"status": "inbox", "unreviewed": true`.
- `find(query, limit, include_inbox=True)`, `context(query, ..., include_inbox=True)`.

## Surfaces (`api.py`, `mcp_server.py`, `cli.py`, `synthesis.py`)

Server modes, from `SECOND_BRAIN_MODE`:

- `writer` (default): all tools.
- `reader`: write tools return `{"error": "read-only: writes go through the brain service"}`
  and never touch the vault; a reader should set its own `SECOND_BRAIN_INDEX_PATH`.

Approval modes, from `SECOND_BRAIN_APPROVAL`:

- `chat` (default): `brain_approve` works when called with `user_approved=true` and the
  matching `content_sha`.
- `human`: `brain_approve` is refused; approval happens through Obsidian
  (`status: approved`), `brain approve` on the CLI, or another out-of-band channel.

Transport: `second-brain-mcp [--transport stdio|streamable-http] [--host 127.0.0.1]
[--port 8765]`. Over HTTP, when `SECOND_BRAIN_HTTP_TOKEN` is set every request needs
`Authorization: Bearer <token>`. The writer starts `EmbeddingWorker` and runs
`promote_reviewed()` at startup.

MCP tools in schema 4:

| Read | Write |
|---|---|
| `brain_find`, `brain_ask`, `brain_read`, `brain_neighbors`, `brain_open`, `brain_list_tags`, `brain_inbox`, `brain_digest`, `brain_report`, `brain_validate` | `brain_capture`, `brain_update_inbox`, `brain_approve`, `brain_discard`, `brain_register_tags`, `brain_prepare_delete`, `brain_commit_delete` |

Removed: `brain_prepare_save`, `brain_commit_save`, `brain_reject_save`, `brain_pending`.
List arguments are native JSON arrays, not `*_json` strings.

`synthesis.py`:

- `digest(service, index) -> dict` and `render_digest(...) -> str`: inbox count, oldest
  age, one line per item with warnings; text short enough for one Telegram message
  (under 3,500 characters; truncate with a count).
- `report(service, index, *, stale_days=180) -> dict`: likely duplicates (same
  `source_url`, near-identical titles, or vector cosine ≥ 0.92 when vectors exist), notes
  with no inbound links and no map, tags used by ≥ 5 notes with no map, inbox items older
  than 14 days, orphan sources. Report only; it changes nothing.

CLI subcommands: `capture`, `inbox`, `approve`, `discard`, `promote`, `digest`, `report`,
`find`, `ask`, `reindex`, `embed`, `validate`, `doctor`, `register-tags`,
`migrate-v4` (`--dry-run` default, `--apply`, `--tag-map FILE`, `--map-tags FILE`).

## Migration (`migrate_v4.py`)

`plan(vault, *, tag_map=None, map_tags=None) -> MigrationPlan` and
`apply(plan) -> dict`. Dry run prints the plan as JSON. Apply refuses unless the vault
is a git repository with a clean working tree and `.brain-schema.json` says 3.

Steps:

1. Move `<Domain>/<Type>/*.md` to `Notes/`. On a title clash, append ` (<Domain>)` and
   rewrite `[[old]]` links across the vault.
2. Convert `source_note`/`source_id` to `sources: [...]`; attach orphan sources whose
   `distilled_note` names an existing note.
3. Apply the tag map (`old: new`, `new` may be `null` to drop); rewrite the registry.
4. Give each existing map `map_tags` (from the map-tags file) and wrap its listed entries
   in generated markers, then regenerate.
5. Convert pending proposals (`.second-brain/proposals/*.json` with status pending and a
   `Reviews/Pending/` file) to inbox notes.
6. Move `Reviews/` to `Archive/Reviews/`.
7. Remove emptied schema-3 folders, write `Views/Review.base`, stamp schema 4, reindex.
8. One git commit for the whole migration.

## Hermes and clients

- A dedicated `second-brain` container runs the writer over streamable HTTP on the
  Compose network, pinned to a tagged runtime build, with the vault bind mount and
  Ollama at `http://ollama:11434`.
- Hermes connects to it over HTTP if its MCP client supports that; otherwise Hermes keeps
  a stdio writer and the Mac clients run as readers.
- Mac clients (Codex, Claude Code) connect over `http://127.0.0.1:8765/mcp` or run as
  `reader` stdio servers with their own index path.
- Hermes cron: daily digest (weekdays 08:30), weekly report (Sunday 18:00).
- Skills are rewritten for capture → digest → approve.
