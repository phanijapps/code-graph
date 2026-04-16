---
name: code-graph-indexer
description: Build and refresh a local SQLite code graph for a workspace. Use this skill to index Python, Java, and TypeScript (including TSX) source trees with tree-sitter, populate a graph of files/symbols/edges, an AST index, an FTS5 keyword index, and (optionally) a sqlite-vec semantic embedding index for semantic search setup. Triggers on requests like "index my codebase", "build a code graph", "scan the workspace for symbols", "reindex changed files since origin/main", "refresh the code index", "set up semantic code search", "re-scan staged files into the graph DB", or any request to create or update the `.kg/code_kg.sqlite` database. Does NOT answer questions about the code — for that, use the companion `code-graph-query` skill.
license: MIT
metadata:
  author: code-graph
  version: "1.0"
---

# code-graph-indexer

Indexes a workspace into a single SQLite database (`.kg/code_kg.sqlite`) so a
separate query skill can answer "who calls X", "find functions without
docstrings", "semantic search for Y", etc.

This skill **builds and updates** the index. It never answers code questions
itself — for that, use `code-graph-query`.

## When to use

Use this skill when the user asks to:

- **Index / reindex / refresh / rebuild** the code graph or code index
- Set up / prepare / bootstrap semantic code search for a repo
- Scan changed files since a git ref into the index
- Reindex staged files
- Update the `.kg/` database after edits

If the user asks a *query* ("who calls `foo`?", "search for X"), they want the
`code-graph-query` skill instead — do not re-run indexing just to answer a
query.

## Prerequisites

Once per workspace:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` lives alongside this skill. It pins `tree-sitter` +
grammars, `sqlite-vec`, and (optional) `fastembed`.

Python 3.12+. On first run, `fastembed` will download an ONNX embedding
model (~130 MB for `BAAI/bge-small-en-v1.5`); budget that time. No PyTorch
required. If you don't want embeddings, pass `--no-embeddings`.

## Canonical invocations

All invocations produce **JSON on stdout** and progress/warnings on **stderr**.
The summary JSON keys are stable; callers can parse them.

### Full index of a workspace

```bash
python scripts/index.py --root . --db .kg/code_kg.sqlite --full
```

### Incremental — only files changed since a git ref

```bash
python scripts/index.py --db .kg/code_kg.sqlite --changed-since origin/main
```

### Incremental — only staged files (pre-commit hook friendly)

```bash
python scripts/index.py --db .kg/code_kg.sqlite --only-staged
```

### Specific file list

```bash
python scripts/index.py --db .kg/code_kg.sqlite \
    --paths src/foo.py src/bar.java web/app.tsx
```

### Skip embeddings (FTS + graph + AST only; much faster, no model download)

```bash
python scripts/index.py --root . --db .kg/code_kg.sqlite --full --no-embeddings
```

### Filter by language or path

```bash
python scripts/index.py --root . --db .kg/code_kg.sqlite --full \
    --include-languages python,typescript \
    --include-paths src,web
