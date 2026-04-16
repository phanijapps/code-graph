#!/usr/bin/env python3
"""Read-only query CLI for the code-graph SQLite database.

Outputs JSON to stdout for every mode. Warnings/progress go to stderr.
DB is opened via ``file:...?mode=ro`` URI; this script never writes.
See ``SKILL.md`` and ``references/EXAMPLES.md`` for invocation examples.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

# --- FTS5 escaping -----------------------------------------------------------
_FTS_SPECIAL = re.compile(r"[^\w\s]", re.UNICODE)


def escape_fts(query: str) -> str:
    """Tokenize + double-quote each term so FTS5 MATCH treats them literally."""
    tokens = []
    for raw in query.split():
        cleaned = _FTS_SPECIAL.sub(" ", raw).strip()
        for word in cleaned.split():
            if word:
                tokens.append('"' + word.replace('"', '""') + '"')
    return " OR ".join(tokens) if tokens else '""'


# --- DB helpers --------------------------------------------------------------
def open_ro(db_path: str) -> sqlite3.Connection:
    """Open DB read-only via URI mode. Raises FileNotFoundError if missing."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None


NODE_COLS = "id, kind, path, name, language, signature, span_start, span_end, extra"


def row_to_node(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in ("id", "kind", "path", "name", "language",
                                "signature", "span_start", "span_end", "extra")}


def fetch_nodes_by_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = list(ids)
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), 500):
        piece = ids[i:i + 500]
        placeholders = ",".join("?" * len(piece))
        for row in conn.execute(
            f"SELECT {NODE_COLS} FROM nodes WHERE id IN ({placeholders})", piece,
        ):
            out[row["id"]] = row_to_node(row)
    return out


# --- Symbol resolution -------------------------------------------------------
@dataclass
class Resolution:
    node: dict[str, Any] | None
    candidates: list[dict[str, Any]]


def resolve_symbol(conn: sqlite3.Connection, symbol: str) -> Resolution:
    """Resolve user input to a ``nodes`` row.

    Accepts: full ``sym:<lang>:<module>:<qualname>``, ``file:<path>``,
    ``<module>:<qualname>``, or bare ``<qualname>``.
    Returns a Resolution with ``node`` set on unique match, otherwise
    ``candidates`` populated (possibly empty) for the caller to surface.
    """
    # (1) exact id match
    row = conn.execute(
        f"SELECT {NODE_COLS} FROM nodes WHERE id=?", (symbol,),
    ).fetchone()
    if row is not None:
        return Resolution(row_to_node(row), [])

    # (2) module:qualname form. Match by id tail (":<qualname>") because
    # nodes.name holds only the unqualified tail (e.g. "greet"), whereas
    # ids encode the full qualified path ("sym:python:mod:Class.greet").
    if ":" in symbol and not symbol.startswith(("sym:", "file:", "ast:")):
        module_frag, _, qualname = symbol.rpartition(":")
        tail = qualname.rpartition(".")[2] or qualname
        rows = conn.execute(
            f"SELECT {NODE_COLS} FROM nodes WHERE id LIKE ? "
            "AND (name = ? OR id LIKE ?) "
            "AND kind IN ('function','method','class','module') "
            "AND (path LIKE ? OR path LIKE ?)",
            (f"%:{qualname}",
             tail, f"%:{qualname}",
             f"%{module_frag}%", f"%{module_frag.replace('.', '/')}%"),
        ).fetchall()
        if len(rows) == 1:
            return Resolution(row_to_node(rows[0]), [])
        if len(rows) > 1:
            return Resolution(None, [row_to_node(r) for r in rows])

    # (3) bare qualname fallback — match on name OR id tail.
    tail = symbol.rpartition(".")[2] or symbol
    rows = conn.execute(
        f"SELECT {NODE_COLS} FROM nodes WHERE (name = ? OR id LIKE ?) "
        "AND kind IN ('function','method','class')",
        (tail, f"%:{symbol}"),
    ).fetchall()
    if len(rows) == 1:
        return Resolution(row_to_node(rows[0]), [])
    return Resolution(None, [row_to_node(r) for r in rows])


def ambiguous_response(mode: str, symbol: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "mode": mode,
        "target": symbol,
        "found": bool(candidates),
        "ambiguous": len(candidates) > 1,
        "candidates": candidates,
    }


