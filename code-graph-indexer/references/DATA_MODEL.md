# Data model — `code_kg.sqlite`

The schema in `scripts/schema.sql` is the source of truth; this file explains
*semantics* (what each column means, what each ID looks like, what `extra`
JSON keys appear per AST kind). Read this when you are writing a query that
needs to decode a row — otherwise the shapes above are enough.

## Tables at a glance

| Table | Rows mean | Primary key |
|---|---|---|
| `nodes` | A file OR a symbol (function, class, method). | `id` |
| `edges` | A typed relationship between two `nodes`. | `(src_id, dst_id, type)` |
| `fts_nodes` | FTS5 virtual index over `name/path/signature/text_blob`. | `rowid` |
| `ast_nodes` | An AST construct captured during parse (FunctionDef, Call, Try, …). | `id` |
| `ast_index` | Inverted index of AST attributes for O(log n) lookup. | — |
| `vec_embeddings` | 384-dim float embedding per `nodes.id` (sqlite-vec). | `node_id` |
| `file_state` | Bookkeeping for incremental reindex (sha256 + timestamp). | `path` |

## ID conventions

- `file:<relative_path>` — e.g. `file:src/foo/bar.py`
- `sym:<language>:<module_path>:<qualified_name>` — e.g.
  `sym:python:src.foo.bar:MyClass.my_method`
- `ast:<relative_path>:<span_start>-<span_end>` — e.g.
  `ast:src/foo/bar.py:42000000-48000042`

Module path rules:
- **Python**: `src/foo/bar.py` → `src.foo.bar`
- **Java**: `src/com/example/Foo.java` → `src.com.example.Foo`
- **TypeScript**: `web/app.tsx` → `web.app` (both `.ts` and `.tsx` strip)

Import target IDs use a synthetic `__module__` qualname:
`sym:<lang>:<imported_module>:__module__`. These are targets of `imports`
edges; they may not correspond to real indexed files (third-party).

## Span encoding

Single integer: `line * 1_000_000 + col`.

- `line` is **1-indexed** (first line of the file = 1)
- `col` is **0-indexed** (first column = 0)
- Underlying tree-sitter returns 0-indexed `(row, col)`; the indexer adds 1
  to the row before encoding.

To decode:
```python
line = span // 1_000_000
col  = span %  1_000_000
```

This encoding lets you `ORDER BY span_start` to walk a file top-to-bottom,
and fits comfortably in a SQLite INTEGER.

## `nodes` columns

| Column | Notes |
|---|---|
| `id` | See ID conventions. |
| `kind` | `file` | `function` | `method` | `class` | `module` | `variable` |
| `path` | Workspace-relative path. Same for file and its symbols. |
| `name` | Unqualified: `bar.py` for a file, `my_method` for a method. |
| `language` | `python` | `java` | `typescript`. |
| `signature` | For symbols only. Best-effort stringified signature. |
| `span_start`, `span_end` | Encoded span of the whole def. |
| `extra` | JSON. See below. |

### `extra` JSON (per kind)

- **file**: `{"bytes": <int>}`
- **function / method**: `{"docstring": "<str>", "return_annotation": "<str>"}`
  - `docstring` is `""` when none.
  - For Python, docstring includes the surrounding quotes; the query skill
    strips them for display.
- **class**: `{"docstring": "<str>"}`

## `edges` types and semantics

| `type` | `src_id` → `dst_id` | Notes |
|---|---|---|
| `defines` | file → top-level symbol | |
| `contains` | parent symbol → nested symbol | class → method, class → inner class |
| `calls` | caller symbol → callee symbol | `dst_id` is best-effort; may not resolve to a real node |
| `imports` | file → `sym:<lang>:<module>:__module__` | `extra` carries `{"raw": "<import text>"}` |
| `inherits` | subclass symbol → base class symbol | `dst_id` may not resolve (external class) |
| `references` | (reserved; not emitted in v1) | |

**Unresolved callees.** The indexer extracts the simple callee name
(`logger.info(...)` → `info`) and synthesizes a `dst_id` in the same module
as the caller: `sym:python:<caller_module>:info`. The query skill tolerates
unresolved `dst_id`s by left-joining.

## `ast_nodes` — kinds and `extra` JSON

All rows have `id`, `file_id`, `kind`, `span_start`, `span_end`, `parent_id`.

| `kind` | `extra` keys |
|---|---|
| `FunctionDef` | `name`, `has_docstring` (bool), `has_return_annotation` (bool) |
| `Call` | `func_name` (simple), `callee_name` (full dotted) |
| `Try` | `{}` (marker; children carry the useful data) |
| `ExceptHandler` | `has_logging_call` (bool). `parent_id` = the enclosing `Try`. |
| `Import` | `raw` (source text), `module` (extracted module name) |

`func_name` for `Call` is the last `.`-separated component (e.g.
`self.repo.save` → `save`). `callee_name` is the full dotted expression.

