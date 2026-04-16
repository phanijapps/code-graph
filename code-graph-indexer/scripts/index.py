#!/usr/bin/env python3
"""code-graph-indexer: build / refresh a SQLite code graph using tree-sitter.

Single JSON summary on stdout; progress / warnings on stderr. See SKILL.md.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
EMBED_DIM = 384
BATCH_SIZE = 32
SNIPPET_MAX = 2048
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".kg"}
LANG_BY_EXT = {".py": "python", ".java": "java", ".ts": "typescript", ".tsx": "typescript"}
LOGGING_FUNC_RE = re.compile(r"^log(ger|ging)?$", re.IGNORECASE)
LOGGING_METHODS = {"info", "warning", "warn", "error", "debug", "exception", "critical"}

# Per-language AST config. Keeps the per-language code path tiny.
LANG_CONFIG: dict[str, dict[str, Any]] = {
    "python": {
        "call": "call",
        "try": "try_statement",
        "handler": {"except_clause"},
        "import": {"import_statement", "import_from_statement"},
        "class": "class_definition",
        "func": {"function_definition"},
        "method": set(),
        "callee_field": "function",
        "ext_strip": (".py",),
    },
    "java": {
        "call": "method_invocation",
        "try": "try_statement",
        "handler": {"catch_clause"},
        "import": {"import_declaration"},
        "class": "class_declaration",
        "func": {"method_declaration"},
        "method": {"method_declaration"},
        "callee_field": "name",
        "ext_strip": (".java",),
    },
    "typescript": {
        "call": "call_expression",
        "try": "try_statement",
        "handler": {"catch_clause"},
        "import": {"import_statement"},
        "class": "class_declaration",
        "func": {"function_declaration", "generator_function_declaration"},
        "method": {"method_definition"},
        "callee_field": "function",
        "ext_strip": (".tsx", ".ts"),
    },
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def encode_span(point: tuple[int, int]) -> int:
    # tree-sitter uses 0-indexed (row, col); we store 1-indexed line.
    return (point[0] + 1) * 1_000_000 + point[1]


# --------------------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build/refresh a code-graph SQLite DB.")
    p.add_argument("--root", default=".")
    p.add_argument("--db", default=".kg/code_kg.sqlite")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true")
    mode.add_argument("--paths", nargs="+")
    mode.add_argument("--changed-since", metavar="REF")
    mode.add_argument("--only-staged", action="store_true")
    p.add_argument("--include-languages", default="python,java,typescript")
    p.add_argument("--include-paths", default="")
    p.add_argument("--max-file-size-bytes", type=int, default=2_000_000)
    p.add_argument("--embed-model", default="sentence-transformers/all-MiniLM-L6-v2")
    p.add_argument("--no-embeddings", action="store_true")
    args = p.parse_args(argv)
    if not (args.full or args.paths or args.changed_since or args.only_staged):
        args.full = True
    return args


# --------------------------------------------------------------------------- DB

def open_db(db_path: Path) -> tuple[sqlite3.Connection, bool]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    vec_loaded = False
    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.NotSupportedError, sqlite3.OperationalError) as e:
        log(f"[warn] sqlite3 extension loading disabled: {e}; continuing FTS-only.")
    else:
        try:
            import sqlite_vec  # type: ignore
            sqlite_vec.load(conn)
            vec_loaded = True
        except Exception as e:
            log(f"[warn] could not load sqlite-vec ({e}); continuing FTS-only.")
        finally:
            with contextlib.suppress(Exception):
                conn.enable_load_extension(False)

    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if vec_loaded:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings "
                f"USING vec0(node_id TEXT PRIMARY KEY, embedding FLOAT[{EMBED_DIM}]);"
            )
        except sqlite3.OperationalError as e:
            log(f"[warn] vec_embeddings create failed ({e}); continuing FTS-only.")
            vec_loaded = False
    conn.commit()
    return conn, vec_loaded


# --------------------------------------------------------------------------- parsers (memoized)

_PARSERS: dict[str, Any] = {}


def get_parser(key: str):
    if key in _PARSERS:
        return _PARSERS[key]
    from tree_sitter import Language, Parser  # type: ignore
    if key == "python":
        import tree_sitter_python as m  # type: ignore
        lang = Language(m.language())
    elif key == "java":
        import tree_sitter_java as m  # type: ignore
        lang = Language(m.language())
    elif key == "typescript":
        import tree_sitter_typescript as m  # type: ignore
        lang = Language(m.language_typescript())
    elif key == "tsx":
        import tree_sitter_typescript as m  # type: ignore
        lang = Language(m.language_tsx())
    else:
        raise ValueError(key)
    _PARSERS[key] = Parser(lang)
    return _PARSERS[key]


# --------------------------------------------------------------------------- discovery

def walk_workspace(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            yield Path(dirpath) / fn


def git_output(cmd: list[str], cwd: Path) -> list[str]:
    try:
        out = subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        log(f"[warn] git failed ({' '.join(cmd)}): {e}")
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def discover_files(args: argparse.Namespace, root: Path) -> list[Path]:
    if args.paths:
        return [Path(p) for p in args.paths]
    if args.only_staged:
        return [root / r for r in git_output(["git", "diff", "--cached", "--name-only"], root)]
    if args.changed_since:
        return [root / r for r in git_output(
            ["git", "diff", "--name-only", f"{args.changed_since}...HEAD"], root)]
    return list(walk_workspace(root))


def normalize_rel(path: Path, root: Path) -> Optional[str]:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return str(rel).replace(os.sep, "/")


def module_path_for(rel: str, lang: str) -> str:
    stem = rel
    for suf in LANG_CONFIG[lang]["ext_strip"]:
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break
    return stem.replace("/", ".")


# --------------------------------------------------------------------------- extraction

@dataclass
class FileResult:
    nodes: list[tuple] = field(default_factory=list)
    edges: list[tuple] = field(default_factory=list)
    ast_nodes: list[tuple] = field(default_factory=list)
    ast_index: list[tuple] = field(default_factory=list)
    fts: list[tuple] = field(default_factory=list)
    embed_pairs: list[tuple[str, str]] = field(default_factory=list)


def text_of(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _field(node, name: str):
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


def _callee_name(call, src: bytes, lang: str) -> tuple[str, str]:
    fn = _field(call, LANG_CONFIG[lang]["callee_field"])
    if fn is None and lang == "java":
        # fall back to first identifier child
        for c in call.children:
            if c.type in ("identifier", "field_access", "scoped_identifier"):
                fn = c
                break
    if fn is None:
        return "", ""
    full = text_of(fn, src).strip()
    simple = full.split(".")[-1].split("(")[0].split("<")[0].strip()
    return simple, full


def _walk_collect(node, types: set[str], out: list) -> None:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in types:
            out.append(n)
        for c in n.children:
            stack.append(c)


def _has_logging_call(scope, src: bytes, lang: str) -> bool:
    calls: list = []
    _walk_collect(scope, {LANG_CONFIG[lang]["call"]}, calls)
    for c in calls:
        _, full = _callee_name(c, src, lang)
        if not full:
            continue
        parts = full.split(".")
        head, tail = parts[0], parts[-1]
        if LOGGING_FUNC_RE.match(head):
            return True
        if tail.lower() in LOGGING_METHODS and head.lower() in {"log", "logger", "logging"}:
            return True
    return False


def _docstring_py(body, src: bytes) -> str:
    if body is None:
        return ""
    for c in body.children:
        if c.type == "expression_statement":
            for gc in c.children:
                if gc.type == "string":
                    return text_of(gc, src).strip()
            return ""
        if c.type.endswith("comment"):
            continue
        return ""
    return ""


def _preceding_jsdoc(node, src: bytes) -> str:
    prev = node.prev_sibling
    while prev is not None and prev.type.endswith("comment"):
        txt = text_of(prev, src)
        if txt.startswith("/**"):
            return txt.strip()
        prev = prev.prev_sibling
    return ""


def _emit_fts_embed(res: FileResult, nid: str, name: str, rel: str,
                    sig: str, doc: str, snippet: str) -> None:
    blob = " ".join(x for x in (name, sig, doc, snippet[:SNIPPET_MAX]) if x).strip()
    res.fts.append((nid, name or "", rel, sig or "", blob))
    res.embed_pairs.append((nid, blob))


def _emit_function_ast(res: FileResult, file_id: str, rel: str, node,
                       name: str, has_doc: bool, has_return: bool) -> None:
    aid = f"ast:{rel}:{encode_span(node.start_point)}-{encode_span(node.end_point)}"
    res.ast_nodes.append((aid, file_id, "FunctionDef",
                          encode_span(node.start_point), encode_span(node.end_point),
                          None, json.dumps({"name": name, "has_docstring": has_doc,
                                            "has_return_annotation": has_return})))
    res.ast_index.append(("FunctionDef", "name", name, aid))
    res.ast_index.append(("FunctionDef", "has_docstring", "true" if has_doc else "false", aid))
    res.ast_index.append(("FunctionDef", "has_return_annotation",
                          "true" if has_return else "false", aid))


def _emit_calls(res: FileResult, file_id: str, rel: str, scope, src: bytes,
                lang: str, caller_id: str) -> None:
    calls: list = []
    _walk_collect(scope, {LANG_CONFIG[lang]["call"]}, calls)
    mod = module_path_for(rel, lang)
    for c in calls:
        simple, full = _callee_name(c, src, lang)
        aid = f"ast:{rel}:{encode_span(c.start_point)}-{encode_span(c.end_point)}"
        res.ast_nodes.append((aid, file_id, "Call",
                              encode_span(c.start_point), encode_span(c.end_point),
                              None, json.dumps({"func_name": simple, "callee_name": full})))
        if simple:
            res.ast_index.append(("Call", "func_name", simple, aid))
            res.edges.append((caller_id, f"sym:{lang}:{mod}:{simple}", "calls",
                              json.dumps({"callsite": encode_span(c.start_point), "raw": full})))
        if full:
            res.ast_index.append(("Call", "callee_name", full, aid))


def _emit_try(res: FileResult, file_id: str, rel: str, scope, src: bytes, lang: str) -> None:
    cfg = LANG_CONFIG[lang]
    tries: list = []
    _walk_collect(scope, {cfg["try"]}, tries)
    for t in tries:
        tid = f"ast:{rel}:{encode_span(t.start_point)}-{encode_span(t.end_point)}"
        res.ast_nodes.append((tid, file_id, "Try",
                              encode_span(t.start_point), encode_span(t.end_point),
                              None, json.dumps({})))
        for h in t.children:
            if h.type in cfg["handler"]:
                hid = f"ast:{rel}:{encode_span(h.start_point)}-{encode_span(h.end_point)}"
                has_log = _has_logging_call(h, src, lang)
                res.ast_nodes.append((hid, file_id, "ExceptHandler",
                                      encode_span(h.start_point), encode_span(h.end_point),
                                      tid, json.dumps({"has_logging_call": has_log})))
                res.ast_index.append(("ExceptHandler", "has_logging_call",
                                      "true" if has_log else "false", hid))


def _emit_import(res: FileResult, file_id: str, rel: str, node, src: bytes, lang: str) -> None:
    raw = text_of(node, src).strip()
    mod = ""
    if lang == "python":
        if node.type == "import_from_statement":
            mf = _field(node, "module_name")
            if mf is not None:
                mod = text_of(mf, src).strip()
        else:
            for c in node.children:
                if c.type in ("dotted_name", "aliased_import", "identifier"):
                    mod = text_of(c, src).strip()
                    break
    elif lang == "java":
        parts: list[str] = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                parts.append(text_of(n, src))
            for c in n.children:
                stack.append(c)
        mod = ".".join(parts) if parts else raw
    else:  # typescript
        sn = _field(node, "source")
        if sn is not None:
            mod = text_of(sn, src).strip().strip("'\"")
    aid = f"ast:{rel}:{encode_span(node.start_point)}-{encode_span(node.end_point)}"
    res.ast_nodes.append((aid, file_id, "Import",
                          encode_span(node.start_point), encode_span(node.end_point),
                          None, json.dumps({"raw": raw, "module": mod})))
    if mod:
        res.ast_index.append(("Import", "module", mod, aid))
        res.edges.append((file_id, f"sym:{lang}:{mod}:__module__", "imports",
                          json.dumps({"raw": raw})))


def _docstring_for(lang: str, child, body, src: bytes) -> str:
    if lang == "python":
        return _docstring_py(body, src)
    return _preceding_jsdoc(child, src)


def extract(tree, src: bytes, rel: str, file_id: str, lang: str) -> FileResult:
    """Unified extractor driven by LANG_CONFIG."""
    cfg = LANG_CONFIG[lang]
    res = FileResult()
    module = module_path_for(rel, lang)

    def sid(qual: str) -> str:
        return f"sym:{lang}:{module}:{qual}"

    def emit_inherits(sid_val: str, super_node, types: set[str]) -> None:
        if super_node is None:
            return
        idents: list = []
        _walk_collect(super_node, types, idents)
        for ident in idents:
            bname = text_of(ident, src).strip()
            if bname:
                res.edges.append((sid_val, sid(bname), "inherits", None))

    def visit(node, parent_qual: str, parent_sid: Optional[str]):
        for child in node.children:
            t = child.type
            if t == cfg["class"]:
                name_n = _field(child, "name")
                body_n = _field(child, "body")
                name = text_of(name_n, src) if name_n else "<anon>"
                qual = f"{parent_qual}.{name}" if parent_qual else name
                cid = sid(qual)
                signature = f"class {name}"
                doc = _docstring_for(lang, child, body_n, src)
                res.nodes.append((cid, "class", rel, name, lang, signature,
                                  encode_span(child.start_point), encode_span(child.end_point),
                                  json.dumps({"docstring": doc})))
                res.edges.append((parent_sid or file_id, cid,
                                  "contains" if parent_sid else "defines", None))
                _emit_fts_embed(res, cid, name, rel, signature, doc, text_of(child, src))
                if lang == "python":
                    emit_inherits(cid, _field(child, "superclasses"),
                                  {"identifier", "attribute", "dotted_name"})
                elif lang == "java":
                    emit_inherits(cid, _field(child, "superclass"),
                                  {"type_identifier", "identifier"})
                else:  # ts
                    for heritage_child in child.children:
                        if heritage_child.type == "class_heritage":
                            emit_inherits(cid, heritage_child,
                                          {"identifier", "type_identifier"})
                if body_n is not None:
                    visit(body_n, qual, cid)

            elif t in cfg["func"] or t in cfg["method"]:
                name_n = _field(child, "name")
                params_n = _field(child, "parameters")
                body_n = _field(child, "body")
                return_n = _field(child, "return_type") or _field(child, "type")
                name = text_of(name_n, src) if name_n else "<anon>"
                qual = f"{parent_qual}.{name}" if parent_qual else name
                fid = sid(qual)
                params = text_of(params_n, src) if params_n else "()"
                rtype = text_of(return_n, src) if return_n else ""
                if lang == "python":
                    signature = f"def {name}{params}" + (f" -> {rtype}" if rtype else "")
                elif lang == "java":
                    signature = f"{rtype or 'void'} {name}{params}"
                else:
                    signature = f"{name}{params}" + (f" {rtype}" if rtype else "")
                doc = _docstring_for(lang, child, body_n, src)
                has_return = bool(rtype) and (lang != "java" or rtype != "void")
                kind = "method" if parent_sid else "function"
                res.nodes.append((fid, kind, rel, name, lang, signature,
                                  encode_span(child.start_point), encode_span(child.end_point),
                                  json.dumps({"docstring": doc, "return_annotation": rtype})))
                res.edges.append((parent_sid or file_id, fid,
                                  "contains" if parent_sid else "defines", None))
                _emit_fts_embed(res, fid, name, rel, signature, doc, text_of(child, src))
                _emit_function_ast(res, file_id, rel, child, name, bool(doc), has_return)
                scope = body_n or child
                _emit_calls(res, file_id, rel, scope, src, lang, fid)
                _emit_try(res, file_id, rel, scope, src, lang)
                if body_n is not None:
                    visit(body_n, qual, fid)

            elif t in cfg["import"]:
                _emit_import(res, file_id, rel, child, src, lang)

            else:
                if parent_sid is None and child.type == cfg["try"]:
                    _emit_try(res, file_id, rel, child, src, lang)
                if child.child_count:
                    visit(child, parent_qual, parent_sid)

    visit(tree.root_node, "", None)
    return res


# --------------------------------------------------------------------------- per-file driver

def process_file(_path: Path, rel: str, lang: str, src_bytes: bytes) -> FileResult:
    parser_key = "tsx" if (lang == "typescript" and rel.endswith(".tsx")) else lang
    parser = get_parser(parser_key)
    tree = parser.parse(src_bytes)
    file_id = f"file:{rel}"
    res = FileResult()
    head = src_bytes[:SNIPPET_MAX].decode("utf-8", errors="replace")
    res.nodes.append((file_id, "file", rel, Path(rel).name, lang, None,
                      encode_span((0, 0)), encode_span(tree.root_node.end_point),
                      json.dumps({"bytes": len(src_bytes)})))
    res.fts.append((file_id, Path(rel).name, rel, "", head))
    res.embed_pairs.append((file_id, f"{rel}\n{head}"))
    ex = extract(tree, src_bytes, rel, file_id, lang)
    for attr in ("nodes", "edges", "ast_nodes", "ast_index", "fts", "embed_pairs"):
        getattr(res, attr).extend(getattr(ex, attr))
    return res


def delete_file_rows(conn: sqlite3.Connection, rel: str, vec_loaded: bool) -> None:
    file_id = f"file:{rel}"
    cur = conn.cursor()
    cur.execute("SELECT id FROM nodes WHERE path = ? OR id = ?", (rel, file_id))
    node_ids = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT id FROM ast_nodes WHERE file_id = ?", (file_id,))
    ast_ids = [r[0] for r in cur.fetchall()]
    cur.execute("DELETE FROM edges WHERE src_id = ? OR dst_id = ?", (file_id, file_id))
    for nid in node_ids:
        cur.execute("DELETE FROM edges WHERE src_id = ? OR dst_id = ?", (nid, nid))
        cur.execute("DELETE FROM fts_nodes WHERE node_id = ?", (nid,))
        if vec_loaded:
            with contextlib.suppress(sqlite3.OperationalError):
                cur.execute("DELETE FROM vec_embeddings WHERE node_id = ?", (nid,))
    cur.execute("DELETE FROM nodes WHERE id = ? OR path = ?", (file_id, rel))
    for aid in ast_ids:
        cur.execute("DELETE FROM ast_index WHERE ast_node_id = ?", (aid,))
    cur.execute("DELETE FROM ast_nodes WHERE file_id = ?", (file_id,))
    cur.execute("DELETE FROM file_state WHERE path = ?", (rel,))


def write_result(conn: sqlite3.Connection, res: FileResult) -> None:
    cur = conn.cursor()
    cur.executemany(
        "INSERT OR REPLACE INTO nodes(id,kind,path,name,language,signature,"
        "span_start,span_end,extra) VALUES (?,?,?,?,?,?,?,?,?)", res.nodes)
    cur.executemany(
        "INSERT OR REPLACE INTO edges(src_id,dst_id,type,extra) VALUES (?,?,?,?)", res.edges)
    cur.executemany(
        "INSERT OR REPLACE INTO ast_nodes(id,file_id,kind,span_start,span_end,parent_id,extra) "
        "VALUES (?,?,?,?,?,?,?)", res.ast_nodes)
    cur.executemany(
        "INSERT INTO ast_index(kind,attribute,value,ast_node_id) VALUES (?,?,?,?)", res.ast_index)
    cur.executemany(
        "INSERT INTO fts_nodes(node_id,name,path,signature,text_blob) VALUES (?,?,?,?,?)", res.fts)


# --------------------------------------------------------------------------- embeddings

class Embedder:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.model = None
        self.ok = False
        self._failed = False

    def ensure(self) -> bool:
        if self.ok:
            return True
        if self._failed:
            return False
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            self.model = SentenceTransformer(self.model_name)
            self.ok = True
            return True
        except Exception as e:
            log(f"[warn] sentence-transformers unavailable ({e}); skipping embeddings.")
            self._failed = True
            return False

    def encode(self, texts: list[str]):
        import numpy as np
        if self.model is None:
            raise RuntimeError("Embedder.encode called before ensure() succeeded")
        v = self.model.encode(texts, batch_size=BATCH_SIZE, show_progress_bar=False)
        return np.asarray(v, dtype="float32")


def write_embeddings(conn: sqlite3.Connection, embedder: Embedder,
                     pairs: list[tuple[str, str]]) -> int:
    if not pairs:
        return 0
    try:
        import sqlite_vec  # type: ignore
    except Exception:
        return 0
    by_id: dict[str, str] = {nid: (txt or " ") for nid, txt in pairs}
    ids = list(by_id.keys())
    texts = [by_id[i] for i in ids]
    count = 0
    cur = conn.cursor()
    for i in range(0, len(ids), BATCH_SIZE):
        batch_ids = ids[i:i + BATCH_SIZE]
        batch_texts = texts[i:i + BATCH_SIZE]
        try:
            vecs = embedder.encode(batch_texts)
        except Exception as e:
            log(f"[warn] embedding batch failed ({e}); skipping {len(batch_ids)} rows.")
            continue
        for nid, v in zip(batch_ids, vecs):
            try:
                cur.execute(
                    "INSERT OR REPLACE INTO vec_embeddings(node_id, embedding) VALUES (?, ?)",
                    (nid, sqlite_vec.serialize_float32(v.tolist())))
                count += 1
            except sqlite3.OperationalError as e:
                log(f"[warn] vec_embeddings insert failed for {nid}: {e}")
    return count


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    t0 = time.time()
    root = Path(args.root).resolve()
    db_path = Path(args.db).resolve()

    conn, vec_loaded = open_db(db_path)
    include_langs = {s.strip() for s in args.include_languages.split(",") if s.strip()}
    include_paths = [s.strip() for s in args.include_paths.split(",") if s.strip()]

    files_indexed = files_skipped = sym_count = edge_count = ast_count = 0
    errors: list[dict] = []
    all_embed_pairs: list[tuple[str, str]] = []
    seen_rels: set[str] = set()

    for raw_path in discover_files(args, root):
        try:
            path = raw_path if raw_path.is_absolute() else (root / raw_path)
            if not path.is_file():
                files_skipped += 1
                continue
            rel = normalize_rel(path, root)
            if rel is None:
                files_skipped += 1
                continue
            if include_paths and not any(
                rel == p or rel.startswith(p.rstrip("/") + "/") for p in include_paths
            ):
                files_skipped += 1
                continue
            seen_rels.add(rel)
            lang = LANG_BY_EXT.get(path.suffix.lower())
            if lang is None or lang not in include_langs:
                files_skipped += 1
                continue
            size = path.stat().st_size
            if size > args.max_file_size_bytes:
                files_skipped += 1
                log(f"[skip] {rel} ({size} bytes > max)")
                continue

            src_bytes = path.read_bytes()
            content_hash = hashlib.sha256(src_bytes).hexdigest()

            if not args.full:
                row = conn.execute(
                    "SELECT content_hash FROM file_state WHERE path = ?", (rel,)
                ).fetchone()
                if row and row[0] == content_hash:
                    files_skipped += 1
                    continue

            try:
                conn.execute("BEGIN")
                delete_file_rows(conn, rel, vec_loaded)
                res = process_file(path, rel, lang, src_bytes)
                write_result(conn, res)
                conn.execute(
                    "INSERT OR REPLACE INTO file_state(path, content_hash, indexed_at, language) "
                    "VALUES (?, ?, ?, ?)",
                    (rel, content_hash, datetime.now(timezone.utc).isoformat(), lang))
                conn.execute("COMMIT")
            except Exception as e:
                conn.execute("ROLLBACK")
                errors.append({"path": rel, "error": f"{type(e).__name__}: {e}"})
                log(f"[error] {rel}: {e}")
                files_skipped += 1
                continue

            files_indexed += 1
            sym_count += sum(1 for n in res.nodes if n[1] != "file")
            edge_count += len(res.edges)
            ast_count += len(res.ast_nodes)
            all_embed_pairs.extend(res.embed_pairs)

        except Exception as e:
            errors.append({"path": str(raw_path), "error": f"{type(e).__name__}: {e}"})
            log(f"[error] {raw_path}: {e}")
            files_skipped += 1

    # --full sweep: drop rows for files that no longer exist on disk.
    files_removed = 0
    if args.full:
        stale = conn.execute("SELECT path FROM file_state").fetchall()
        for (rel_stale,) in stale:
            if rel_stale not in seen_rels:
                try:
                    conn.execute("BEGIN")
                    delete_file_rows(conn, rel_stale, vec_loaded)
                    conn.execute("COMMIT")
                    files_removed += 1
                except Exception as e:
                    conn.execute("ROLLBACK")
                    log(f"[warn] failed to purge stale {rel_stale}: {e}")

    embeddings_written = 0
    if not args.no_embeddings and vec_loaded and all_embed_pairs:
        embedder = Embedder(args.embed_model)
        if embedder.ensure():
            try:
                conn.execute("BEGIN")
                embeddings_written = write_embeddings(conn, embedder, all_embed_pairs)
                conn.execute("COMMIT")
            except Exception as e:
                conn.execute("ROLLBACK")
                log(f"[warn] embedding phase failed: {e}")

    conn.commit()
    conn.close()

    summary = {
        "files_indexed": files_indexed,
        "files_skipped": files_skipped,
        "files_removed": files_removed,
        "symbols": sym_count,
        "edges": edge_count,
        "ast_nodes": ast_count,
        "embeddings": embeddings_written,
        "duration_ms": int((time.time() - t0) * 1000),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
