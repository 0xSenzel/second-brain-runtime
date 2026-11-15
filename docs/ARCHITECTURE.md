# Second brain architecture (schema 4)

The second brain is a Markdown knowledge vault in Obsidian with one service that writes
to it. Agents capture material into an inbox in a single call. You review the inbox in
batches, in chat, in Telegram, or in Obsidian. Approved notes become the trusted
library. Search covers both, and it labels inbox items as unreviewed.

The rest of this page shows the system from the outside in: where each part runs, how a
capture becomes a note, how search ranks results, and how the service keeps the vault
consistent.

## System context

```mermaid
flowchart LR
    you(["You"])
    tg["Telegram"]
    phone["Obsidian on phone"]
    mac["Obsidian on Mac"]
    cc["Claude Code / Codex on Mac"]

    subgraph docker["Docker on the Mac (Compose network)"]
        hermes["Hermes gateway<br/>agent + skills + approval plugin"]
        capture["video-capture<br/>browser + yt-dlp"]
        worker["video-worker<br/>retention"]
        brain["second-brain<br/>MCP writer (HTTP :8765)"]
        ollama["Ollama<br/>bge-m3 embeddings"]
    end

    vault[("Vault<br/>~/Documents/second-brain<br/>Markdown + git")]
    sync(["Obsidian Sync"])

    you --> tg --> hermes
    you --> phone
    you --> mac
    you --> cc
    hermes -- "MCP over HTTP" --> brain
    hermes --> capture
    cc -- "MCP over HTTP<br/>127.0.0.1:18765" --> brain
    brain -- "embed" --> ollama
    brain -- "read/write" --> vault
    hermes -. "read-only mount" .-> vault
    worker -. "read-only mount" .-> vault
    mac <--> vault
    phone <--> sync <--> mac
```

| Part | Runs where | Role |
|---|---|---|
| `second-brain` | Its own container, pinned to a tagged runtime build | The only process that writes the vault. Serves MCP over streamable HTTP. |
| Hermes | `hermes` container | Telegram agent. Extracts content, calls brain tools, sends the daily digest. |
| Approval plugin | Inside Hermes | Runs `/brain_review` and `/brain_approve` without a model turn, so approval comes from you, not the model. |
| Ollama | `ollama` container | Local `bge-m3` embeddings. No note text leaves the machine for embedding. |
| Vault | Mac file system, bind-mounted | Source of truth. Git records every write. Obsidian Sync carries it to the phone. |
| Claude Code, Codex | Mac | Connect to the writer over HTTP on `127.0.0.1`, or run a read-only stdio server. |

## Repositories and locations

Five git repositories and two unversioned locations make up the system. Each one has a
single job, and the boundary between them is a deliberate trust or lifecycle split.

```mermaid
flowchart LR
    subgraph code["Code repositories (GitHub)"]
        rt["second-brain-runtime<br/>0xSenzel/second-brain-runtime<br/>brain code + deploy/ staging"]
        vs["video-saving<br/>0xSenzel/video-saving<br/>Douyin and YouTube capture"]
        ha["hermes-agent<br/>NousResearch/hermes-agent<br/>upstream, read only"]
    end
    subgraph data["Data repository (GitHub)"]
        vault[("second-brain<br/>0xSenzel/second-brain<br/>the vault: Markdown notes")]
    end
    subgraph mac["Mac, not versioned"]
        dep["~/.hermes/deployment<br/>compose files, token,<br/>image build contexts"]
        reg["~/.agent-registry (local git)<br/>MCP declarations for<br/>Claude Code and Codex"]
    end
    subgraph docker["Docker"]
        img1["image second-brain-runtime:4.0.x"]
        img2["image hermes-local:durable-video-*"]
        img3["image hermes-video-capture"]
        vol[("volume hermes-data (/opt/data)<br/>live Hermes config, .env,<br/>skills, plugins, cron")]
    end

    rt -- "git tag → docker build" --> img1
    rt -- "deploy/hermes/runtime-patch<br/>copied into" --> dep
    rt -- "deploy/hermes/skills, plugin,<br/>config snippet copied into" --> vol
    ha -. "official image<br/>nousresearch/hermes-agent" .-> img2
    dep -- "hermes-runtime/ build" --> img2
    vs -- "docker build" --> img3
    img1 -- "only writer (rw mount)" --> vault
    img2 -. "read-only mount" .-> vault
    reg -- "declares relay command;<br/>Claude and Codex configs copy it" --> rt
```

