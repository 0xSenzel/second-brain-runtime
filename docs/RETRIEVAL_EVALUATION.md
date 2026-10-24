# Retrieval baseline and evaluation

Run the repeatable synthetic fixture from the repository root:

```sh
.venv/bin/python scripts/evaluate_retrieval.py --rounds 10
.venv/bin/python scripts/evaluate_mcp_transport.py --rounds 10
```

The fixture creates two approved canonical notes and runs in lexical-only mode. It measures
warm API latency, JSON response bytes, top-1 accuracy, recall@5, answer evidence, and the
no-answer rate. `tool_calls` counts the equivalent MCP calls made by each flow (`brain_find`,
`brain_ask`, plus `brain_read` for the exact-read case). These are local fixture measurements,
not measurements of a live vault or MCP transport. The exact-read case fetches the canonical
body; the broad-question case checks for a body-only fact in the evidence pack.
The latency values below are recorded 10-round samples of the direct API fixture and local
MCP transport; timing varies between runs, and the small fixture does not predict full-vault
performance.
The MCP transport script launches the configured `uv run ... second-brain-mcp` entry point
against a temporary one-note vault and measures actual stdio tool round trips after startup.
Its `mean_tool_text_bytes_per_flow` metric counts the UTF-8 text returned by the two tools; it
does not include JSON-RPC framing or transport overhead.

| Case | Earlier p50 | Recorded p50 | Recorded p95 | Mean response bytes | Calls | Top-1 | Recall@5 | Body evidence | No-answer accuracy | Exact body |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Known title | 7.36 ms | 3.50 ms | 3.89 ms | 767 B | 1 | 1.0 | 1.0 | — | — | No |
| Paraphrase | 7.23 ms | 3.40 ms | 4.59 ms | 789 B | 1 | 1.0 | 1.0 | — | — | No |
| Chinese alias | 7.27 ms | 3.33 ms | 4.01 ms | 710 B | 1 | 1.0 | 1.0 | — | — | No |
| Ambiguous matching-items request | — | 4.03 ms | 4.57 ms | 1,406 B | 1 | 1.0 | 1.0 | — | — | No |
| UI-note query + exact read | 7.71 ms | 7.81 ms | 8.44 ms | 2,431 B | 2 | 1.0 | 1.0 | — | — | Yes |
| Body-only broad question | — | 4.47 ms | 4.91 ms | 2,748 B | 1 | 1.0 | 1.0 | 1.0 | — | No |
| No answer | 7.27 ms | 2.87 ms | 3.60 ms | 82 B | 1 | — | — | — | 1.0 | No |

The earlier p50 values cover the original five cases only; the recorded run adds explicit
ambiguous matching, broad-question evidence, and per-case accuracy metrics. All expected
matches appeared within the top five in this two-note fixture, and the body-only fact and
no-answer case were handled correctly. Exact-find responses are smaller because they contain
compact matching notes rather than repeated source and map entries. The UI-note flow spends a
second call and returns the full note body.

Live integration check: `brain_find("UI microinteraction")` returned the approved note
`Tech/Notes/Microinteraction patterns for polished AI apps.md`, then `brain_read` fetched
its body by stable ID. The read reported `truncated: false` and zero omitted characters.
Ten sequential live MCP find/read rounds measured find p50/p95 of **499/777 ms** and read
p50/p95 of **419/470 ms**. The read returned the same canonical path and full 3,845-character
body on every round. This connected-process sample predates the local API-cache and
frontmatter-scan optimizations implemented in this checkout.

The recorded seven-case API fixture run reported overall p50 **3.77 ms** and p95 **7.88 ms**;
exact-read API latency was p50 **3.61 ms** and p95 **4.17 ms**. A separate 10-round run of
the current checkout through `uv run --directory <checkout> second-brain-mcp` measured local
MCP find p50/p95 **5.30/11.54 ms**, read **4.55/5.19 ms**, and the two-call flow **9.90/16.78 ms**.
It returned the full body in every round. These figures are one sample; repeated runs vary.

The connected live MCP p95 values exceeded the proposed targets: warm lexical p95 below
500 ms and exact reads below 250 ms. Those values include MCP invocation overhead and do not
isolate service time; the connected process was not reloaded after these local optimizations.
The fixtures are useful for correctness and regression checks, but are too small to establish
latency or retrieval quality for the full vault.
