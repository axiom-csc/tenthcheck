"""Compare Tenthcheck's static IMPORTS edges with runtime-traced ones (scripts/trace_imports.py).

    python scripts/compare_imports.py <trace.json> <parse_root> <out.json>

Static edges are restricted to the traced modules (package code only; tests and `__main__` excluded),
so both sides cover the same modules.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from tenthcheck.cycles import strongly_connected
from tenthcheck.parser import parse_package

trace = json.loads(Path(sys.argv[1]).read_text())
mods = set(trace["modules"])
g = parse_package(sys.argv[2], skip_syntax_errors=True)
static = {(r.src, r.dst) for r in g.rels("IMPORTS") if r.src in mods and r.dst in mods and r.src != r.dst}
runtime = {tuple(e) for e in trace["edges"] if e[0] in mods and e[1] in mods}


def comps(edges):
    adj: dict[str, list[str]] = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, [])
    return strongly_connected({k: sorted(v) for k, v in adj.items()})


both = static & runtime
res = {
    "package": trace["package"], "modules": len(mods),
    "static_edges": len(static), "runtime_edges": len(runtime), "both": len(both),
    "runtime_only (static misses)": sorted(runtime - static),
    "static_only (not executed at runtime)": sorted(static - runtime),
    "edge_recall_vs_runtime": round(len(both) / len(runtime), 4) if runtime else None,
    "static_cyclic_components": comps(static),
    "runtime_cyclic_components": comps(runtime),
}
Path(sys.argv[3]).write_text(json.dumps(res, indent=2))
print(json.dumps({k: (v if not isinstance(v, list) else (v if len(v) < 12 else f"{len(v)} items")) for k, v in res.items()}, indent=1))
