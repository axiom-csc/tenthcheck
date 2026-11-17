"""Parse real repositories and record crashes, time, entity/edge counts and cycles.

    python scripts/harden.py <corpus_dir> <out.json>

<corpus_dir> holds clones named httpx, flask, pydantic (see results/corpus.txt for SHAs).
Every repo is parsed at its root (tests and docs included), as a user would point the tool at it.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

from tenthcheck.cycles import import_cycles
from tenthcheck.parser import iter_py_files, parse_package
from tenthcheck.topology import dependents_radius, reverse_adjacency

REPOS = ["httpx", "flask", "pydantic"]
MAX_NODES = 10_000


def run(repo: Path) -> dict:
    row: dict = {"repo": repo.name, "path": str(repo)}
    row["sha"] = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    row["py_files"] = len(iter_py_files(repo))
    try:
        t0 = time.perf_counter()
        g = parse_package(str(repo), skip_syntax_errors=True)
        row["parse_s"] = round(time.perf_counter() - t0, 3)
    except Exception as e:  # record, don't hide
        row["crash"] = f"{type(e).__name__}: {e}"
        row["traceback"] = traceback.format_exc()
        return row
    row["crash"] = None
    row["skipped_syntax_files"] = g.meta.get("skipped_syntax_files", [])
    row["entities"] = dict(sorted(Counter(e.type for e in g.entities.values()).items()))
    row["relations"] = dict(sorted(Counter(r.type for r in g.relations).items()))
    t0 = time.perf_counter()
    cyc = import_cycles(g)
    row["cycles_s"] = round(time.perf_counter() - t0, 4)
    row["cyclic_components"] = len(cyc)
    row["cyclic_component_sizes"] = sorted((len(c["modules"]) for c in cyc), reverse=True)
    row["example_cycles"] = [c["cycle"] for c in cyc[:5]]
    incoming = reverse_adjacency(g)
    funcs = [eid for eid, e in g.entities.items() if e.type in ("function", "class")]
    t0 = time.perf_counter()
    sizes, truncated = [], 0
    for eid in funcs:
        r = dependents_radius(g, eid, max_nodes=MAX_NODES, incoming=incoming)
        sizes.append(r["affected"] - 1)
        truncated += r["truncated"]
    row["dependents_all_defs_s"] = round(time.perf_counter() - t0, 3)
    sizes.sort()
    row["dependents"] = {
        "n_defs": len(funcs),
        "with_zero_dependents": sum(1 for s in sizes if s == 0),
        "median": sizes[len(sizes) // 2] if sizes else None,
        "max": sizes[-1] if sizes else None,
        "hit_node_cap": truncated,
    }
    return row


def main() -> None:
    corpus, out = Path(sys.argv[1]), Path(sys.argv[2])
    rows = [run(corpus / name) for name in REPOS]
    out.write_text(json.dumps({"python": sys.version.split()[0], "max_nodes": MAX_NODES, "repos": rows}, indent=2))
    for r in rows:
        print(r["repo"], "crash=" + str(r["crash"]), r.get("py_files"), "files", r.get("parse_s"), "s",
              r.get("entities"), r.get("relations"), "cycles", r.get("cyclic_components"),
              "skipped", len(r.get("skipped_syntax_files", [])), r.get("dependents"))


if __name__ == "__main__":
    main()
