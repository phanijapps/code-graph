# code-graph-query — Example invocations

Every mode below is shown as a shell command plus a trimmed JSON response. Real responses may include additional optional keys; always parse by key name, never by position.

Assume the index lives at `.kg/code_kg.sqlite` and was built by `code-graph-indexer` on a Python project with a `src/ingest/pipeline.py`.

---

## 1. `--search` (hybrid keyword + semantic)

```bash
python3 scripts/query.py --db .kg/code_kg.sqlite --search "rate limit retry" --top-k 5
```

```json
{
  "mode": "search",
  "query": "rate limit retry",
  "results": [
    {
      "id": "sym:python:src/http/client.py:RetryClient.send",
      "kind": "method",
      "path": "src/http/client.py",
      "name": "send",
      "signature": "def send(self, request: Request) -> Response",
      "language": "python",
      "span_start": 42000012,
      "span_end": 87000004,
      "fts_rank": 1,
      "vec_distance": 0.312,
      "score": 0.0326
    },
    {
      "id": "sym:python:src/http/backoff.py:exponential_backoff",
      "kind": "function",
      "path": "src/http/backoff.py",
      "name": "exponential_backoff",
      "signature": "def exponential_backoff(attempt: int) -> float",
      "language": "python",
      "span_start": 11000000,
      "span_end": 24000004,
      "fts_rank": 3,
      "vec_distance": 0.488,
      "score": 0.0322
    }
  ]
}
```

Stderr (when vectors are missing): `warn: vec_embeddings table not present; FTS-only results`. In that case `vec_distance` is `null` for all rows.

---

## 2. `--callers SYMBOL`

```bash
python3 scripts/query.py --db .kg/code_kg.sqlite --callers ingest_file --max-depth 2
```

```json
{
  "mode": "callers",
  "target": {
    "id": "sym:python:src/ingest/pipeline.py:ingest_file",
    "kind": "function",
    "path": "src/ingest/pipeline.py",
    "name": "ingest_file",
    "language": "python",
    "signature": "def ingest_file(path: str) -> None",
    "span_start": 52000000,
    "span_end": 98000004,
    "extra": "{\"docstring\": \"Index a single file.\"}"
  },
  "max_depth": 2,
  "edges": [
    {"src_id": "sym:python:src/ingest/runner.py:run", "dst_id": "sym:python:src/ingest/pipeline.py:ingest_file", "type": "calls", "depth": 1},
    {"src_id": "sym:python:src/cli.py:main", "dst_id": "sym:python:src/ingest/runner.py:run", "type": "calls", "depth": 2}
  ],
  "nodes": [
    {"id": "sym:python:src/ingest/runner.py:run", "kind": "function", "name": "run", "path": "src/ingest/runner.py", "language": "python", "signature": "def run(paths: list[str]) -> int", "span_start": 3000000, "span_end": 41000004, "extra": null},
    {"id": "sym:python:src/cli.py:main", "kind": "function", "name": "main", "path": "src/cli.py", "language": "python", "signature": "def main() -> None", "span_start": 1000000, "span_end": 19000004, "extra": null}
  ]
}
```

---

## 3. `--callees SYMBOL`

```bash
python3 scripts/query.py --db .kg/code_kg.sqlite --callees UserService.login
```

Same shape as `--callers`, except `edges` go outward (`src_id` is the target).

---

## 4. `--neighbors SYMBOL`

```bash
python3 scripts/query.py --db .kg/code_kg.sqlite --neighbors ingest_file --radius 1
```

```json
{
  "mode": "neighbors",
  "target": {"id": "sym:python:src/ingest/pipeline.py:ingest_file", "kind": "function", "name": "ingest_file", "path": "src/ingest/pipeline.py", "language": "python", "signature": "def ingest_file(path: str) -> None", "span_start": 52000000, "span_end": 98000004, "extra": null},
  "radius": 1,
  "edges": [
    {"src_id": "file:src/ingest/pipeline.py", "dst_id": "sym:python:src/ingest/pipeline.py:ingest_file", "type": "contains", "depth": 1},
    {"src_id": "sym:python:src/ingest/pipeline.py:ingest_file", "dst_id": "sym:python:src/ingest/parser.py:parse", "type": "calls", "depth": 1}
  ],
  "nodes": [
    {"id": "file:src/ingest/pipeline.py", "kind": "file", "name": "pipeline.py", "path": "src/ingest/pipeline.py", "language": "python", "signature": null, "span_start": null, "span_end": null, "extra": null},
    {"id": "sym:python:src/ingest/parser.py:parse", "kind": "function", "name": "parse", "path": "src/ingest/parser.py", "language": "python", "signature": "def parse(src: str)", "span_start": 4000000, "span_end": 33000004, "extra": null}
  ]
}
```

---

## 5. `--ast-call FUNC_NAME`

```bash
python3 scripts/query.py --db .kg/code_kg.sqlite --ast-call commit
```

```json
{
  "mode": "ast-call",
  "func_name": "commit",
  "results": [
    {"id": "ast:src/db.py:102000004-102000012", "file_id": "file:src/db.py", "kind": "Call", "span_start": 102000004, "span_end": 102000012, "parent_id": "ast:src/db.py:99000000-110000004", "extra": "{\"func_name\": \"commit\"}", "file_path": "src/db.py"},
    {"id": "ast:src/ingest/pipeline.py:200000008-200000016", "file_id": "file:src/ingest/pipeline.py", "kind": "Call", "span_start": 200000008, "span_end": 200000016, "parent_id": null, "extra": "{\"func_name\": \"commit\"}", "file_path": "src/ingest/pipeline.py"}
  ]
}
```

---

## 6. `--functions-without-docstring`

