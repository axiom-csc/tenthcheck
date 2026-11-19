"""Efficiency table (PLAN_EFFICIENCY.md): per task and arm, medians over repeats.

    python analyze_efficiency.py <pristine rich checkout> results/<run_id> [results/<run_id> ...] [--json out.json]

For every session in the given runs: tokens processed and output tokens, reported cost, search calls (Grep + Glob +
Read), characters returned by tools, edit calls, tenthcheck MCP calls, hook messages fed back, model time, turns,
newly broken tests (scored), done-check, and missed callers from caller_check.py (independent of Tenthcheck).
Then per (model, task, arm): median and range; per (model, task): ratio of each arm's median to the without arm's.
No significance tests (see PLAN_EFFICIENCY.md: with 2-3 sessions per cell only large, consistent differences count).
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

from caller_check import RULES, check
from efficiency_metrics import metrics

MEASURES = ["processed_tokens", "output_tokens", "reported_cost_usd", "search_calls", "tool_result_chars",
            "edit_calls", "tenthcheck_calls", "hook_feedback", "model_s", "num_turns", "newly_broken", "missed_callers"]


def sessions(pristine: Path, run_dirs: list[Path]) -> list[dict]:
    rows = []
    for rd in run_dirs:
        data = json.loads((rd / "results.json").read_text())
        model = data["config"]["model"]
        multi = data["config"].get("repeats", 1) > 1
        for r in data["runs"]:
            tag = f"{r['task_id']}__{r['arm']}" + (f"__r{r.get('rep', 0)}" if multi else "")
            m = metrics(rd / tag)
            missed = None
            if r["task_id"] in RULES:
                cc = check(r["task_id"], pristine, rd / tag / "agent.diff")
                missed = cc["missed"]
            rows.append({"run": rd.name, "model": model, "task": r["task_id"], "arm": r["arm"], "rep": r.get("rep", 0),
                         "processed_tokens": m["tokens"]["processed"], "output_tokens": m["tokens"]["output"],
                         "reported_cost_usd": m["reported_cost_usd"], "search_calls": m["search_calls"],
                         "tool_result_chars": m["tool_result_chars_total"], "edit_calls": m["edit_calls"],
                         "tenthcheck_calls": m["tenthcheck_calls"], "hook_feedback": r.get("hook_feedback", 0),
                         "model_s": m["model_s"], "num_turns": m["num_turns"], "newly_broken": r["newly_broken"],
                         "done_check": r["done_check_passed"], "missed_callers": missed})
    return rows


def table(rows: list[dict]) -> dict:
    cells: dict[tuple, list[dict]] = {}
    for r in rows:
        cells.setdefault((r["model"], r["task"], r["arm"]), []).append(r)
    out = {}
    for key, rs in sorted(cells.items()):
        cell = {"n": len(rs)}
        for m in MEASURES:
            vals = [r[m] for r in rs if r[m] is not None]
            cell[m] = {"median": st.median(vals), "min": min(vals), "max": max(vals)} if vals else None
        out["|".join(key)] = cell
    ratios = {}
    for (model, task, arm) in cells:
        base = out.get(f"{model}|{task}|without")
        if arm == "without" or not base:
            continue
        cell = out[f"{model}|{task}|{arm}"]
        ratios[f"{model}|{task}|{arm}"] = {
            m: round(cell[m]["median"] / base[m]["median"], 2)
            for m in ("processed_tokens", "output_tokens", "search_calls", "tool_result_chars", "model_s")
            if cell[m] and base[m] and base[m]["median"]}
    return {"cells": out, "ratios_vs_without": ratios}


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--json"]
    out_json = None
    if "--json" in sys.argv:
        out_json = sys.argv[sys.argv.index("--json") + 1]
        args.remove(out_json)
    pristine, runs = Path(args[0]).expanduser().resolve(), [Path(a) for a in args[1:]]
    rows = sessions(pristine, runs)
    t = table(rows)
    print(f"{'model|task|arm':58s} {'n':>2s} {'tokens':>9s} {'out':>6s} {'search':>6s} {'chars':>7s} {'tc':>3s} "
          f"{'hook':>4s} {'model s':>7s} {'missed':>6s} {'broken':>6s}")
    for k, c in t["cells"].items():
        f = lambda m, fmt="{:.0f}": fmt.format(c[m]["median"]) if c[m] else "-"
        print(f"{k:58s} {c['n']:2d} {f('processed_tokens'):>9s} {f('output_tokens'):>6s} {f('search_calls'):>6s} "
              f"{f('tool_result_chars'):>7s} {f('tenthcheck_calls'):>3s} {f('hook_feedback'):>4s} "
              f"{f('model_s', '{:.1f}'):>7s} {f('missed_callers'):>6s} {f('newly_broken'):>6s}")
    if t["ratios_vs_without"]:
        print("\nratio of medians vs without:")
        for k, r in t["ratios_vs_without"].items():
            print(f"  {k}: " + ", ".join(f"{m} {v}" for m, v in r.items()))
    if out_json:
        Path(out_json).write_text(json.dumps({"sessions": rows, **t}, indent=1))


if __name__ == "__main__":
    main()
