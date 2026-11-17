# Vendored from axiom-lab mind/code/check_edit.py by scripts/sync_from_mind.py. Edit the source, then re-sync.
"""Check a proposed edit to one file before it is written.

Parses the repo twice: as it is on disk, and with `file_path` replaced in memory by the
proposed source. Reports
- `new_cycles`: import edges that are on a cycle after the edit but were not before;
- `changed`: functions/classes added, removed or modified (by AST hash) with their dependents
  (callers, subclasses, importers), from `tenthcheck.scope_audit.audit_graphs`.

`ok` is False when a new import cycle appears. Nothing is written to disk. Like the parser, this
is structural only: it does not run code and does not judge whether the edit is correct.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from tenthcheck.cycles import format_cycle, new_cycles
from tenthcheck.parser import parse_package
from tenthcheck.scope_audit import audit_graphs


def check_edit(repo_dir: str | Path, file_path: str | Path, new_source: str, max_nodes: int | None = 10_000,
               index=None) -> dict[str, Any]:
    """`index`: an optional `IncrementalCodeIndex` for `repo_dir`, reused instead of re-parsing the repo."""
    root = Path(repo_dir).resolve()
    target = Path(file_path)
    if target.is_absolute():
        target = target.resolve().relative_to(root)
    rel = target.as_posix()
    if not rel.endswith(".py"):
        raise ValueError(f"not a Python file: {rel}")
    try:
        ast.parse(new_source, filename=rel)
    except SyntaxError as error:
        return {"ok": False, "file": rel, "syntax_error": str(error), "new_cycles": [], "changed": None}
    if index is not None:
        before = index.graph
        after = index.graph_with_overrides({rel: new_source})
    else:
        before = parse_package(str(root), skip_syntax_errors=True)
        after = parse_package(str(root), skip_syntax_errors=True, overrides={rel: new_source})
    cycles = new_cycles(before, after)
    changed = audit_graphs(before, after, max_nodes=max_nodes)
    return {"ok": not cycles, "file": rel, "syntax_error": None, "new_cycles": cycles, "changed": changed}


def format_check(res: dict[str, Any]) -> str:
    lines = [f"File: {res['file']}"]
    if res.get("syntax_error"):
        lines.append(f"SYNTAX ERROR in proposed source: {res['syntax_error']}")
        return "\n".join(lines)
    if res["new_cycles"]:
        lines.append(f"NEW IMPORT CYCLES: {len(res['new_cycles'])}")
        for c in res["new_cycles"]:
            added = ", ".join(f"{a} -> {b}" for a, b in c["new_edges"]) or "(no new edge; existing edges now close a cycle)"
            lines.append(f"  {format_cycle(c['cycle'])}   [added by this edit: {added}]")
    else:
        lines.append("New import cycles: none")
    ch = res["changed"]
    lines.append(f"Changed symbols: {ch['touched_entities']} "
                 f"(modified {len(ch['modified_entities'])}, added {len(ch['added_entities'])}, "
                 f"removed {len(ch['removed_entities'])})")
    for eid in ch["changed_entities"]:
        rad = ch["blast_radii"].get(eid, {})
        deps = sorted({d for depth, ids in rad.get("by_depth", {}).items() if depth > 0 for d in ids})
        more = " (truncated)" if rad.get("truncated") else ""
        lines.append(f"  {eid}: {len(deps)} dependents{more}")
        for d in deps[:20]:
            lines.append(f"      <- {d}")
        if len(deps) > 20:
            lines.append(f"      ... {len(deps) - 20} more")
    return "\n".join(lines)