```bash
python3 scripts/query.py --db .kg/code_kg.sqlite --functions-without-docstring
```

```json
{
  "mode": "functions-without-docstring",
  "results": [
    {"ast_id": "ast:src/util/math.py:5000000-18000004", "file_path": "src/util/math.py", "name": "clamp", "signature": "def clamp(x: float, lo: float, hi: float) -> float", "span_start": 5000000, "span_end": 18000004},
    {"ast_id": "ast:src/cli.py:1000000-19000004", "file_path": "src/cli.py", "name": "main", "signature": "def main() -> None", "span_start": 1000000, "span_end": 19000004}
  ]
}
```

---

## 7. `--try-except-without-logging`

```bash
python3 scripts/query.py --db .kg/code_kg.sqlite --try-except-without-logging
```

```json
{
  "mode": "try-except-without-logging",
  "results": [
    {"ast_id": "ast:src/ingest/pipeline.py:70000004-82000004", "file_id": "file:src/ingest/pipeline.py", "span_start": 70000004, "span_end": 82000004, "file_path": "src/ingest/pipeline.py"}
  ]
}
```

---

## 8. `--explain-symbol SYMBOL`

```bash
python3 scripts/query.py --db .kg/code_kg.sqlite --explain-symbol UserService.login
```

```json
{
  "mode": "explain-symbol",
  "target": {"id": "sym:python:src/auth/user_service.py:UserService.login", "kind": "method", "path": "src/auth/user_service.py", "name": "login", "language": "python", "signature": "def login(self, email: str, password: str) -> Session", "span_start": 88000004, "span_end": 140000004, "extra": "{\"docstring\": \"Authenticate a user and return a session.\"}"},
  "file": {"id": "file:src/auth/user_service.py", "kind": "file", "name": "user_service.py", "path": "src/auth/user_service.py", "language": "python", "signature": null, "span_start": null, "span_end": null, "extra": null},
  "docstring": "Authenticate a user and return a session.",
  "callers": {
    "edges": [{"src_id": "sym:python:src/api/auth.py:AuthRouter.post_login", "dst_id": "sym:python:src/auth/user_service.py:UserService.login", "type": "calls", "extra": null}],
    "nodes": [{"id": "sym:python:src/api/auth.py:AuthRouter.post_login", "kind": "method", "name": "post_login", "path": "src/api/auth.py", "language": "python", "signature": "def post_login(self, body: LoginBody) -> Response", "span_start": 44000004, "span_end": 76000004, "extra": null}]
  },
  "callees": {
    "edges": [{"src_id": "sym:python:src/auth/user_service.py:UserService.login", "dst_id": "sym:python:src/auth/hashing.py:verify_password", "type": "calls", "extra": null}],
    "nodes": [{"id": "sym:python:src/auth/hashing.py:verify_password", "kind": "function", "name": "verify_password", "path": "src/auth/hashing.py", "language": "python", "signature": "def verify_password(plain: str, hashed: str) -> bool", "span_start": 3000000, "span_end": 21000004, "extra": null}]
  }
}
```

---

## 9. `--explain-file PATH`

```bash
python3 scripts/query.py --db .kg/code_kg.sqlite --explain-file src/ingest/pipeline.py
```

```json
{
  "mode": "explain-file",
  "target": "src/ingest/pipeline.py",
  "found": true,
  "file": {"id": "file:src/ingest/pipeline.py", "kind": "file", "name": "pipeline.py", "path": "src/ingest/pipeline.py", "language": "python", "signature": null, "span_start": null, "span_end": null, "extra": null},
  "symbols": [
    {"id": "sym:python:src/ingest/pipeline.py:Pipeline", "kind": "class", "name": "Pipeline", "path": "src/ingest/pipeline.py", "language": "python", "signature": "class Pipeline", "span_start": 10000000, "span_end": 50000000, "extra": null},
    {"id": "sym:python:src/ingest/pipeline.py:ingest_file", "kind": "function", "name": "ingest_file", "path": "src/ingest/pipeline.py", "language": "python", "signature": "def ingest_file(path: str) -> None", "span_start": 52000000, "span_end": 98000004, "extra": null}
  ],
  "imports": [
    {"src_id": "file:src/ingest/pipeline.py", "dst_id": "file:src/ingest/parser.py", "type": "imports", "extra": null}
  ],
  "top_callers": [
    {"node": {"id": "sym:python:src/ingest/pipeline.py:Pipeline.run", "kind": "method", "name": "run", "path": "src/ingest/pipeline.py", "language": "python", "signature": "def run(self) -> int", "span_start": 20000004, "span_end": 48000004, "extra": null}, "outgoing_calls": 7}
  ]
}
```

---

## Ambiguous symbol resolution

When a name matches multiple nodes, the response carries `ambiguous: true` and a `candidates` list (exit code is still 0). Pick the correct `id` from the list and retry with the full id:

```json
{
  "mode": "callers",
  "target": "login",
  "ambiguous": true,
  "candidates": [
    {"id": "sym:python:src/auth/user_service.py:UserService.login", "kind": "method", "name": "login", "path": "src/auth/user_service.py", "language": "python", "signature": "def login(self, email: str, password: str) -> Session", "span_start": 88000004, "span_end": 140000004, "extra": null},
    {"id": "sym:python:src/admin/admin_service.py:AdminService.login", "kind": "method", "name": "login", "path": "src/admin/admin_service.py", "language": "python", "signature": "def login(self, token: str) -> Session", "span_start": 40000004, "span_end": 71000004, "extra": null}
  ]
}
```

Retry with `--callers sym:python:src/auth/user_service.py:UserService.login`.

---

## Error JSON

On unreadable DB or malformed args (exit 2):

```json
{"mode": "error", "error": "database not found: .kg/code_kg.sqlite"}
```
