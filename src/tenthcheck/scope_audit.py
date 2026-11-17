# Vendored from axiom-lab mind/code/scope_audit.py by scripts/sync_from_mind.py. Edit the source, then re-sync.
"""Code-scope diff auditor based on blast radius.

Compares two versions of a codebase (before and after directories), identifies
function and class entities that were added, removed, or modified (via AST hash),
computes their dependents (callers, subclasses, importers, transitively) with
`dependents_radius`, and reports the gap between the entities that can be affected
by the change and the entities the diff actually touched.

`scope_creep` counts untouched dependents: code the diff did not edit but which
calls into edited code. It measures review surface, not whether the edits were
on-task; the tool cannot know the task.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tenthcheck.parser import parse_package
from tenthcheck.topology import dependents_radius, reverse_adjacency
from tenthcheck.graph import Graph


def audit_scope(before_dir: str | Path, after_dir: str | Path, max_nodes: int | None = None) -> dict[str, Any]:
    """Audit structural differences and untouched dependents between two codebase snapshots."""
    before_path = Path(before_dir).resolve()
    after_path = Path(after_dir).resolve()
    before_graph = parse_package(str(before_path), skip_syntax_errors=True)
    after_graph = parse_package(str(after_path), skip_syntax_errors=True)
    result = {"before_dir": str(before_path), "after_dir": str(after_path)}
    result.update(audit_graphs(before_graph, after_graph, max_nodes=max_nodes))
    return result


def audit_graphs(before_graph: Graph, after_graph: Graph, max_nodes: int | None = None) -> dict[str, Any]:
    """`audit_scope` on two already-parsed graphs.

    A class counts as modified when anything in its body changes, methods included, because its
    `ast_hash` covers the whole class statement. `max_nodes` caps each dependents walk.
    """
    before_fc = {
        eid: e for eid, e in before_graph.entities.items()
        if e.type in ("function", "class")
    }
    after_fc = {
        eid: e for eid, e in after_graph.entities.items()
        if e.type in ("function", "class")
    }

    added = sorted(set(after_fc) - set(before_fc))
    removed = sorted(set(before_fc) - set(after_fc))
    common = set(before_fc) & set(after_fc)
    modified = sorted([
        eid for eid in common
        if before_fc[eid].attrs.get("ast_hash") != after_fc[eid].attrs.get("ast_hash")
    ])

    changed_entities = sorted(set(added) | set(removed) | set(modified))

    blast_radii: dict[str, dict] = {}
    union_blast_entities: set[str] = set()

    before_in = reverse_adjacency(before_graph)
    after_in = reverse_adjacency(after_graph)
    for eid in changed_entities:
        # Modified: dependents in either snapshot. Removed: old callers. Added: new callers.
        radii: list[dict] = []
        if eid in before_graph.entities:
            radii.append(dependents_radius(before_graph, eid, max_nodes=max_nodes, incoming=before_in))
        if eid in after_graph.entities:
            radii.append(dependents_radius(after_graph, eid, max_nodes=max_nodes, incoming=after_in))
        if not radii:
            continue
        by_depth: dict[int, set[str]] = {}
        for rad in radii:
            for depth, ids in rad["by_depth"].items():
                by_depth.setdefault(depth, set()).update(ids)
        merged: dict[int, list[str]] = {}
        placed: set[str] = set()
        for depth in sorted(by_depth):  # an entity is listed at its shallowest depth
            fresh = sorted(by_depth[depth] - placed)
            if fresh:
                merged[depth] = fresh
                placed.update(fresh)
        blast_radii[eid] = {"affected": len(placed), "by_depth": merged,
                            "truncated": any(rad["truncated"] for rad in radii)}
        union_blast_entities.update(placed)

    total_distinct = len(union_blast_entities)
    touched_count = len(changed_entities)
    scope_creep = total_distinct - touched_count
    untouched = sorted(union_blast_entities - set(changed_entities))

    return {
        "changed_entities": changed_entities,
        "modified_entities": modified,
        "added_entities": added,
        "removed_entities": removed,
        "total_distinct_entities": total_distinct,
        "touched_entities": touched_count,
        "scope_creep": scope_creep,
        "union_blast_entities": sorted(union_blast_entities),
        "untouched_dependencies": untouched,
        "blast_radii": {
            eid: {
                "affected": rad["affected"],
                "by_depth": rad["by_depth"],
                "truncated": rad["truncated"],
            }
            for eid, rad in blast_radii.items()
        },
    }


def format_report(result: dict[str, Any]) -> str:
    lines = [
        "==================================================",
        "             CODE SCOPE AUDIT REPORT              ",
        "==================================================",
        f"Before: {result.get('before_dir', '(in memory)')}",
        f"After:  {result.get('after_dir', '(in memory)')}",
        "--------------------------------------------------",
        f"Changed Entities Total: {result['touched_entities']}",
        f"  - Modified: {len(result['modified_entities'])}",
        f"  - Added:    {len(result['added_entities'])}",
        f"  - Removed:  {len(result['removed_entities'])}",
        "",
        f"Distinct entities in dependents (incl. changed): {result['total_distinct_entities']}",
        f"Actually touched entities:                 {result['touched_entities']}",
        f"Untouched dependents (review surface):      {result['scope_creep']}",
        "--------------------------------------------------",
    ]

    if result["changed_entities"]:
        lines.append("Changed entities:")
        for eid in result["changed_entities"]:
            tag = "MOD" if eid in result["modified_entities"] else ("ADD" if eid in result["added_entities"] else "REM")
            rad = result["blast_radii"].get(eid, {})
            aff = rad.get("affected", 0)
            lines.append(f"  [{tag}] {eid} (dependents: {aff})")

    if result["untouched_dependencies"]:
        lines.append("")
        lines.append("Untouched dependents of changed code:")
        for dep in result["untouched_dependencies"]:
            lines.append(f"  * {dep}")

    lines.append("==================================================")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Audit scope changes and blast radius between two code snapshots.")
    parser.add_argument("before_dir", type=str, help="Path to before code directory.")
    parser.add_argument("after_dir", type=str, help="Path to after code directory.")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text report.")
    args = parser.parse_args()

    result = audit_scope(args.before_dir, args.after_dir)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