| Location | What it owns | Why it is separate | How a change ships |
|---|---|---|---|
| `~/Desktop/codes/second-brain-runtime` (this repository) | The brain: `src/second_brain/` (service, index, API, MCP server, relay, migration), tests, these docs, and `deploy/`, which stages every file that goes into Hermes or the Mac clients | Reusable code with no private content, so it can be shared and versioned on its own | Tag a release, rebuild `second-brain-runtime:<tag>`, recreate the `second-brain` container. Mac clients run the relay from this checkout, so they pick up `master` on their next start |
| `~/Documents/second-brain` (vault) | Notes, sources, maps, archive, `.brain-schema.json`; `.second-brain/` holds the gitignored index and locks | Private content with its own history. Git records one commit per brain write, and Obsidian Sync carries it to the phone | Only the brain writes it. The git journal never pushes; push it yourself when you want an off-machine copy |
| `~/.hermes/video-saving` | The capture service (browser, yt-dlp, media checks), Hermes capture clients, and media-processing scripts | Downloads and browses untrusted sites, so it runs isolated with no vault mount and no Hermes credentials | Rebuild `hermes-video-capture` with `~/.hermes/deployment/update-video-capture.sh` |
| `~/.hermes/hermes-agent` | Upstream Hermes source, also used to build the Hermes desktop app | Third-party code; read for reference, not edited | The container runs the pinned official image `nousresearch/hermes-agent:v0.22.5`, not this checkout |
| `~/.hermes/deployment` (not versioned) | `compose.yaml` and `compose.second-brain.yaml`, `second-brain.env` (the bearer token), `hermes-runtime/` (builds `hermes-local` from the official image plus the durable-video plugin and retention worker), `second-brain/` (build context), `migration/` | Machine-specific wiring and secrets | Edit, then `docker compose up -d <service>`. The tracked copy of each change lives in `deploy/` in this repository |
| Docker volume `hermes-data` (`/opt/data` in Hermes) | The live Hermes `config.yaml`, `.env`, skills, plugins (including `second-brain-approval`), cron jobs, sessions | Hermes owns it at runtime | Install files with a short-lived helper container so they stay owned by the `hermes` user (see `deploy/README.md`) |
| `~/.agent-registry` (local git) | `mcp/second-brain.json`: the declaration that Claude Code (`~/.claude.json`) and Codex (`~/.codex/config.toml`) must match | One source for MCP entries across tools | Change it in a registry maintenance transaction, update the tool configs, then `agent-sync doctor` |

`~/.hermes/config.yaml` and `~/.hermes/skills` on the Mac are older copies. The running
agent and the desktop app (which connects to `127.0.0.1:9119`) use the copies in the
`hermes-data` volume.

## Runtime modules

```mermaid
flowchart TB
    subgraph surface["Surfaces"]
        mcp["mcp_server.py<br/>tools, instructions,<br/>stdio or HTTP + bearer token"]
        cli["cli.py<br/>brain ..."]
    end
    api["api.py — BrainAPI<br/>modes: writer / reader<br/>approval: chat / human"]
    subgraph core["Write path"]
        service["service.py — BrainService<br/>capture, inbox, approve,<br/>discard, promote_reviewed"]
        validation["validation.py<br/>approval gate, lint"]
        notes["notes.py<br/>render and parse notes"]
        maps["maps.py<br/>generated maps, Review.base"]
        vaultio["vaultio.py<br/>lock, atomic write, git journal"]
        deletion["deletion.py<br/>two-step archive"]
    end
    subgraph read["Read path"]
        index["index.py — BrainIndex<br/>FTS5 + vectors, RRF"]
        embedding["embedding.py<br/>OllamaEmbedder, EmbeddingWorker"]
    end
    synthesis["synthesis.py<br/>digest, weekly report"]
    ingest["ingest.py<br/>URL, PDF, YouTube, file"]
    migrate["migrate_v4.py<br/>schema 3 → 4"]

    mcp --> api
    cli --> api
    cli --> migrate
    api --> ingest
    api --> service
    api --> index
    api --> synthesis
    api --> deletion
    service --> validation
    service --> notes
    service --> maps
    service --> vaultio
    deletion --> vaultio
    deletion --> maps
    index --> embedding
    synthesis --> index
```

