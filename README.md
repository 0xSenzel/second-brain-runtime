# Local Second Brain

A review-first knowledge layer for an Obsidian vault. Codex and Claude can capture
material, find and read approved notes, optionally use local embeddings when lexical search
is sparse, and return bounded cited context without loading the whole vault.

## Everyday flow

1. Paste text, a webpage, YouTube link, PDF, Markdown file, prompt, or `SKILL.md` into Codex or Claude.
2. Say **"Save this to my brain."**
3. The agent stages a proposal and shows its title, full summary and body, destination, type, domain, tags, aliases, maps, and suggested connections.
4. Approve or reject it. Only approval creates an approved distilled note and an unchanged raw-source note.
5. Use `brain_find` for matching items, then `brain_read` for an exact note body; use `brain_ask` for a broad question and its bounded evidence pack.
6. Open the Obsidian vault when you want to browse maps, backlinks, properties, or the graph visually.

Normal questions are read-only. Pending/rejected proposals, Maps, and raw Sources are excluded
from normal retrieval; source URLs remain available as provenance on the approved note.

## Save a reusable note

Pass concise prose in `summary` for search and map listings. Pass the complete reusable
Markdown in `body`: methods, examples, limits, evidence pointers, or exact prompts.
Keep the raw transcript, captions, and visual extraction in `content_or_url`; these
remain in the linked source note. Review the full summary and body before approving.

Use `brain prepare INPUT --body-file NOTE.md` to supply the body from a file.
When revising a note, omit `body` to preserve its existing Details section, or supply
its complete replacement. An empty string proposes removing that section.
Older pending proposals load with an empty body. This additive field uses the existing
proposal deserialization defaults and Markdown storage; it requires no vault migration.

## Where a note goes

A note's path is `<Domain>/<Type>/`, decided by two closed sets and nothing else:

| Domain      | For                                                          |
|-------------|--------------------------------------------------------------|
| `Personal/` | Health, training, self-reflection, style.                     |
| `Tech/`     | Tooling, code, prompts about building, technical reference.   |

| Type      | Folder      | For                                                |
|-----------|-------------|----------------------------------------------------|
| `concept` | `Notes/`    | Everything distilled that is not one of the below.  |
| `prompt`  | `Prompts/`  | Reusable prompts.                                   |
| `skill`   | `Skills/`   | Agent skills.                                       |
| `project` | `Projects/` | Material scoped to one active piece of work.        |
| `essay`   | `Essays/`   | Your own writing, not captured from someone else.   |

Shared across both domains: `Maps/` (index notes, headed by job to be done — the browsing
layer), `Sources/` (raw captures, flat, `<id>-<title>.md`), and `Reviews/`.

**Subject matter never becomes a folder.** A note about frontend design is a `concept`
tagged `frontend`, not a Frontend folder. Topic lives in tags, which are many-to-many, so
material that belongs in several places is not forced to pick one parent. Both axes are
closed sets; adding a domain or type is a deliberate edit to `models.py`, never something a
save decides on its own.

Type folders stay flat. Retrieval runs on search, tags, maps, and links, none of which care
about folder depth. Split one only when a single tag passes roughly 30 notes.

## Structure is enforced, not requested

Prose guidance is advice an agent drops under context pressure, so the rules live in code.
`commit_save` refuses a proposal that lacks:

- a **summary** — prose only, no Markdown headings, under 1500 characters;
- at least **2 tags**, all registered in the vault's `Maps/Tag Registry.md`;
- at least **2 aliases**, covering other phrasings someone would search for later;
- at least **1 map**, since a note on no map cannot be found by browsing.

`brain_prepare_save` reports the same failures as `blocking_problems` before approval, so
they are fixed while it still costs nothing. Re-preparing the same capture retires the
earlier attempt, so a retry does not strand a dead proposal in the queue. Notes edited by
hand bypass all of this — `brain validate` applies the same rules to the whole vault.

## Schema version

The code and the notes live in separate repositories, so they can move independently and
nothing else records that they belong together. `models.SCHEMA_VERSION` is the layout this
code produces, and it is stamped into the vault as `.brain-schema.json`:

