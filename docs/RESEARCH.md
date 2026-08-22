# Existing-project research

Reviewed on 2026-08-08. Links and project status re-verified on 2026-08-22; the
comparisons themselves have not been re-run.

Several strong local projects cover parts of this idea, but none of the reviewed options combines the exact workflow required here: shared Codex and Claude access, Obsidian-native Markdown, local hybrid retrieval, raw-plus-distilled storage, a mandatory proposal/approval boundary before every approved write, and a structure the runtime refuses to violate.

That last requirement is the sharpest one, and it was added after this review. None of the
projects below will decline a write because a note lacks the tags, aliases, or map entry
that make it findable again; they ingest what they are given. Enforcing that at the
boundary is what a general-purpose tool cannot do on your behalf, because only you know
what "findable" means for your library.

## Closest options

| Project | Strong at | Why it was not used as the complete system |
| --- | --- | --- |
| [Khoj](https://github.com/khoj-ai/khoj) | A mature self-hosted second brain with Obsidian, document chat, semantic search, local or hosted models, and many clients. | It introduces a larger application and server stack. Its normal chat/search model does not provide this vault's exact raw-plus-distilled, one-proposal/one-approval write contract. |
| [Smart Connections](https://github.com/brianpetro/obsidian-smart-connections) | Local-first embeddings and semantically related notes directly inside Obsidian. | Excellent as an Obsidian discovery interface, but it is not the shared review-first capture service for Codex and Claude. |
| [QMD](https://github.com/tobi/qmd) | Local BM25, vector search, and reranking for Markdown and other documents. | A compelling retrieval engine, but it does not own the capture, review, source-preservation, typed-graph, and Obsidian dashboard workflow. It remains the most likely replacement for the small custom index. That case has strengthened since this review: the hand-rolled ranking has already needed correcting for scoring documents by length and for compounding graph hops, both of which a purpose-built engine handles as a matter of course. |
| [Obsidian MCP Server](https://github.com/Vasallo94/obsidian-mcp-server) | Safe, vault-native MCP access for Codex and Claude, with optional writes, graph tools, and external RAG integration. | Closest at the access layer, though a small project (single-digit stars as of 2026-08-22), so this is a comparison of approach rather than of a mature alternative. Its generic optional writes do not enforce the two-call proposal/approval workflow, the structural rules, or paired raw and distilled notes. |
| [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm) | A full private document-chat application with local/cloud model support and MCP compatibility. | Adds another primary chat UI and document workspace when the desired primary UI is already Codex or Claude and the durable library is Obsidian. |

## Decision

Use a small purpose-built layer now and keep the vault format portable. The implementation borrows established architectural ideas—Markdown as source of truth, local embeddings, hybrid search, and MCP—but its approval boundary and note schema are specific to this workflow. No third-party source code was copied.

The design deliberately leaves replacement seams: SQLite search can later be replaced by QMD, and generic vault operations can later be delegated to an established Obsidian MCP server without migrating the Markdown library. The Markdown and its frontmatter are the contract in both cases, and `SCHEMA_VERSION` records which version of that contract a vault was written against.

What would not survive such a swap is the structural gate, since it lives in the write
path rather than in the files. Replacing the retrieval half is cheap; replacing the
capture half means reimplementing the rules.