`models.py` holds the shared constants and result types that every module imports.

## Note lifecycle

```mermaid
stateDiagram-v2
    [*] --> Inbox: brain_capture
    Inbox --> Inbox: brain_update_inbox
    Inbox --> Notes: approve (chat, /brain_approve, CLI)
    Inbox --> Notes: status approved in Obsidian, then promote_reviewed
    Inbox --> Inbox: promotion fails, review_problems written
    Inbox --> ArchiveInbox: brain_discard
    Notes --> Notes: approve an inbox item that revises it
    Notes --> ArchiveNotes: prepare_delete, then commit_delete
    ArchiveInbox --> [*]
    ArchiveNotes --> [*]

    state "Archive/Inbox" as ArchiveInbox
    state "Archive/Notes" as ArchiveNotes
```

- **Inbox** notes are searchable at once, ranked at half weight, and marked
  `unreviewed`. They never appear on maps.
- **Approval** binds to `content_sha`, the SHA-256 of the inbox file you reviewed. Any
  later edit changes the hash, and the approval no longer applies.
- **A revision** keeps the note's `id` and `created` date and appends the new source to
  `sources`. Git keeps the previous text.
- **Archive** moves never delete. Each archive action writes a receipt.

## Capture and review

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant TG as Telegram
    participant H as Hermes agent
    participant P as Approval plugin
    participant B as second-brain
    participant V as Vault (git)

    You->>TG: Share a link, "save this"
    TG->>H: Message
    H->>H: Extract transcript, frames, provenance
    H->>B: brain_find(topic)
    B-->>H: Related notes
    H->>B: brain_capture(summary, body, raw content, tags, revises?)
    B->>V: Sources/… + Inbox/… , git commit
    B-->>H: note_id, content_sha, problems
    H-->>TG: "Saved to Inbox" + problems to fix later

    Note over H,B: Weekdays 08:30, Hermes cron
    H->>B: brain_digest
    B-->>H: Inbox items with note_id and sha prefix
    H-->>TG: Digest

    You->>TG: /brain_review
    TG->>P: Command (no model turn)
    P->>B: brain_inbox, then brain_inbox(oldest note_id)
    B-->>P: Full note + content_sha
    P-->>TG: Summary and body verbatim
    You->>TG: /brain_approve
    TG->>P: Command (no model turn)
    P->>B: brain_inbox(note_id), sha unchanged?, then brain_approve(note_id, content_sha)
    B->>B: Check sha and approval gate
    B->>V: Inbox → Notes, maps regenerated, git commit
    B-->>P: Approved path
    P-->>TG: Done
```

You can also approve in Obsidian. Open **Views/Review.base**, change `status` to
`approved`, and the writer promotes the note the next time it runs a tool or restarts.
When the gate fails, the note stays in `Inbox/` and its `review_problems` property says why.

When the approval plugin is installed, Hermes hides `brain_approve` from the model. A
prompt injection in captured content can then write to the inbox, but it can't approve
anything.

## Write path

```mermaid
flowchart LR
    call["Write tool call"] --> mode{"Writer mode?"}
    mode -- "no (reader)" --> ro["Return read-only error"]
    mode -- "yes" --> schema{"Schema stamp = 4?"}
    schema -- "no" --> block["SchemaMismatch:<br/>run brain migrate-v4"]
    schema -- "yes" --> lock["VaultLock<br/>flock .second-brain/write.lock"]
    lock --> compute["Compute every new file"]
    compute --> gate{"Approval gate<br/>(approve only)"}
    gate -- "problems" --> refuse["ValidationError,<br/>vault unchanged"]
    gate -- "pass" --> write["atomic_write / atomic_move<br/>temp file + fsync + rename"]
    write --> regen["maps.regenerate<br/>for affected tags"]
    regen --> git["GitJournal.commit<br/>only the touched paths"]
    git --> reindex["Lexical reindex"]
    reindex --> queue["Embedding worker<br/>fills missing vectors later"]