| Version | Layout |
|---------|--------|
| 1 | Flat `Prompts/ Skills/ Knowledge/ Resources/ Projects/`, routed by a single `kind`. |
| 2 | `<Domain>/<Type>/`, closed sets, structure enforced at commit. |
| 3 | Same folders as 2. The stamp gained axis validation and lost its timestamp, and runtimes older than this reorganise a vault before checking the stamp, so they are fenced out. |

The stamp is tracked rather than kept under the gitignored `.second-brain/` index, so it
travels with a clone. On startup the runtime compares the two:

- **Match** — everything proceeds. The stamp is rewritten only when its content would
  actually change, so opening the vault never dirties the notes repository.
- **Vault newer** — it was written by a runtime you have not pulled yet.
- **Vault older** — it needs migrating to the layout this code produces.

A mismatch blocks writes and leaves reads working, because retrieving from a vault whose
layout has moved is harmless, while writing into it puts notes where neither version will
look for them. Nothing is created, moved, or removed either — opening a vault this code does
not understand leaves it exactly as it was. `brain doctor` and `brain validate` report it.

Bump `SCHEMA_VERSION` when a change would leave existing notes in the wrong place, and ship
the vault migration in the same breath. Also bump when an older runtime must be stopped from
writing to a vault this version has touched, as with 2 to 3 — that upgrade moves no notes,
so migrating a v2 vault means deleting `.brain-schema.json` and letting it be re-stamped.

## Agent tools

The MCP server exposes:

- `brain_prepare_save` — stage a review proposal
- `brain_commit_save` — save only after explicit approval
- `brain_reject_save` and `brain_pending` — manage the review queue
- `brain_register_tags` — extend the controlled vocabulary
- `brain_validate` — check every note against the structural rules, including the schema stamp
- `brain_find` — compact matching items with stable IDs, short snippets, and provenance
- `brain_read` — exact approved note body or one bounded section by stable ID
- `brain_ask` — up to four cited evidence notes in a 4,000-token context budget
- `brain_neighbors` — typed graph links and backlinks
- `brain_open` — an Obsidian link for a result

The rules above travel with these tools as MCP server instructions, so a client running
outside the vault directory still gets them. The server is registered for both Codex and
Claude; restart those apps after changing the code so they pick it up.

## Installed locations

- Runtime: `/Users/senzel/Desktop/codes/second-brain-runtime`
- Vault: `/Users/senzel/Documents/second-brain`
- Embeddings: Ollama with `bge-m3`, stored locally
- Search index: `<vault>/.second-brain/index.sqlite3`

## Local maintenance

```sh
cd /Users/senzel/Desktop/codes/second-brain-runtime
uv sync
uv run pytest
uv run brain --vault /Users/senzel/Documents/second-brain doctor
uv run brain --vault /Users/senzel/Documents/second-brain validate
uv run brain --vault /Users/senzel/Documents/second-brain reindex
uv run brain --vault /Users/senzel/Documents/second-brain pending
```

Keyword and graph search continue working if Ollama is stopped. Restart Ollama for
semantic search.

## Design choices

The Markdown vault is the durable source of truth; SQLite and vectors are disposable local
indexes. Every approved capture stores both the original material and a distilled note.
Typed Markdown links make useful relationships explicit, while bounded retrieval avoids
sending an entire private vault to an AI provider.

Lexical search is the normal path. `brain_find` stays lexical; `brain_ask` tries local
semantic search only when lexical search returns no results, and falls back cleanly if
Ollama fails. Foreground retrieval updates the lexical index without embedding work.
Reindexing checks canonical note paths and file metadata on each request, but reads and
rebuilds only changed notes; Markdown remains the source of truth. `brain_find` does not
expand graph links by default. `brain_ask` returns at most four notes and 4,000 tokens, and
graph expansion is opt-in. Raw sources and Maps are not in normal retrieval, so a raw
capture or a map listing cannot crowd out approved notes.

The implementation was informed by existing Obsidian/RAG projects, but is purpose-built
around the stricter proposal-before-write boundary and shared Codex/Claude MCP interface.
No third-party project source code was copied.

See [docs/RESEARCH.md](docs/RESEARCH.md) for the evaluated local alternatives and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full flow and privacy boundary.
