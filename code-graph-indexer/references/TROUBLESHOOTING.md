# Troubleshooting

## Embedding model can't be downloaded

Symptom: on the first `--full` run with embeddings, you see one of:

- `SSLError` / `CERTIFICATE_VERIFY_FAILED` (corporate TLS interception)
- `ConnectionError` to `huggingface.co` (HF blocked by network policy)
- `OSError: [Errno -3] Temporary failure in name resolution` (offline)
- The `Fetching N files:` progress bar on stderr never moves

**Do this:** tell the user the embedding model can't be downloaded in
this environment, and rerun the indexer with `--no-embeddings`:

```bash
python scripts/index.py --root . --db .kg/code_kg.sqlite --full --no-embeddings
```

You lose semantic `--search`. You keep **everything else**:

- FTS5 keyword search
- Full call graph (callers, callees, neighbors)
- All AST queries (functions without docstrings, try/except without
  logging, `--ast-call`)
- `--explain-symbol`, `--explain-file`
- Cross-repo dependency analysis via `imports` edges

This covers the majority of agent use cases. Do not spend time fighting
certs unless the user explicitly asks for semantic search.

## `sqlite-vec` won't load

Symptom on stderr:

```
warn: failed to load sqlite-vec: ...
warn: vec_embeddings table not created; FTS-only results
```

The indexer **automatically** continues without the vector table. No
action needed — FTS + graph + AST still work. If the user wants
semantic search, they need a Python build with extension loading
enabled (pyenv, python.org, or conda all ship with it).

## That's it

Every other failure mode reduces to one of the above: either the
embedding step fails and `--no-embeddings` is the fix, or sqlite-vec
can't load and the skill already falls back. Don't build elaborate
cert / proxy setup instructions unless the user asks for them.