```

- One writer process plus a file lock removes write races between clients.
- Atomic renames mean a crash leaves either the old file or the new one, never half of one.
- The git journal commits only the paths a write touched, so your own uncommitted edits
  stay out of brain commits. Git never pushes.
- Embedding runs on a background thread. A slow or stopped Ollama never blocks a save.

## Search

```mermaid
flowchart TB
    q["Query"] --> promote["promote_reviewed()<br/>(writer only)"]
    promote --> refresh["Incremental lexical reindex<br/>(stat-based)"]
    refresh --> fts["FTS5 BM25 over chunks<br/>title weighted 3×"]
    refresh --> vecq{"Vectors stored and<br/>query embeds in ≤ 3 s?"}
    vecq -- "yes" --> vec["Vector search, cosine ≥ 0.5<br/>sqlite-vec, or JSON + cosine"]
    vecq -- "no" --> warn["Warning: lexical only,<br/>every lexical hit kept"]
    vec --> gate["Lexical hits must contain half<br/>of the distinctive query terms"]
    fts --> gate
    fts -. "lexical only" .-> rrf
    gate --> rrf["Reciprocal rank fusion, k = 60"]
    vec --> rrf
    rrf --> weight["Inbox hits × 0.5,<br/>flag unreviewed"]
    weight --> out{"Tool"}
    out -- "brain_find" --> find["Compact hits:<br/>id, title, snippet, status"]
    out -- "brain_ask" --> ask["Up to 4 cited notes<br/>within the token budget"]
    out -- "brain_read" --> readn["Exact note body by id"]
```

When vectors are available, a lexical hit must also contain at least half of the
distinctive query terms. Prefix matching otherwise returns loosely related notes for
questions the vault can't answer. On the real-vault evaluation, hybrid search raised
top-1 accuracy from 0.71 to 0.97 and no-answer accuracy from 0.20 to 1.00. For the
method and caveats, see `docs/RETRIEVAL_EVALUATION.md`.

Search indexes `Notes/` and `Inbox/` only. Sources, maps, and archive are excluded, so raw
transcripts and listings can't crowd out distilled notes. The index lives in
`.second-brain/index.sqlite3`, or at `SECOND_BRAIN_INDEX_PATH`, and can be rebuilt from the
Markdown at any time. Vectors are stored as JSON in the index. When the `sqlite-vec`
extension loads, the index mirrors them in a `vec0` table for faster search.

## Vault layout

```mermaid
flowchart LR
    root[("Vault root")]
    root --> inbox["Inbox/<br/>status: inbox"]
    root --> notes["Notes/<br/>status: approved, flat"]
    root --> sources["Sources/<br/>raw captures, untrusted"]
    root --> mapsd["Maps/<br/>generated maps,<br/>Tag Registry.md"]
    root --> archive["Archive/<br/>Inbox, Notes, Sources,<br/>Reviews, Receipts"]
    root --> views["Views/<br/>Review.base"]
    root --> stamp[".brain-schema.json<br/>schema_version: 4"]
    root --> state[".second-brain/<br/>index, locks (gitignored)"]
    notes -- "sources:" --> sources
    inbox -- "sources:" --> sources
    mapsd -- "map_tags select" --> notes
```

Folders express lifecycle. Type (`concept`, `prompt`, `skill`, `project`, `essay`) and
domain (`personal`, `tech`) are frontmatter properties, checked against closed sets in
code. Topic lives in tags from `Maps/Tag Registry.md`. A map is a note with `map_tags`.
The service rewrites only the block between `<!-- brain:generated:start -->` and
`<!-- brain:generated:end -->`, so text you write around it stays.

## Approval gate

A note can't reach `Notes/` unless it has:

- a prose summary without Markdown headings, up to 1,500 characters;
- one to five tags, all registered;
- a valid type and domain;
- a `revises` target that exists, for a revision.

Capture accepts notes that fail these rules and reports the problems. Approval refuses
them.

## Trust boundaries

```mermaid
flowchart LR
    subgraph untrusted["Untrusted"]
        web["Web pages, videos,<br/>captions, PDFs"]
        srcs["Sources/ content"]
    end
    subgraph agent["Model-driven"]
        model["Hermes / Claude / Codex model"]
    end
    subgraph human["Human-only"]
        cmd["/brain_approve command"]
        obs["Obsidian status edit"]
        clia["brain approve (CLI)"]
    end
    subgraph service["second-brain"]
        cap["capture → Inbox"]
        appr["approve → Notes"]
    end
    web --> model
    model --> cap
    cap --> srcs
    cmd --> appr
    obs --> appr
    clia --> appr
    model -. "only if the plugin is not installed<br/>(chat approval, sha-bound)" .-> appr