## `ast_index` — inverted lookups

`(kind, attribute, value)` → `ast_node_id`. This is the join target for
structural queries.

| Kind | Attribute | Value | Use case |
|---|---|---|---|
| `FunctionDef` | `name` | unqualified name | "find all functions named `calculate`" |
| `FunctionDef` | `has_docstring` | `"true"` / `"false"` | "find functions without docstrings" |
| `FunctionDef` | `has_return_annotation` | `"true"` / `"false"` | "find untyped returns" |
| `Call` | `func_name` | simple name | "find all call sites of `save`" |
| `Call` | `callee_name` | full dotted expression | "find calls of `self.repo.save`" |
| `ExceptHandler` | `has_logging_call` | `"true"` / `"false"` | "find try/except without logging" |
| `Import` | `module` | module name | "find files that import `requests`" |

Booleans are stored as the literal strings `"true"` or `"false"`.

## `fts_nodes` (FTS5)

One row per `nodes` row.

| Column | Source |
|---|---|
| `node_id` | `nodes.id` (UNINDEXED; used for join back) |
| `name` | `nodes.name` |
| `path` | `nodes.path` |
| `signature` | `nodes.signature` |
| `text_blob` | `name + " " + signature + " " + docstring + " " + source_snippet[:2048]` |

Tokenizer: `porter unicode61`. Query via `MATCH`, e.g.
`SELECT node_id FROM fts_nodes WHERE fts_nodes MATCH 'calculate total'`.

## `vec_embeddings` (sqlite-vec, optional)

Created at runtime *only if* `sqlite-vec` loads successfully:

```sql
CREATE VIRTUAL TABLE vec_embeddings USING vec0(
    node_id TEXT PRIMARY KEY,
    embedding FLOAT[384]
);
```

One row per `nodes.id` that had non-empty text. Same `text_blob` used for
FTS is embedded (via `fastembed` with `BAAI/bge-small-en-v1.5` by default —
384-dim ONNX model, no PyTorch).

Query (KNN) via the vec0 MATCH syntax:
```sql
SELECT node_id, distance FROM vec_embeddings
WHERE embedding MATCH :qvec AND k = 25
ORDER BY distance;
```

## `file_state`

Used for incremental reindexing. The indexer computes `sha256` over file
bytes; if the stored hash matches, the file is skipped (no matter which
mode was requested — even `--full` skips unchanged files). To force a full
rewrite, delete the `.kg/code_kg.sqlite` file or the specific row.

| Column | Notes |
|---|---|
| `path` | Workspace-relative. PK. |
| `content_hash` | Lowercase hex sha256 of file bytes. |
| `indexed_at` | ISO 8601 UTC, e.g. `2026-04-16T12:34:56.789012+00:00`. |
| `language` | Detected language at index time. |

## Example rows

### A Python method
```
nodes:
  id=sym:python:src.billing.totals:Invoice.calculate_total
  kind=method  path=src/billing/totals.py  name=calculate_total
  language=python
  signature=def calculate_total(self, tax: float = 0.0) -> Decimal
  span_start=42000004 span_end=60000000
  extra={"docstring": "\"\"\"Return invoice total with tax.\"\"\"",
         "return_annotation": "Decimal"}

edges:
  ('sym:python:src.billing.totals:Invoice',
   'sym:python:src.billing.totals:Invoice.calculate_total',
   'contains', null)

ast_nodes:
  id=ast:src/billing/totals.py:42000004-60000000
  kind=FunctionDef
  extra={"name": "calculate_total",
         "has_docstring": true,
         "has_return_annotation": true}

ast_index:
  ('FunctionDef', 'name', 'calculate_total', <ast_id>)
  ('FunctionDef', 'has_docstring', 'true', <ast_id>)
  ('FunctionDef', 'has_return_annotation', 'true', <ast_id>)

fts_nodes:
  node_id=sym:python:src.billing.totals:Invoice.calculate_total
  text_blob='calculate_total def calculate_total(self, tax: float = 0.0) -> Decimal ...'
```

### A Java import
```
ast_nodes:
  id=ast:src/com/example/Foo.java:3000000-3000036
  kind=Import
  extra={"raw": "import java.util.List;", "module": "java.util.List"}

edges:
  ('file:src/com/example/Foo.java',
   'sym:java:java.util.List:__module__',
   'imports',
   '{"raw": "import java.util.List;"}')
```

### An `ExceptHandler` without logging
```
ast_nodes:
  id=ast:src/foo.py:10000000-12000000
  kind=ExceptHandler
  parent_id=ast:src/foo.py:9000000-12000000
  extra={"has_logging_call": false}

ast_index:
  ('ExceptHandler', 'has_logging_call', 'false', <ast_id>)
```

The query skill's `--try-except-without-logging` mode selects these rows
directly from `ast_index`.