```

## Arguments

| Flag | Default | Meaning |
|---|---|---|
| `--root` | `.` | Workspace root. Paths in the DB are stored relative to this. |
| `--db` | `.kg/code_kg.sqlite` | SQLite file. Parent dir is created if missing. |
| `--full` | (default mode if no other given) | Walk `--root` and reindex everything. |
| `--paths FILE [FILE...]` | — | Reindex exactly these files. |
| `--changed-since REF` | — | `git diff --name-only REF...HEAD`. |
| `--only-staged` | — | `git diff --cached --name-only`. |
| `--include-languages` | `python,java,typescript` | CSV filter by language. |
| `--include-paths` | — | CSV root-relative path prefixes to keep. |
| `--max-file-size-bytes` | `2000000` | Skip files larger than this. |
| `--embed-model` | `BAAI/bge-small-en-v1.5` | 384-dim ONNX model (served by fastembed). |
| `--no-embeddings` | off | Skip embeddings entirely. |

Mode flags (`--full`, `--paths`, `--changed-since`, `--only-staged`) are
mutually exclusive. If none is given, `--full` is assumed.

## Output

Stdout is **one JSON object** at the end of the run:

```json
{
  "files_indexed": 42,
  "files_skipped": 3,
  "files_removed": 1,
  "symbols": 317,
  "edges": 509,
  "ast_nodes": 1804,
  "embeddings": 359,
  "duration_ms": 4213,
  "errors": []
}
```

`files_removed` counts DB rows purged for files that no longer exist on disk (only populated in `--full` mode).

`errors` is an array of `{"path": "...", "error": "..."}` objects; one
parse/IO failure per file never aborts the whole run.

## Gotchas

- **First-run embedding model download.** `fastembed` pulls
  `BAAI/bge-small-en-v1.5` (ONNX) from Hugging Face on first use (~130 MB).
  Use `--no-embeddings` if offline or constrained — FTS + graph + AST still
  work. For fully offline runs, pre-stage the model cache and set
  `FASTEMBED_CACHE_PATH=/path/to/cache`.
- **sqlite-vec platform wheels.** `sqlite-vec` ships binary wheels for
  linux-x86_64, linux-aarch64, macOS, and win64. On unsupported platforms it
  will fail to load — the indexer detects this, warns on stderr, and continues
  **without** `vec_embeddings`. FTS-only search still works in the query skill.
- **sqlite extension loading.** Some distro-packaged Python builds disable
  `sqlite3.enable_load_extension`. If you see "not authorized" errors, install
  a Python that has extension loading compiled in (pyenv / official
  python.org / conda). The indexer will fall back to FTS-only rather than
  crashing.
- **Paths are always workspace-root-relative.** Even if you pass absolute paths
  via `--paths`, they are normalized to be relative to `--root` before being
  stored as IDs (`file:<relative_path>`). Running the indexer from a different
  cwd is fine as long as `--root` is correct.
- **Idempotency.** Incremental modes (`--paths`, `--changed-since`,
  `--only-staged`) skip a file if its sha256 matches the stored hash. `--full`
  bypasses the hash check and **re-parses every file** it walks, then sweeps
  any `file_state` rows whose paths no longer exist on disk — so renames and
  deletions stay consistent.
- **Partial-parse tolerance.** Files with syntax errors are parsed
  best-effort; tree-sitter returns a tree with `ERROR` nodes and the indexer
  extracts whatever symbols it can recognize.
- **Big repos.** Walking also skips `.git`, `node_modules`, `.venv`, `venv`,
  `__pycache__`, `dist`, `build`, `.kg`, and any directory whose name starts
  with a dot.
- **TypeScript vs TSX.** `.ts` uses the `typescript` grammar; `.tsx` uses the
  `tsx` grammar (both shipped by `tree_sitter_typescript`). Both produce
  `language='typescript'` rows.
- **Call-edge dst may not resolve to a real node.** The indexer records
  `calls` edges using the best-effort callee name. If the callee isn't a
  symbol we indexed (third-party library, dynamic call), the `dst_id` is a
  synthetic `sym:<lang>:<module>:<name>` that won't join to `nodes`. The
  query skill handles this.

## Data model reference

Load `references/DATA_MODEL.md` when you need exact column semantics, ID
grammar, span encoding, or `extra` JSON shapes per AST kind.

## Validation after a run

Quick sanity check (no query skill required):

```bash
sqlite3 .kg/code_kg.sqlite "SELECT kind, COUNT(*) FROM nodes GROUP BY kind;"
sqlite3 .kg/code_kg.sqlite "SELECT type, COUNT(*) FROM edges GROUP BY type;"
sqlite3 .kg/code_kg.sqlite "SELECT COUNT(*) FROM ast_nodes;"
```

If embeddings were enabled, also:

```bash
sqlite3 .kg/code_kg.sqlite "SELECT COUNT(*) FROM vec_embeddings;"
```

If any of these are zero unexpectedly, re-run with `--full` and inspect the
`errors` array in the summary JSON.
