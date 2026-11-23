"""`tenthcheck ci`: run on a pull request and write a summary (markdown) for a PR comment.

Wording rule: "checked" for what ran, "no counterexample found" for a Z3 pass, "candidate inconsistencies" for
mined patterns. Nothing here says "proven" or "bug".
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from tenthcheck import config as cfgmod
from tenthcheck import mining
from tenthcheck.enforce import describe_counterexample, describe_violation, enforce
from tenthcheck.impact import impact

MARKER = "<!-- tenthcheck-summary -->"


def _merge_base(root: Path, base: str) -> str | None:
    r = subprocess.run(["git", "-C", str(root), "merge-base", base, "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def run_ci(repo_dir: str, base: str | None = None) -> dict:
    t0 = time.perf_counter()
    root = Path(repo_dir).resolve()
    cfg = cfgmod.load(root)
    scan = mining.scan(root, cfg.min_support, cfg.min_consistency, exclude=cfg.exclude)
    enf = enforce(root, cfg, scan=scan)
    imp = None
    base_ref = None
    if base:
        base_ref = _merge_base(root, base) or base
        try:
            imp = impact(str(root), base_ref)
        except ValueError:
            imp = None
    changed = set(imp["files"]) if imp else set()
    cands = [(i, e) for i in scan.findings if i.id not in cfg.rejected() and i.id not in cfg.accepted()
             for e in i.exceptions if (not changed or e.file in changed)]
    blast: dict[str, int] = {}
    if imp and imp["changed"]:
        ch = imp["changed"]
        for eid in ch["changed_entities"]:
            rad = ch["blast_radii"].get(eid, {})
            deps = {d for depth, ids in rad.get("by_depth", {}).items() if int(depth) > 0 for d in ids}
            blast[eid] = len(deps)
    z3 = enf.z3_results
    status = {s: sum(1 for r in z3 if r.status == s) for s in ("holds", "violated", "not_checkable", "unknown")}
    summary = {"files_scanned": scan.files, "patterns_mined": len(scan.invariants),
               "accepted_mined_invariants": len(cfg.accepted()), "accepted_violations": len(enf.mined_violations),
               "handwritten_numeric_invariants": len(cfg.handwritten), "z3_no_counterexample": status["holds"],
               "z3_counterexamples": status["violated"], "z3_not_checkable": status["not_checkable"],
               "z3_unknown": status["unknown"], "candidates_unreviewed": len(cands),
               "changed_files": len(changed), "changed_symbols": len(blast),
               "dependents_of_changed": sum(blast.values()), "elapsed_seconds": round(time.perf_counter() - t0, 2)}
    return {"summary": summary, "enforcement": enf, "candidates": cands, "blast": blast, "impact": imp,
            "failed": enf.failed, "config_text": cfg.path.read_text() if cfg.path else None, "base": base_ref}


def markdown(res: dict, limit: int = 10) -> str:
    s, enf = res["summary"], res["enforcement"]
    L = [MARKER, "### Tenthcheck: checked", ""]
    L.append(f"Checked {s['files_scanned']} files in {s['elapsed_seconds']}s. "
             f"{s['accepted_mined_invariants']} accepted pattern(s) and {s['handwritten_numeric_invariants']} "
             f"hand-written numeric invariant(s) enforced.")
    L.append("")
    if enf.failed:
        L.append(f"**{s['accepted_violations']} accepted pattern violation(s), {s['z3_counterexamples']} counterexample(s):**")
        for r in enf.counterexamples[:limit]:
            L.append(f"- `{r.file}:{r.line}` `{r.function}()`: `{r.check}` can be violated. Counterexample: "
                     + ", ".join(f"`{k}={v}`" for k, v in r.counterexample.items())
                     + " → " + ", ".join(f"`{k}={v}`" for k, v in r.outcome.items()))
        for inv, site in enf.mined_violations[:limit]:
            L.append(f"- `{site}`: inconsistent with {inv.follow} other places that follow the accepted rule: {inv.pattern}")
    else:
        L.append("No violations of accepted invariants found.")
    L.append("")
    L.append("| Check | Result |\n|---|---|")
    L.append(f"| Z3 numeric invariants | {s['z3_no_counterexample']} no counterexample found, "
             f"{s['z3_counterexamples']} counterexample(s), {s['z3_not_checkable']} not checkable, {s['z3_unknown']} unknown |")
    L.append(f"| Accepted mined patterns | {s['accepted_violations']} violation(s) |")
    L.append(f"| Candidate inconsistencies (not enforced){' in changed files' if s['changed_files'] else ''} | {s['candidates_unreviewed']} |")
    if res["impact"]:
        L.append(f"| Changed | {s['changed_files']} file(s), {s['changed_symbols']} symbol(s), "
                 f"{s['dependents_of_changed']} dependent(s) |")
    L.append("")
    if res["blast"]:
        L.append("<details><summary>Blast radius (changed symbols → dependents)</summary>\n")
        for eid, n in sorted(res["blast"].items(), key=lambda x: -x[1])[:limit]:
            L.append(f"- `{eid}`: {n} dependent(s)")
        L.append("\n</details>\n")
    if res["candidates"]:
        L.append(f"<details><summary>{len(res['candidates'])} candidate inconsistencies (review; some are intentional)</summary>\n")
        for inv, e in res["candidates"][:limit]:
            L.append(f"- `{e}`: inconsistent with {inv.follow} other places: {inv.pattern}")
        L.append("\n</details>\n")
    nc = [r for r in enf.z3_results if r.status == "not_checkable"]
    if nc:
        L.append("<details><summary>Not checkable by Z3</summary>\n")
        for r in nc[:limit]:
            L.append(f"- `{r.function}()`: {r.reason}")
        L.append("\n</details>\n")
    L.append("_Candidates are patterns the code mostly follows, not proofs. Z3 results model numbers as exact "
             "reals/ints. A check shows what ran, not that the code is correct._")
    return "\n".join(L)
