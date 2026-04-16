# code-graph

A local, workspace-aware code index you hand to your AI coding agent.

Two small Agent Skills that let Claude Code, Kiro, or any skill-aware agent **actually understand** the repo it's working in — across Python, Java, and TypeScript — without shipping your source off to a third-party service.

Everything lives in a single SQLite file.

---

## What it does

| You ask... | The agent does... |
|---|---|
| "Who calls `ingest_file`?" | BFS over a persisted call graph, returns every caller with depth |
| "Find code related to rate limiting" | FTS5 keyword + local vector search, fused with reciprocal-rank |
| "Which functions are missing docstrings?" | AST-level query, returns name + file + span |
| "Any try/except blocks that swallow errors silently?" | Finds `except` blocks with no logging call in scope |
| "Explain the `UserService` class" | Bundles the symbol, its callers, callees, file context, docstring |
| "Index this repo so queries are fast" | Walks the tree with tree-sitter, builds the graph, FTS, AST index, and embeddings |

No daemons. No cloud. No vector DB to manage. One SQLite file in `.kg/`.

---

## Architecture at a glance

```
┌──────────────────────┐         ┌──────────────────────┐
│ code-graph-indexer   │         │ code-graph-query     │
│ (Skill)              │         │ (Skill)              │
│                      │  writes │                      │
│  tree-sitter ──┐     │────────▶│  read-only           │
│  sqlite-vec   ─┤     │         │  FTS5 + vec search   │
│  embeddings  ──┘     │◀────────│  BFS call graph      │
│                      │  reads  │  AST queries         │
└──────────────────────┘         └──────────────────────┘
              │                             │
              └─────────── .kg/code_kg.sqlite ───────────
                  nodes / edges / fts_nodes /
                  ast_nodes / ast_index / vec_embeddings
```

The skills are intentionally split: the query skill has no parser dependencies and no embedding model, so agents can load it dozens of times per session cheaply. The indexer runs occasionally.

---

## Supported languages

- **Python** (`.py`)
- **Java** (`.java`)
- **TypeScript** (`.ts`) and **TSX** (`.tsx`)

Adding another language is ~50 lines of per-language extraction logic; everything else (schema, CLI, query surface) is language-agnostic.

---

## Quick start

```bash
# One-time setup per workspace
cd your-project
python3 -m venv .venv
source .venv/bin/activate
pip install -r /path/to/code-graph/code-graph-indexer/requirements.txt

# Build the index
python /path/to/code-graph/code-graph-indexer/scripts/index.py \
    --root . --db .kg/code_kg.sqlite --full

# Query it
python /path/to/code-graph/code-graph-query/scripts/query.py \
    --db .kg/code_kg.sqlite \
    --search "how requests are retried"
```

In practice you don't run these yourself — you install the two skills into your coding agent, and the agent picks the right one based on what you ask.

---

## Installing as Agent Skills

Each directory (`code-graph-indexer/`, `code-graph-query/`) is a self-contained [Agent Skill](https://agentskills.io/) with its own `SKILL.md`. Drop them into whatever skills directory your agent uses. Consult your agent's docs for the exact path:

- **Claude Code** — place under `~/.claude/skills/` (or a project-local `.claude/skills/`)
- **Kiro** — place under its skills directory per Kiro's config
- Others — follow the skill-discovery convention for that client

The indexer skill is invoked when the user asks to *index / refresh / rebuild*; the query skill is invoked when the user asks code-understanding questions.

---

## What gets indexed

For every source file, the indexer emits:

- **Symbols** — files, modules, classes, functions, methods — with fully-qualified IDs (`sym:python:src.foo:MyClass.method`).
- **Edges** — `defines`, `contains`, `calls`, `imports`, `inherits`.
- **AST nodes** — `FunctionDef`, `Call`, `Try`, `ExceptHandler`, `Import` — with spans and kind-specific attributes.
- **Full-text index** — names, signatures, docstrings, short code snippets.
- **Embeddings** (optional) — 384-dim vectors via `fastembed` with `BAAI/bge-small-en-v1.5` (ONNX, no PyTorch), stored in `sqlite-vec`.

Files that fail to parse are logged and skipped; the run never aborts on a single bad file.

---

## Query modes

All modes emit a single JSON document on stdout, designed to be easy for an agent to parse and narrate.

```bash
--search "concept"                       # FTS + optional semantic fusion
--callers SYMBOL [--max-depth N]         # who calls X
--callees SYMBOL [--max-depth N]         # what X calls
--neighbors SYMBOL [--radius N]          # undirected BFS
--ast-call FUNC_NAME                     # every call site by simple name
--functions-without-docstring            # AST structural query
--try-except-without-logging             # "silent failure" finder
--explain-symbol SYMBOL                  # narratable bundle for an agent
--explain-file PATH                      # file overview bundle
```

Symbol names are flexible: pass the full `sym:...` id, or `module:qualname`, or just `qualname` — the resolver picks a single match or returns candidates.

See `code-graph-query/references/EXAMPLES.md` for example invocations and their JSON shapes.

---

## Design choices worth calling out

- **Single-file SQLite.** Portable, easy to ship, easy to inspect with `sqlite3`. No separate service to babysit.
- **Tree-sitter for all languages.** One parser library, one API surface, grammars that are production-grade (used by GitHub, Zed, Neovim). Python/Java/TypeScript today; adding Go or Rust is small.
- **Graceful degradation.** If `sqlite-vec` fails to load on your platform, you still get FTS + graph + AST. If `fastembed` isn't installed, you still get everything except semantic search.
- **Idempotent by default.** Incremental modes skip files whose sha256 hash hasn't changed. `--full` forces a rebuild and sweeps any DB rows for files that no longer exist on disk.
- **Two skills, not one.** The query skill has no parsing or embedding deps — agents can load it cheaply many times per session. The indexer runs occasionally.

---

## Repository layout

```
code-graph/
├── README.md                          # you are here
├── CLAUDE.md                          # agent guidelines for maintainers
├── code-graph-indexer/
│   ├── SKILL.md                       # skill definition
│   ├── requirements.txt
│   ├── scripts/
│   │   ├── index.py                   # indexer CLI
│   │   └── schema.sql                 # single source of truth
│   ├── references/DATA_MODEL.md
│   └── evals/                         # trigger evals + language fixtures
└── code-graph-query/
    ├── SKILL.md
    ├── requirements.txt
    ├── scripts/query.py               # read-only query CLI
    ├── references/EXAMPLES.md
    └── evals/
```

---

## Requirements

- Python 3.12+
- SQLite with extension loading compiled in (pyenv, python.org installer, or conda ship with this; some distro-packaged Pythons don't)
- `sqlite-vec` wheel for your platform (linux-x86_64, linux-aarch64, macOS, win64 are pre-built)
- Optional: `fastembed` for semantic search (ONNX runtime, ~60 MB install — no PyTorch)

Pinned versions live in each skill's `requirements.txt`.

---

## Status

v1 is ship-ready: Python / Java / TypeScript indexing, all nine query modes verified end-to-end, two subagents built it in parallel with a code-review gate. Future versions may add Go, Rust, a richer AST pattern DSL, and a long-running daemon mode — see the design notes for details.

---

## License

MIT.
