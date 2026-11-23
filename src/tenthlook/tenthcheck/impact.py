"""`tenthcheck impact`: what does the uncommitted diff touch?

changed symbols (AST-hash diff against HEAD) -> their dependents (existing blast-radius graph) ->
hand-written / accepted invariants in the touched code -> Z3 re-check of touched numeric invariants.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from tenthlook.tenthcheck import config as cfgmod
from tenthlook.tenthcheck.enforce import describe_violation, enforce
from tenthlook.tenthcheck.graphcache import load_graph
from tenthlook.tenthcheck.scope_audit import audit_graphs


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def changed_files(root: Path, base: str = "HEAD") -> dict[str, str | None]:
    """repo-relative .py path -> base source (None if the file is new)."""
    out: dict[str, str | None] = {}
    for line in _git(root, "diff", "--no-renames", "--name-status", base).splitlines():
        st, _, path = line.partition("\t")
        if not path.endswith(".py"):
            continue
        if st == "A":
            out[path] = None
        else:
            out[path] = _git(root, "show", f"{base}:{path}")
    for path in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        if path.endswith(".py"):
            out[path] = None
    return out


def impact(repo_dir: str, base: str = "HEAD", max_nodes: int = 10_000) -> dict:
    root = Path(repo_dir).resolve()
    t = {}
    t0 = time.perf_counter()
    files = changed_files(root, base)
    t["git"] = time.perf_counter() - t0
    if not files:
        return {"files": [], "changed": None, "z3": [], "mined_violations": [], "timing": {"total": t["git"], **t}}
    t1 = time.perf_counter()
    after, hit_after = load_graph(root)
    before, hit_before = load_graph(root, dict(files))
    t["parse"] = time.perf_counter() - t1
    t2 = time.perf_counter()
    ch = audit_graphs(before, after, max_nodes=max_nodes)
    t["blast"] = time.perf_counter() - t2
    cfg = cfgmod.load(root)
    changed_ids = set(ch["changed_entities"])
    t3 = time.perf_counter()
    z3res = []
    if any(h.file in files for h in cfg.handwritten):
        from tenthlook.tenthcheck import z3check
    for h in cfg.handwritten:
        if h.file in files and any(e.endswith("." + h.function) or e == h.function for e in changed_ids):
            z3res.append(z3check.check_file(root / h.file, h.function, h.check, h.assume, rel=h.file))
    t["z3"] = time.perf_counter() - t3
    t4 = time.perf_counter()
    mined = []
    if cfg.accepted():
        enf = enforce(root, cfg, files=set(files))
        mined = [{"id": i.id, "site": str(s), "text": describe_violation(i, s)} for i, s in enf.mined_violations]
    t["mined"] = time.perf_counter() - t4
    t["total"] = time.perf_counter() - t0
    return {"files": sorted(files), "changed": ch, "z3": z3res, "mined_violations": mined,
            "cache": {"before_graph_hit": hit_before, "after_graph_hit": hit_after},
            "timing": {k: round(v, 4) for k, v in t.items()}}


def format_impact(res: dict, limit: int = 15) -> str:
    if not res["files"]:
        return "No uncommitted Python changes."
    ch = res["changed"]
    lines = [f"Changed: {len(res['files'])} file(s), {ch['touched_entities']} symbol(s) "
             f"(modified {len(ch['modified_entities'])}, added {len(ch['added_entities'])}, removed {len(ch['removed_entities'])})"]
    affected: set[str] = set()
    for eid in ch["changed_entities"]:
        rad = ch["blast_radii"].get(eid, {})
        deps = sorted({d for depth, ids in rad.get("by_depth", {}).items() if int(depth) > 0 for d in ids})
        affected |= set(deps)
        lines.append(f"  {eid}: {len(deps)} dependent(s)" + (" (truncated)" if rad.get("truncated") else ""))
        for d in deps[:5]:
            lines.append(f"      <- {d}")
        if len(deps) > 5:
            lines.append(f"      ... {len(deps) - 5} more")
    lines.append(f"Blast radius: {len(affected)} distinct dependent symbol(s) not changed or changed together")
    for r in res["z3"]:
        lines.append(r.format())
    for m in res["mined_violations"]:
        lines.append("ACCEPTED INVARIANT: " + m["text"])
    if not res["z3"] and not res["mined_violations"]:
        lines.append("Invariants: none touched (or none accepted/hand-written in tenthcheck.toml)")
    c = res.get("cache")
    if c:
        lines.append("Graph cache: base " + ("hit" if c["before_graph_hit"] else "miss")
                     + ", working tree " + ("hit" if c["after_graph_hit"] else "miss (re-parsed)"))
    tm = res["timing"]
    lines.append("Time: " + ", ".join(f"{k} {v * 1000:.0f} ms" for k, v in tm.items()))
    return "\n".join(lines)
