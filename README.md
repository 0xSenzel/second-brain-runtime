# Local Second Brain

A review-first knowledge layer for an Obsidian vault. Codex and Claude can capture material, search approved notes with local embeddings, follow typed graph connections, and return cited context without loading the whole vault.

Distilled knowledge belongs in `Knowledge/`; browsable tools, sites, and other references belong in `Resources/`.

## Everyday flow

1. Paste text, a webpage, YouTube link, PDF, Markdown file, prompt, or `SKILL.md` into Codex or Claude.
2. Say **“Save this to my brain.”**
3. The agent stages a proposal and shows its title, summary, destination, and suggested connections.
4. Approve or reject it. Only approval creates an approved distilled note and an unchanged raw-source note.
5. Later ask questions such as **“What prompts have I saved for UI reviews?”** The agent retrieves a small, cited bundle from the local index and knowledge graph.
6. Open the Obsidian vault when you want to browse folders, backlinks, properties, or the graph visually.

Normal questions are read-only. Pending and rejected proposals are excluded from answers.

## Installed locations

- Runtime: `/Users/senzel/Desktop/codes/second-brain-runtime`
- Vault: `/Users/senzel/Documents/second-brain`
- Embeddings: Ollama with `bge-m3`, stored locally
- Search index: `<vault>/.second-brain/index.sqlite3`

## Agent tools

The MCP server exposes:

- `brain_prepare_save` — stage a review proposal
- `brain_commit_save` — save only after explicit approval
- `brain_reject_save` and `brain_pending` — manage the review queue
- `brain_ask` — compact cited retrieval for answering
- `brain_find` — matching saved items
- `brain_neighbors` — typed graph links and backlinks
- `brain_open` — an Obsidian link for a result

The server is registered for both Codex and Claude. Restart those apps or begin a new session after installation so they discover it.

## Local maintenance

```sh
cd /Users/senzel/Desktop/codes/second-brain-runtime
uv sync
uv run pytest
uv run brain --vault /Users/senzel/Documents/second-brain doctor
uv run brain --vault /Users/senzel/Documents/second-brain reindex
uv run brain --vault /Users/senzel/Documents/second-brain pending
```

Keyword and graph search continue working if Ollama is stopped. Restart Ollama for semantic search.

## Design choices

The Markdown vault is the durable source of truth; SQLite and vectors are disposable local indexes. Every approved capture stores both the original material and a distilled note. Typed Markdown links make useful relationships explicit, while bounded retrieval avoids sending an entire private vault to an AI provider.

The implementation was informed by existing Obsidian/RAG projects, but is purpose-built around the stricter proposal-before-write boundary and shared Codex/Claude MCP interface. No third-party project source code was copied.

See [docs/RESEARCH.md](docs/RESEARCH.md) for the evaluated local alternatives and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full flow and privacy boundary.
