---
name: code-graph-query
description: Use this skill to answer questions about a codebase by querying a pre-built SQLite code graph. Triggers on "who calls X", "find callers/callees of <func>", "trace the call graph for <func>", "search the codebase for <concept>", "find functions without docstrings", "find try/except blocks that swallow exceptions", "explain this symbol/class/file", "find all call sites of <func>", "find usages of <symbol>", "what does <symbol> call", "show me where <func> is used" — even when the user does not mention indexing, graphs, or SQLite. Runs FTS5 keyword search, optional semantic vector search, BFS over call/import/contains edges, and AST-level structural queries. Read-only; requires a database built by the `code-graph-indexer` skill. Do not use for simple in-file text search (use grep instead) or when the task is to edit code.
license: MIT
---

# code-graph-query

Read-only CLI over a code-graph SQLite index. Answers "who calls X?", "find functions missing docstrings", "search for concept Y", etc. by running one subcommand of `scripts/query.py` and returning JSON.

## Preconditions

- A SQLite DB produced by the `code-graph-indexer` skill must exist (default path: `.kg/code_kg.sqlite`). If it is missing or stale, ask the user to run the indexer first — do not attempt to index from this skill.
- `python3` on PATH. `sqlite-vec` and `sentence-transformers` are optional; the script degrades gracefully to FTS-only if either is absent.

## How to use

Always invoke `scripts/query.py` with `--db <path>` and exactly one mode flag. Output is a single JSON document on stdout. Parse it; never rely on stderr for data (stderr carries warnings only). Exit code 0 always means the command ran; 2 means bad args or unreadable DB.

```
python3 scripts/query.py --db .kg/code_kg.sqlite <MODE>
```

## Mode reference

Pick the narrowest mode that answers the user's question. Defaults are good; only pass `--top-k` / `--max-depth` / `--radius` when the user asks for more or less.

### `--search TEXT` (keyword + semantic)
Use for "search the codebase for …", "find code related to …", "where do we handle rate limiting".
```
python3 scripts/query.py --db .kg/code_kg.sqlite --search "rate limit retry" --top-k 15
```
Returns `{"mode":"search","query":"rate limit retry","results":[{"id","kind","path","name","signature","language","span_start","span_end","fts_rank","vec_distance","score"} …]}`. Results are ranked by reciprocal-rank fusion of FTS5 and (optional) vector distance. If embeddings are missing, `vec_distance` is `null` and the warning goes to stderr.

### `--callers SYMBOL` (who calls X)
```
python3 scripts/query.py --db .kg/code_kg.sqlite --callers ingest_file --max-depth 2
```
Returns `{"mode":"callers","target":{…},"max_depth":2,"edges":[{"src_id","dst_id","type","depth"} …],"nodes":[…]}`. BFS backward over `edges.type='calls'`.

### `--callees SYMBOL` (what does X call)
Same shape as `--callers`, BFS forward.

### `--neighbors SYMBOL` (local graph context)
Undirected BFS over `calls`, `imports`, `contains`. Use for "give me the context around `UserService`".

### `--ast-call FUNC_NAME` (every call site of a name)
```
python3 scripts/query.py --db .kg/code_kg.sqlite --ast-call commit
```
Returns `{"mode":"ast-call","func_name":"commit","results":[{"id","file_id","file_path","kind":"Call","span_start","span_end","parent_id","extra"} …]}`. Matches the simple callee name only — not full qualified dotted calls.

### `--functions-without-docstring`
```
python3 scripts/query.py --db .kg/code_kg.sqlite --functions-without-docstring
```
Returns `{"mode":"functions-without-docstring","results":[{"ast_id","file_path","name","signature","span_start","span_end"} …]}`.

### `--try-except-without-logging`
Finds `ExceptHandler` AST nodes with no `has_logging_call=true` marker. Good for "find silent failures".

### `--explain-symbol SYMBOL`
Bundles target node + direct callers + direct callees + owning file + extracted docstring. Use when the user asks "explain this function / class".

### `--explain-file PATH`
Bundles file node + symbols defined + imports + top 10 symbols by outgoing call count. Use for "give me an overview of `src/foo.py`".

## Symbol resolution

`SYMBOL` accepts three forms, tried in order:
1. Full id: `sym:python:src/pkg/mod.py:ClassName.method` or `file:<path>`.
2. `<module_fragment>:<qualname>` (e.g. `user_service:login`).
3. Bare qualname (e.g. `login`).

If multiple nodes match, the response is `{"mode":"…","target":"…","ambiguous":true,"candidates":[…]}` with exit 0. Show the candidates to the user and re-query with a more specific form (prefer the full id from the first candidate's `id` field).

## Gotchas

- The DB is opened read-only via URI (`?mode=ro`). You cannot index from this skill — do not suggest modifications; route indexing requests to `code-graph-indexer`.
- FTS input is auto-escaped. Don't try to pass FTS5 operators (`AND`, `OR`, `*`) — they are stripped and treated as plain tokens.
- Semantic search loads the embedding model on first `--search` call (~100MB, slow first time). Subsequent invocations in the same Python process are faster, but each CLI call is a fresh process. If you need many semantic searches in a row, batch them in a single higher-level tool call or skip semantic mode.
- `--ast-call` matches by *simple name only*. `foo.bar.commit()` and `commit()` both match `--ast-call commit`. Use `--search` plus path filter logic in your own code if you need stricter matching.
- `extra` columns are JSON strings, not objects. Parse them client-side if you need nested fields.
- A `"warn: vec_embeddings table not present; FTS-only results"` on stderr just means the index was built with `--no-embeddings`. Results are still valid, just keyword-only.
- Span encoding: `line * 1_000_000 + col`. To decode: `line = span // 1_000_000; col = span % 1_000_000`.

## When NOT to use this skill

- The user wants to search a single file or a short path → use `grep` / `Grep` tool, not a full-graph search.
- The user wants to edit, refactor, or add logging to code → this skill only reads. Do the edit directly.
- The user asks to build/rebuild/refresh the index → use `code-graph-indexer`.
- No DB exists yet → tell the user to run `code-graph-indexer --full` first.

## Examples

See `references/EXAMPLES.md` for a concrete invocation + expected JSON for every mode.
