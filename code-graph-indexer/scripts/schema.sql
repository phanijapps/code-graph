-- code-graph schema. Single source of truth for both the indexer and query skills.
-- Apply with: conn.executescript(open('schema.sql').read())
-- Idempotent: uses CREATE ... IF NOT EXISTS throughout.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = OFF;

-- =============================================================================
-- Graph: nodes (files + symbols) and edges (relationships between them).
-- =============================================================================

CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,            -- file:<rel>  OR  sym:<lang>:<module>:<qualname>
    kind        TEXT NOT NULL,               -- file | module | function | method | class | variable
    path        TEXT,                        -- filesystem path (rel to workspace root)
    name        TEXT,                        -- basename for files; unqualified name for symbols
    language    TEXT,                        -- python | java | typescript | ...
    signature   TEXT,                        -- e.g. "def foo(x: int) -> str"
    span_start  INTEGER,                     -- line * 1_000_000 + col
    span_end    INTEGER,
    extra       TEXT                         -- JSON blob: docstring, decorators, return type, etc.
);

CREATE INDEX IF NOT EXISTS idx_nodes_kind     ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_path     ON nodes(path);
CREATE INDEX IF NOT EXISTS idx_nodes_name     ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_language ON nodes(language);

CREATE TABLE IF NOT EXISTS edges (
    src_id  TEXT NOT NULL,
    dst_id  TEXT NOT NULL,
    type    TEXT NOT NULL,                   -- defines | contains | calls | imports | inherits | references
    extra   TEXT,                            -- optional JSON (e.g. call-site span)
    PRIMARY KEY (src_id, dst_id, type)
);

CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);

-- =============================================================================
-- Full-text search over symbol/file names, signatures, docstrings.
-- =============================================================================

CREATE VIRTUAL TABLE IF NOT EXISTS fts_nodes USING fts5(
    node_id   UNINDEXED,
    name,
    path,
    signature,
    text_blob,
    tokenize = 'porter unicode61'
);

-- =============================================================================
-- AST: per-file AST nodes + inverted index on their attributes.
-- =============================================================================

CREATE TABLE IF NOT EXISTS ast_nodes (
    id          TEXT PRIMARY KEY,            -- ast:<rel_path>:<span_start>-<span_end>
    file_id     TEXT NOT NULL,               -- FK to nodes.id where kind='file'
    kind        TEXT NOT NULL,               -- FunctionDef | Call | Try | ExceptHandler | Import | ...
    span_start  INTEGER,
    span_end    INTEGER,
    parent_id   TEXT,                        -- nullable FK to ast_nodes.id
    extra       TEXT                         -- JSON: kind-specific fields
);

CREATE INDEX IF NOT EXISTS idx_ast_nodes_file   ON ast_nodes(file_id);
CREATE INDEX IF NOT EXISTS idx_ast_nodes_kind   ON ast_nodes(kind);
CREATE INDEX IF NOT EXISTS idx_ast_nodes_parent ON ast_nodes(parent_id);

CREATE TABLE IF NOT EXISTS ast_index (
    kind         TEXT NOT NULL,              -- AST node kind (mirrors ast_nodes.kind)
    attribute    TEXT NOT NULL,              -- func_name | has_docstring | callee_name | ...
    value        TEXT NOT NULL,              -- stringified value
    ast_node_id  TEXT NOT NULL               -- FK to ast_nodes.id
);

CREATE INDEX IF NOT EXISTS idx_ast_index_lookup ON ast_index(kind, attribute, value);
CREATE INDEX IF NOT EXISTS idx_ast_index_node   ON ast_index(ast_node_id);

-- =============================================================================
-- Bookkeeping: file-level hashes so incremental reindex can short-circuit.
-- =============================================================================

CREATE TABLE IF NOT EXISTS file_state (
    path         TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    indexed_at   TEXT NOT NULL,              -- ISO 8601 UTC
    language     TEXT
);

-- =============================================================================
-- Vector index is created at runtime by the indexer via sqlite-vec:
--   CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
--       node_id TEXT PRIMARY KEY, embedding FLOAT[384]
--   );
-- It is NOT defined here because the vec0 module must be loaded first.
-- =============================================================================
