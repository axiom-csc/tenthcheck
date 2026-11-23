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
    tenthcheck agents edit  <repo_dir> <file_path> [new_source_file] --session S [--symbol X ...]
                                                                record a session's edit; warn (never block) when it
                                                                is inside another session's blast radius
    tenthcheck agents check <repo_dir> <file_path> <new_source_file> --session S   same warnings, records nothing
    tenthcheck agents list  <repo_dir>                          live sessions and the symbols each has edited
    tenthcheck agents end   <repo_dir> --session S              forget a session
    tenthcheck serve  [repo_dir]                                MCP stdio server for repo_dir (default: current
                                                                directory; needs `pip install "tenthcheck[mcp]"`)
"""
from __future__ import annotations

import argparse
import json
import os
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


def _agents(a, ap) -> int:
    from tenthcheck import agents
    if a.agents_cmd == "list":
        res = agents.list_sessions(a.repo_dir)
        if a.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"{len(res['sessions'])} live session(s)")
            for s in res["sessions"]:
                print(f"  {s['session']}{' (' + s['label'] + ')' if s['label'] else ''}: {len(s['edits'])} symbol(s), "
                      f"last edit {s['last_seen_age_s']} s ago")
                for e in s["edits"]:
                    print(f"      {e}")
        return 0
    if not a.session:
        ap.error("agents: give --session or set TENTHCHECK_SESSION")
    if a.agents_cmd == "end":
        print(f"session {a.session}: {'ended' if agents.end_session(a.repo_dir, a.session) else 'not found'}")
        return 0
    if a.new_source_file and not a.file_path:
        ap.error("agents: NEW_SOURCE_FILE needs FILE_PATH")
    if not a.new_source_file and not a.symbol:
        ap.error("agents: give FILE_PATH NEW_SOURCE_FILE and/or --symbol")
    src = Path(a.new_source_file).read_text(encoding="utf-8") if a.new_source_file else None
    from tenthcheck.resolve import resolve_symbol
    syms = []
    if a.symbol:
        g = parse_package(a.repo_dir, skip_syntax_errors=True)
        syms = [resolve_symbol(g, s) for s in a.symbol]
    fn = agents.record_edit if a.agents_cmd == "edit" else agents.check
    kw = {"label": a.label} if a.agents_cmd == "edit" else {}
    res = fn(a.repo_dir, a.session, a.file_path, src, syms, depth=a.depth, **kw)
    print(json.dumps(res, indent=2)) if a.json else print(agents.format_result(res))
    return 0  # warn only: overlap never changes the exit code


def _install(a, ap) -> int:
    from tenthcheck import install as inst
    targets = [t for t in ("claude", "cursor") if getattr(a, t)]
    if not targets:
        ap.error(f"{a.cmd}: give --claude and/or --cursor")
    root = Path(a.dir).resolve()
    un = a.cmd == "uninstall"
    changes = []
    for t in targets:
        changes += inst.plan(root, t, un, mcp=not a.no_mcp, hook=not a.no_hook)
    shown = False
    for c in changes:
        if c.changed:
            print(c.diff(root)); shown = True
    if not shown:
        print("Nothing to change.")
    if a.dry_run:
        print("(dry run: nothing written)")
        return 0
    for m in inst.apply(root, changes, un):
        print(m)
    if not un:
        for w in inst.preflight_warnings():
            print("warning:", w)
    return 0


def _verify(a, ap) -> int:
    from tenthcheck import config, z3check
    if "::" in a.target:
        if not a.check:
            ap.error("verify FILE::FUNC needs a CHECK expression")
        f, fn = a.target.split("::", 1)
        results = [z3check.check_file(f, fn, a.check, a.assume)]
    else:
        cfg = config.load(a.target)
        if not cfg.handwritten:
            print(f"No [[invariant]] entries in {config.FILENAME}. Add one, or run: tenthcheck verify file.py::func 'expr'")
            return 0
        results = z3check.check_handwritten(a.target, cfg)
    if a.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            print(r.format())
    return 1 if any(r.status == "violated" for r in results) else 0


def _scan_cmds(a) -> int:
    from tenthcheck import config, mining, report
    cfg = config.load(a.repo_dir)
    ms = getattr(a, "min_support", None) or cfg.min_support
    mc = getattr(a, "min_consistency", None) or cfg.min_consistency
    res = mining.scan(a.repo_dir, ms, mc, include_tests=getattr(a, "include_tests", False), exclude=cfg.exclude)
    if a.cmd == "init":
        path, how = config.init(a.repo_dir, res.invariants)
        print(f"{path} {how}: {len(res.invariants)} mined patterns "
              f"({len(res.findings)} with exceptions). Edit status = accepted / rejected.")
        return 0
    if a.json:
        print(json.dumps({"files": res.files, "seconds": round(res.seconds, 3),
                          "invariants": [i.to_dict() for i in res.invariants]}, indent=2))
    else:
        print(report.format_scan(res, cfg, report.use_color(), a.limit))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or (args[0].startswith("-") and args[0] not in ("--version", "-h", "--help")):
        args = ["scan"] + args  # `tenthcheck` alone is the zero-config scan
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
    p = sub.add_parser("scan", help="zero-config scan: mine invariants, report inconsistencies (default command)")
    p.add_argument("repo_dir", nargs="?", default=".")
    p.add_argument("--min-support", type=int, default=None); p.add_argument("--min-consistency", type=float, default=None)
    p.add_argument("--include-tests", action="store_true"); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="Z3-check numeric invariants: from tenthcheck.toml, or FILE::FUNC CHECK on the command line")
    p.add_argument("target", nargs="?", default=".", help="repo dir, or path/to/file.py::function")
    p.add_argument("check", nargs="?", help="invariant in plain Python, e.g. 'final_balance >= 0'")
    p.add_argument("--assume", action="append", default=[], help="a condition known true on entry (repeatable)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("impact", help="uncommitted diff -> changed symbols -> dependents -> touched invariants (Z3 re-check)")
    p.add_argument("repo_dir", nargs="?", default="."); p.add_argument("--base", default="HEAD")
    p.add_argument("--json", action="store_true")
    for name, hlp in (("install", "register the MCP server and/or hook for an agent (backup + diff, idempotent)"),
                      ("uninstall", "reverse `install`")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--claude", action="store_true"); p.add_argument("--cursor", action="store_true")
        p.add_argument("--no-hook", action="store_true", help="MCP server only"); p.add_argument("--no-mcp", action="store_true", help="hook only")
        p.add_argument("--dry-run", action="store_true"); p.add_argument("--dir", default=".")
    p = sub.add_parser("ci", help="CI mode: enforce accepted invariants, write a PR-comment summary, optional signed receipt")
    p.add_argument("repo_dir", nargs="?", default="."); p.add_argument("--base", default=None, help="base ref of the PR, e.g. origin/main")
    p.add_argument("--summary-file", default=None, help="write the markdown summary here (default: print it)")
    p.add_argument("--receipt", default=None, help="write a signed receipt here (needs TENTHCHECK_SIGNING_KEY)")
    p.add_argument("--json", action="store_true")
    sub.add_parser("keygen", help="print a new Ed25519 receipt keypair (store the private part as a repo secret)")
    p = sub.add_parser("verify-receipt", help="verify a signed receipt")
    p.add_argument("receipt"); p.add_argument("--public-key", default=None); p.add_argument("--repo", default=None)
    p = sub.add_parser("init", help="write tenthcheck.toml listing mined invariants to accept or reject")
    p.add_argument("repo_dir", nargs="?", default=".")
    sub.add_parser("hook", help="Claude Code hook mode (see `tenthcheck hook --help`)")
    p = sub.add_parser("agents", help="multi-agent edit warnings (sessions under .tenthcheck/)")
    asub = p.add_subparsers(dest="agents_cmd", required=True)
    for name, hlp in (("edit", "record an edit and warn on overlap"), ("check", "warn on overlap, record nothing")):
        q = asub.add_parser(name, help=hlp)
        q.add_argument("repo_dir"); q.add_argument("file_path", nargs="?")
        q.add_argument("new_source_file", nargs="?", help="proposed new contents of file_path")
        q.add_argument("--symbol", action="append", default=[], help="an edited symbol (repeatable)")
        q.add_argument("--session", default=os.environ.get("TENTHCHECK_SESSION"))
        q.add_argument("--label", default="")
        q.add_argument("--depth", type=int, default=2, help="hops of callers/callees in a blast radius (default 2)")
        q.add_argument("--json", action="store_true")
    q = asub.add_parser("list", help="live sessions"); q.add_argument("repo_dir"); q.add_argument("--json", action="store_true")
    q = asub.add_parser("end", help="forget a session"); q.add_argument("repo_dir")
    q.add_argument("--session", default=os.environ.get("TENTHCHECK_SESSION"))
    p = sub.add_parser("serve", help="run the MCP stdio server (server name: tenthcheck)")
    p.add_argument("repo_dir", nargs="?", default=".")
    a = ap.parse_args(args)

    try:
        if a.cmd == "keygen":
            from tenthcheck.receipt import keygen
            priv, pub = keygen()
            print(f"private (secret TENTHCHECK_SIGNING_KEY): {priv}\npublic  (--public-key):                  {pub}")
            return 0
        if a.cmd == "verify-receipt":
            from tenthcheck.receipt import verify
            ok, msgs = verify(Path(a.receipt), a.public_key, Path(a.repo) if a.repo else None)
            print("\n".join(msgs))
            return 0 if ok else 1
        if a.cmd == "ci":
            from tenthcheck import ci, receipt
            res = ci.run_ci(a.repo_dir, a.base)
            md = ci.markdown(res)
            if a.summary_file:
                Path(a.summary_file).write_text(md, encoding="utf-8")
            if a.receipt:
                wrote = receipt.write_receipt(Path(a.receipt), Path(a.repo_dir).resolve(), res["summary"], res["config_text"])
                print(("receipt written to " + a.receipt) if wrote else f"no receipt: {receipt.KEY_ENV} is not set", file=sys.stderr)
            print(json.dumps(res["summary"], indent=2) if a.json else md)
            return 1 if res["failed"] else 0
        if a.cmd in ("install", "uninstall"):
            return _install(a, ap)
        if a.cmd == "impact":
            from tenthcheck.impact import format_impact, impact
            res = impact(a.repo_dir, a.base)
            if a.json:
                res = dict(res, z3=[r.to_dict() for r in res["z3"]])
                print(json.dumps(res, indent=2, default=list))
            else:
                print(format_impact(res))
            return 1 if res["mined_violations"] or any(r.status == "violated" for r in res["z3"]) else 0
        if a.cmd == "verify":
            return _verify(a, ap)
        if a.cmd in ("scan", "init"):
            return _scan_cmds(a)
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
        if a.cmd == "agents":
            return _agents(a, ap)
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
