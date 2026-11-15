# Retrieval evaluation

`scripts/evaluate_retrieval.py` scores retrieval in two ways: a built-in fixture that runs
anywhere, and a real vault scored against your own queries.

## Fixture method

With no `--vault`, the script seeds a small synthetic schema 4 vault and runs a lexical
fixture. It checks top-1 accuracy, recall@5, and no-answer handling, so it catches
regressions but can't predict full-vault quality or latency.

```sh
uv run python scripts/evaluate_retrieval.py --rounds 10
uv run python scripts/evaluate_mcp_transport.py --rounds 10
```

The second script measures stdio MCP round trips against a temporary one-note vault.

## Real-vault results

The query set has 40 queries: 35 answerable and 5 with no answer. The vault has 80
approved notes, and embeddings come from `bge-m3` through Ollama.

| Run | top1 | recall@5 | MRR | no-answer | p50 ms |
|---|---:|---:|---:|---:|---:|
| v3 lexical (baseline) | 0.686 | 0.800 | 0.735 | 0.20 | 11.0 |
| v4 lexical | 0.714 | 0.800 | 0.769 | 0.20 | 15.5 |
| v4 hybrid | 0.971 | 0.986 | 0.977 | 1.00 | 235.6 |

The query set and the per-query results are private. They stay outside the repository.

### Caveats

- **Latency.** The hybrid p50 is dominated by one `docker exec` round trip per query
  embedding in the evaluation harness, not by service time. The hybrid p95 is 293 ms.
- **Thin no-answer margin.** The 0.5 vector similarity floor was tuned on this vault with
  only 5 no-answer queries. The highest no-answer similarity is 0.442 and the weakest
  answerable one is 0.546.
- **CJK queries.** Lexical search uses the SQLite FTS `unicode61` tokenizer, which misses
  CJK queries. Vector search covers them.
- **Indexing cost.** The first full embedding of 280 chunks took about 13 minutes on CPU.

## Rerun on your vault

1. Write a YAML list of `{query, expected, kind}`. `expected` is a list of note titles.
   An empty list marks a no-answer query. Keep the file outside the repository.
2. Score lexical retrieval:

   ```sh
   uv run python scripts/evaluate_retrieval.py --vault VAULT --queries QUERIES.yaml \
     --mode lexical --out lexical.json
   ```

3. Score hybrid retrieval. Hybrid mode needs `--embedder ollama` or `--embedder docker-ollama`:

   ```sh
   uv run python scripts/evaluate_retrieval.py --vault VAULT --queries QUERIES.yaml \
     --mode hybrid --embedder ollama --ollama-url http://localhost:11434 --out hybrid.json
   ```

Options:

| Flag | Default | Use |
|---|---|---|
| `--vault` | none | Vault to score. Omit it to run the fixture. |
| `--queries` | none | YAML query list |
| `--mode` | `lexical` | `lexical` or `hybrid` |
| `--embedder` | `none` | `none`, `ollama`, or `docker-ollama` |
| `--ollama-url` | `http://localhost:11434` (`ollama`), `http://ollama:11434` (`docker-ollama`) | Embedding server URL |
| `--container` | `hermes` | Container for `--embedder docker-ollama`, which embeds through `docker exec` |
| `--embed-cache` | none | JSON cache for index-time vectors |
| `--index-path` | temp directory | Scratch index file. The script never writes an index into the vault. |
| `--rounds` | `3` | Timing repetitions per query |
| `--out` | none | Write the JSON result here |

To score another checkout as a baseline, set `SECOND_BRAIN_CODE_ROOT` to its path.
