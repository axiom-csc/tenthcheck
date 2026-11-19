"""`tenthcheck` command line.

    tenthcheck check  <repo_dir>                                import cycles + a summary of the parsed repo
    tenthcheck check  <repo_dir> <file_path> <new_source_file>  new import cycles + dependents of changed symbols;
                                                                exit 1 if a new cycle appears (2 on a syntax error)
    tenthcheck blast  <repo_dir> <symbol>                       what calls/imports/subclasses <symbol>, by hop
    tenthcheck cycles <repo_dir>                                import cycles from module-level imports
    tenthcheck scope  <before_dir> <after_dir>                  changed symbols and their untouched dependents
    tenthcheck hook   [--dependents N] [--max-lines 10]         Claude Code hook (PostToolUse on Edit/Write, and
                                                                Stop): reads the hook JSON on stdin; silent unless
                                                                an edit adds a cycle or leaves callers of a changed
                                                                signature broken (exit 2, shown to the agent)
    tenthcheck serve  [repo_dir]                                MCP stdio server for repo_dir (default: current
                                                                directory; needs `pip install "tenthcheck[mcp]"`)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from tenthcheck import __version__
from tenthcheck.check_edit import check_edit, format_check
from tenthcheck.cycles import format_cycle, import_cycles
from tenthcheck.parser import parse_package
from tenthcheck.resolve import AmbiguousSymbol, resolve_symbol
from tenthcheck.scope_audit import audit_scope, format_report
from tenthcheck.topology import dependents_radius

DEFAULT_MAX_NODES = 10_000


def blast(repo_dir: str, symbol: str, max_depth: int | None = None, max_nodes: int | None = DEFAULT_MAX_NODES) -> dict:
    g = parse_package(repo_dir, skip_syntax_errors=True)
    eid = resolve_symbol(g, symbol)
    rad = dependents_radius(g, eid, max_depth=max_depth, max_nodes=max_nodes)
    return {"symbol": eid, "type": g[eid].type, **rad,
            "skipped_syntax_files": g.meta.get("skipped_syntax_files", [])}


def cycles(repo_dir: str) -> dict:
    g = parse_package(repo_dir, skip_syntax_errors=True)
    found = import_cycles(g)
    return {"n_cyclic_components": len(found), "components": found,
            "skipped_syntax_files": g.meta.get("skipped_syntax_files", [])}


def summary(repo_dir: str) -> dict:
    """Cycles plus entity/edge counts for the whole repo (no proposed edit)."""
    g = parse_package(repo_dir, skip_syntax_errors=True)
    found = import_cycles(g)
    return {"entities": dict(sorted(Counter(e.type for e in g.entities.values()).items())),
            "edges": dict(sorted(Counter(r.type for r in g.relations).items())),
            "n_cyclic_components": len(found), "components": found,
            "skipped_syntax_files": g.meta.get("skipped_syntax_files", [])}


def _print_summary(res: dict) -> None:
    ent, edg = res["entities"], res["edges"]
    print("Parsed: " + ", ".join(f"{n} {k}" for k, n in ent.items()))
    print("Edges: " + (", ".join(f"{n} {k}" for k, n in edg.items()) or "none"))
    if res["skipped_syntax_files"]:
        print(f"Skipped (syntax errors): {len(res['skipped_syntax_files'])} file(s)")
        for f in res["skipped_syntax_files"]:
            print(f"  {f}")
    _print_cycles(res)


def _print_blast(res: dict) -> None:
    print(f"{res['symbol']} ({res['type']}): {res['affected'] - 1} dependents"
          + (" (TRUNCATED at node cap)" if res["truncated"] else ""))
    for depth in sorted(res["by_depth"]):
        if depth == 0:
            continue
        ids = res["by_depth"][depth]
        print(f"  hop {depth} ({len(ids)}):")
        for eid in ids:
            print(f"    {eid}")


def _print_cycles(res: dict) -> None:
    print(f"Import cycles: {res['n_cyclic_components']} cyclic component(s)")
    for comp in res["components"]:
        print(f"  [{len(comp['modules'])} modules] {format_cycle(comp['cycle'])}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["hook"]:  # own parser: stdin JSON in, nothing on stdout, exit 0 or 2
        from tenthcheck.hook import main as hook_main
        return hook_main(args[1:])
    ap = argparse.ArgumentParser(prog="tenthcheck", description="Tenthcheck, from Tenthlook: see what an AI coding agent's edit could break before it lands.")
    ap.add_argument("--version", action="version", version=f"tenthcheck {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="cycles + summary of a repo; with FILE_PATH NEW_SOURCE_FILE, check a proposed edit")
    p.add_argument("repo_dir")
    p.add_argument("file_path", nargs="?", help="file to replace, relative to repo_dir")
    p.add_argument("new_source_file", nargs="?", help="proposed new contents of file_path")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("blast", help="dependents of a symbol by hop depth")
    p.add_argument("repo_dir"); p.add_argument("symbol")
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("cycles", help="import cycles")
    p.add_argument("repo_dir"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("scope", help="scope report between two snapshots")
    p.add_argument("before_dir"); p.add_argument("after_dir"); p.add_argument("--json", action="store_true")
    sub.add_parser("hook", help="Claude Code hook mode (see `tenthcheck hook --help`)")
    p = sub.add_parser("serve", help="run the MCP stdio server (server name: tenthcheck)")
    p.add_argument("repo_dir", nargs="?", default=".")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "blast":
            res = blast(a.repo_dir, a.symbol, a.max_depth, a.max_nodes)
            print(json.dumps(res, indent=2)) if a.json else _print_blast(res)
            return 0
        if a.cmd == "cycles":
            res = cycles(a.repo_dir)
            print(json.dumps(res, indent=2)) if a.json else _print_cycles(res)
            return 0
        if a.cmd == "scope":
            res = audit_scope(a.before_dir, a.after_dir)
            print(json.dumps(res, indent=2)) if a.json else print(format_report(res))
            return 0
        if a.cmd == "check" and a.file_path is None:
            res = summary(a.repo_dir)
            print(json.dumps(res, indent=2)) if a.json else _print_summary(res)
            return 0
        if a.cmd == "check":
            if a.new_source_file is None:
                ap.error("check: give both FILE_PATH and NEW_SOURCE_FILE, or neither")
            src = Path(a.new_source_file).read_text(encoding="utf-8")
            res = check_edit(a.repo_dir, a.file_path, src)
            print(json.dumps(res, indent=2)) if a.json else print(format_check(res))
            if res.get("syntax_error"):
                return 2
            return 0 if res["ok"] else 1
        if a.cmd == "serve":
            from tenthcheck.server import serve
            serve(a.repo_dir)
            return 0
    except AmbiguousSymbol as e:
        print(f"tenthcheck: {e}", file=sys.stderr)
        return 2
    except (KeyError, NotADirectoryError, FileNotFoundError, ValueError) as e:
        print(f"tenthcheck: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