# --- Search ------------------------------------------------------------------
def _fts_candidates(conn: sqlite3.Connection, text: str, limit: int) -> list[tuple[str, int]]:
    try:
        rows = conn.execute(
            "SELECT node_id, rank FROM fts_nodes WHERE fts_nodes MATCH ? "
            "ORDER BY rank LIMIT ?",
            (escape_fts(text), limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"warn: fts query failed: {exc}", file=sys.stderr)
        return []
    return [(r["node_id"], idx + 1) for idx, r in enumerate(rows)]


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec  # type: ignore
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as exc:
        print(f"warn: failed to load sqlite-vec: {exc}", file=sys.stderr)
        return False


def _embed_query(text: str, model_name: str) -> list[float] | None:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        print("warn: sentence-transformers not installed; skipping semantic search",
              file=sys.stderr)
        return None
    try:
        vec = SentenceTransformer(model_name).encode([text], normalize_embeddings=True)[0]
        return [float(x) for x in vec]
    except Exception as exc:
        print(f"warn: embedding failed ({exc}); skipping semantic search", file=sys.stderr)
        return None


def _vec_candidates(conn: sqlite3.Connection, vector: list[float], k: int) -> list[tuple[str, float]]:
    try:
        rows = conn.execute(
            "SELECT node_id, distance FROM vec_embeddings "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (json.dumps(vector), k),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"warn: vec query failed: {exc}", file=sys.stderr)
        return []
    return [(r["node_id"], float(r["distance"])) for r in rows]


def cmd_search(conn: sqlite3.Connection, text: str, top_k: int, embed_model: str) -> dict[str, Any]:
    pool = max(top_k * 4, 50)
    fts_rank: dict[str, int] = dict(_fts_candidates(conn, text, pool))

    vec_dist: dict[str, float] = {}
    if table_exists(conn, "vec_embeddings") and _load_vec_extension(conn):
        vector = _embed_query(text, embed_model)
        if vector is not None:
            for nid, dist in _vec_candidates(conn, vector, pool):
                vec_dist[nid] = dist
    elif not table_exists(conn, "vec_embeddings"):
        print("warn: vec_embeddings table not present; FTS-only results", file=sys.stderr)

    vec_order = sorted(vec_dist.items(), key=lambda kv: kv[1])
    vec_rank = {nid: i + 1 for i, (nid, _) in enumerate(vec_order)}

    scored: list[tuple[str, float]] = []
    for nid in set(fts_rank) | set(vec_dist):
        score = 0.0
        if nid in fts_rank:
            score += 1.0 / (60 + fts_rank[nid])
        if nid in vec_rank:
            score += 1.0 / (60 + vec_rank[nid])
        scored.append((nid, score))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    scored = scored[:top_k]

    nodes = fetch_nodes_by_ids(conn, [nid for nid, _ in scored])
    results = []
    for nid, score in scored:
        node = nodes.get(nid)
        if node is None:
            continue
        results.append({
            **{k: node[k] for k in ("id", "kind", "path", "name", "signature",
                                    "language", "span_start", "span_end")},
            "fts_rank": fts_rank.get(nid),
            "vec_distance": vec_dist.get(nid),
            "score": score,
        })
    return {"mode": "search", "query": text, "results": results}


# --- Graph BFS ---------------------------------------------------------------
def _bfs(conn: sqlite3.Connection, start_id: str, direction: str,
         edge_types: list[str], max_depth: int) -> tuple[list[dict[str, Any]], set[str]]:
    """BFS over ``edges``. direction: 'out' | 'in' | 'undirected'."""
    seen: set[str] = {start_id}
    frontier: deque[tuple[str, int]] = deque([(start_id, 0)])
    edges_out: list[dict[str, Any]] = []
    ph = ",".join("?" * len(edge_types))
    while frontier:
        node_id, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        hits: list[tuple[str, str, str]] = []
        if direction in ("out", "undirected"):
            for row in conn.execute(
                f"SELECT src_id, dst_id, type FROM edges "
                f"WHERE src_id=? AND type IN ({ph})", (node_id, *edge_types),
            ):
                hits.append((row["src_id"], row["dst_id"], row["type"]))
        if direction in ("in", "undirected"):
            for row in conn.execute(
                f"SELECT src_id, dst_id, type FROM edges "
                f"WHERE dst_id=? AND type IN ({ph})", (node_id, *edge_types),
            ):
                hits.append((row["src_id"], row["dst_id"], row["type"]))
        for src, dst, etype in hits:
            edges_out.append({"src_id": src, "dst_id": dst, "type": etype, "depth": depth + 1})
            other = dst if (src == node_id and direction != "in") else src
            if other not in seen:
                seen.add(other)
                frontier.append((other, depth + 1))
    return edges_out, seen


def _graph_mode(conn: sqlite3.Connection, mode: str, symbol: str,
                direction: str, edge_types: list[str], depth: int,
                depth_key: str) -> dict[str, Any]:
    res = resolve_symbol(conn, symbol)
    if res.node is None:
        return ambiguous_response(mode, symbol, res.candidates)
    edges, node_ids = _bfs(conn, res.node["id"], direction, edge_types, depth)
    nodes = fetch_nodes_by_ids(conn, node_ids)
    return {
        "mode": mode,
        "target": res.node,
        depth_key: depth,
        "edges": edges,
        "nodes": list(nodes.values()),
    }


def cmd_callers(conn, symbol, max_depth):
    return _graph_mode(conn, "callers", symbol, "in", ["calls"], max_depth, "max_depth")


def cmd_callees(conn, symbol, max_depth):
    return _graph_mode(conn, "callees", symbol, "out", ["calls"], max_depth, "max_depth")


def cmd_neighbors(conn, symbol, radius):
    return _graph_mode(conn, "neighbors", symbol, "undirected",
                       ["calls", "imports", "contains"], radius, "radius")


# --- AST queries -------------------------------------------------------------
def cmd_ast_call(conn: sqlite3.Connection, func_name: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT an.id, an.file_id, an.kind, an.span_start, an.span_end,
               an.parent_id, an.extra, n.path AS file_path
        FROM ast_index ai
        JOIN ast_nodes an ON ai.ast_node_id = an.id
        LEFT JOIN nodes n ON an.file_id = n.id
        WHERE ai.kind='Call' AND ai.attribute='func_name' AND ai.value=?
        ORDER BY an.file_id, an.span_start
        """,
        (func_name,),
    ).fetchall()
    return {"mode": "ast-call", "func_name": func_name,
            "results": [dict(r) for r in rows]}


def cmd_functions_without_docstring(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT an.id AS ast_id, an.file_id, an.span_start, an.span_end,
               nfile.path AS file_path,
               (SELECT value FROM ast_index
                 WHERE ast_node_id = an.id AND kind='FunctionDef'
                   AND attribute='has_docstring' LIMIT 1) AS has_docstring,
               (SELECT value FROM ast_index
                 WHERE ast_node_id = an.id AND kind='FunctionDef'
                   AND attribute='name' LIMIT 1) AS func_name,
               nsym.signature AS signature
        FROM ast_nodes an
        LEFT JOIN nodes nfile ON an.file_id = nfile.id
        LEFT JOIN nodes nsym ON nsym.kind IN ('function','method')
                             AND nsym.path = nfile.path
                             AND nsym.span_start = an.span_start
        WHERE an.kind='FunctionDef'
        """,
    ).fetchall()
    results = [
        {
            "ast_id": r["ast_id"],
            "file_path": r["file_path"],
            "name": r["func_name"],
            "signature": r["signature"],
            "span_start": r["span_start"],
            "span_end": r["span_end"],
        }
        for r in rows
        if (r["has_docstring"] or "").lower() != "true"
    ]
    return {"mode": "functions-without-docstring", "results": results}


def cmd_try_except_without_logging(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT an.id AS ast_id, an.file_id, an.span_start, an.span_end,
               n.path AS file_path
        FROM ast_nodes an
        LEFT JOIN nodes n ON an.file_id = n.id
        WHERE an.kind='ExceptHandler'
          AND NOT EXISTS (
              SELECT 1 FROM ast_index ai
              WHERE ai.ast_node_id = an.id AND ai.kind='ExceptHandler'
                AND ai.attribute='has_logging_call' AND ai.value='true'
          )
        ORDER BY an.file_id, an.span_start
        """,
    ).fetchall()
    return {"mode": "try-except-without-logging",
            "results": [dict(r) for r in rows]}


# --- Explain -----------------------------------------------------------------
def _direct_edges(conn: sqlite3.Connection, node_id: str, direction: str,
                  edge_type: str) -> list[dict[str, Any]]:
    col = "dst_id" if direction == "in" else "src_id"
    rows = conn.execute(
        f"SELECT src_id, dst_id, type, extra FROM edges WHERE {col}=? AND type=?",
        (node_id, edge_type),
    ).fetchall()
    return [dict(r) for r in rows]


def cmd_explain_symbol(conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    res = resolve_symbol(conn, symbol)
    if res.node is None:
        return ambiguous_response("explain-symbol", symbol, res.candidates)
    target = res.node
    caller_edges = _direct_edges(conn, target["id"], "in", "calls")
    callee_edges = _direct_edges(conn, target["id"], "out", "calls")
    neighbor_ids = ({e["src_id"] for e in caller_edges}
                    | {e["dst_id"] for e in callee_edges})
    neighbors = fetch_nodes_by_ids(conn, neighbor_ids)

    file_node = None
    if target.get("path"):
        row = conn.execute(
            f"SELECT {NODE_COLS} FROM nodes WHERE kind='file' AND path=? LIMIT 1",
            (target["path"],),
        ).fetchone()
        if row:
            file_node = row_to_node(row)

    docstring = None
    if target.get("extra"):
        try:
            extra_obj = json.loads(target["extra"])
            if isinstance(extra_obj, dict):
                docstring = extra_obj.get("docstring")
        except (ValueError, TypeError):
            pass

    return {
        "mode": "explain-symbol",
        "target": target,
        "file": file_node,
        "docstring": docstring,
        "callers": {
            "edges": caller_edges,
            "nodes": [neighbors[e["src_id"]] for e in caller_edges
                      if e["src_id"] in neighbors],
        },
        "callees": {
            "edges": callee_edges,
            "nodes": [neighbors[e["dst_id"]] for e in callee_edges
                      if e["dst_id"] in neighbors],
        },
    }


def cmd_explain_file(conn: sqlite3.Connection, path: str) -> dict[str, Any]:
    file_id = path if path.startswith("file:") else f"file:{path}"
    row = conn.execute(
        f"SELECT {NODE_COLS} FROM nodes WHERE id=?", (file_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"SELECT {NODE_COLS} FROM nodes WHERE kind='file' AND path=? LIMIT 1",
            (path,),
        ).fetchone()
    if row is None:
        return {"mode": "explain-file", "target": path, "found": False,
                "ambiguous": False, "candidates": []}
    file_node = row_to_node(row)

    sym_rows = conn.execute(
        f"SELECT {NODE_COLS} FROM nodes WHERE "
        "kind IN ('function','method','class','module','variable') "
        "AND path = ? ORDER BY span_start",
        (file_node["path"],),
    ).fetchall()
    symbols = [row_to_node(r) for r in sym_rows]

    imports = [dict(r) for r in conn.execute(
        "SELECT src_id, dst_id, type, extra FROM edges "
        "WHERE src_id=? AND type='imports'", (file_node["id"],),
    ).fetchall()]

    top_callers: list[dict[str, Any]] = []
    if symbols:
        ids = [s["id"] for s in symbols]
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT src_id, COUNT(*) AS n FROM edges "
            f"WHERE type='calls' AND src_id IN ({ph}) "
            f"GROUP BY src_id ORDER BY n DESC LIMIT 10", ids,
        ).fetchall()
        by_id = {s["id"]: s for s in symbols}
        for r in rows:
            n = by_id.get(r["src_id"])
            if n is not None:
                top_callers.append({"node": n, "outgoing_calls": r["n"]})

    return {
        "mode": "explain-file",
        "target": path,
        "found": True,
        "file": file_node,
        "symbols": symbols,
        "imports": imports,
        "top_callers": top_callers,
    }


# --- CLI ---------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="query.py",
                                description="Read-only queries over the code-graph SQLite DB.")
    p.add_argument("--db", required=True, help="Path to the SQLite DB file.")
    p.add_argument("--embed-model", default="sentence-transformers/all-MiniLM-L6-v2",
                   help="Embedding model for --search semantic fusion.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--search", metavar="TEXT")
    mode.add_argument("--callers", metavar="SYMBOL")
    mode.add_argument("--callees", metavar="SYMBOL")
    mode.add_argument("--neighbors", metavar="SYMBOL")
    mode.add_argument("--ast-call", metavar="FUNC_NAME", dest="ast_call")
    mode.add_argument("--functions-without-docstring", action="store_true",
                      dest="functions_without_docstring")
    mode.add_argument("--try-except-without-logging", action="store_true",
                      dest="try_except_without_logging")
    mode.add_argument("--explain-symbol", metavar="SYMBOL", dest="explain_symbol")
    mode.add_argument("--explain-file", metavar="PATH", dest="explain_file")
    p.add_argument("--top-k", type=int, default=25)
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--radius", type=int, default=2)
    return p


def dispatch(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    if args.search is not None:
        return cmd_search(conn, args.search, args.top_k, args.embed_model)
    if args.callers is not None:
        return cmd_callers(conn, args.callers, args.max_depth)
    if args.callees is not None:
        return cmd_callees(conn, args.callees, args.max_depth)
    if args.neighbors is not None:
        return cmd_neighbors(conn, args.neighbors, args.radius)
    if args.ast_call is not None:
        return cmd_ast_call(conn, args.ast_call)
    if args.functions_without_docstring:
        return cmd_functions_without_docstring(conn)
    if args.try_except_without_logging:
        return cmd_try_except_without_logging(conn)
    if args.explain_symbol is not None:
        return cmd_explain_symbol(conn, args.explain_symbol)
    if args.explain_file is not None:
        return cmd_explain_file(conn, args.explain_file)
    raise AssertionError("no mode selected")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(json.dumps({"mode": "error", "error": "invalid arguments"}))
        return 2
    try:
        conn = open_ro(args.db)
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(json.dumps({"mode": "error", "error": str(exc)}))
        return 2
    try:
        result = dispatch(conn, args)
    finally:
        conn.close()
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