```

- The writer listens on the Compose network. The host publishes it only on
  `127.0.0.1:18765`, and every request needs the bearer token.
- Captured text is stored as data and marked untrusted. Tools never act on instructions
  in it.
- Embeddings run locally. Only the small evidence packs that search returns go to the
  active agent's model provider.

## Background jobs

| Job | Where | Schedule | Effect |
|---|---|---|---|
| Embedding worker | `second-brain` thread | Every 30 seconds, with backoff | Fills missing vectors. |
| Promote reviewed | `second-brain` | Startup and before reads | Promotes inbox notes you approved in Obsidian. |
| Inbox digest | Hermes cron | Weekdays 08:30 | Sends the inbox list to Telegram. |
| Weekly report | Hermes cron | Sundays 18:00 | Duplicates, unlinked notes, tags without maps, stale inbox, orphan sources. Report only. |
| Video retention | `video-worker` | Hourly | Prunes captured media after its note is approved or discarded. |

## Migration from schema 3

```mermaid
flowchart TB
    start["brain migrate-v4 --dry-run"] --> review["Review plan JSON,<br/>tag-map.yaml, map-tags.yaml"]
    review --> pre{"git repo, clean tree,<br/>stamp 3, HEAD unchanged?"}
    pre -- "no" --> stop["Refuse"]
    pre -- "yes" --> m1["Move Domain/Type notes to Notes/,<br/>rename clashes, rewrite links"]
    m1 --> m2["source_note → sources,<br/>attach orphan sources"]
    m2 --> m3["Apply tag map,<br/>rewrite registry"]
    m3 --> m4["Add map_tags,<br/>wrap entries in markers"]
    m4 --> m5["Pending proposals → Inbox/"]
    m5 --> m6["Reviews/ → Archive/Reviews/"]
    m6 --> m7["Remove empty schema-3 folders,<br/>write Review.base, stamp 4"]
    m7 --> m8["Regenerate maps, reindex"]
    m8 --> commit["One git commit"]
    m1 & m2 & m3 & m4 & m5 & m6 & m7 & m8 -. "any failure" .-> rollback["Reset to HEAD,<br/>remove only created paths"]
```

The deployment runbook in `deploy/README.md` orders the live steps: snapshot the vault,
build the pinned image, migrate, switch Hermes, verify, and roll back if needed.

## Rollout

```mermaid
flowchart TB
    s0["0. Snapshot the vault<br/>git commit + tag"]:::live
    s1["1. Tag runtime v4.0.0, build second-brain image,<br/>rebuild Hermes image with runtime-patch"]
    s2["2. Compose service, token env file,<br/>tag-map.yaml, map-tags.yaml"]
    s3["3. Stop writers, pause Obsidian Sync,<br/>brain migrate-v4 --apply"]:::live
    s4["4. Start second-brain, switch Hermes config,<br/>skills, plugin, cron; Hermes /vault read-only"]:::live
    s5["5. Verify: capture → inbox → approve,<br/>discard, Obsidian promotion, digest, git log"]
    s6["Point Claude Code / Codex<br/>at 127.0.0.1:18765"]:::live
    s0 --> s1 --> s2 --> s3 --> s4 --> s5 --> s6
    s3 -. "fails" .-> r3["git reset to the snapshot tag"]
    s4 -. "fails" .-> r4["Restore old Hermes config,<br/>skills, and image"]
    classDef live stroke-width:3px
```

Steps with a thick border change live state. `deploy/README.md` holds the exact
commands and the rollback for each step.

