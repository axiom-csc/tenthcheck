"""Paired comparison of the with/without runs. n = 10 pairs is a demonstration, not a significance test.

    python analyze.py results/<run_id>/results.json [--json summary.json]
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import statistics
import sys
from math import comb


def sign_test_p(wins: int, losses: int) -> float:
    """Two-sided exact sign test, ties dropped."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def permutation_p(diffs: list[float]) -> float:
    """Two-sided exact paired permutation (sign-flip) test on the mean difference."""
    obs = abs(sum(diffs))
    if len(diffs) > 20:  # not expected here; fall back to sampling
        rng = random.Random(0)
        hits = sum(abs(sum(d * rng.choice((-1, 1)) for d in diffs)) >= obs - 1e-12 for _ in range(100_000))
        return hits / 100_000
    tot = hits = 0
    for signs in itertools.product((-1, 1), repeat=len(diffs)):
        tot += 1
        hits += abs(sum(d * s for d, s in zip(diffs, signs))) >= obs - 1e-12
    return hits / tot


def bootstrap_ci(diffs: list[float], b: int = 10_000, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(b))
    return means[int(0.025 * b)], means[int(0.975 * b) - 1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--json", help="also write the summary here")
    a = ap.parse_args()
    data = json.load(open(a.results))
    runs = {(r["task_id"], r["arm"]): r for r in data["runs"]}
    tasks = sorted({t for t, _ in runs})
    pairs = [t for t in tasks if (t, "with") in runs and (t, "without") in runs]
    if not pairs:
        sys.exit("no complete pairs yet")

    print(f"run {data['run_id']}  model {data['config']['model']}  Claude Code {data['config']['claude_code_version']}")
    print(f"baseline: {data['config']['baseline_passed']} passing tests; {len(pairs)} complete pairs\n")
    hdr = f"{'task':32s} {'broken w/o':>10s} {'broken with':>11s} {'diff':>5s}  {'done w/o':>8s} {'done with':>9s}  {'tc calls':>8s}  first"
    print(hdr)
    print("-" * len(hdr))
    diffs, rows = [], []
    for t in pairs:
        w, wo = runs[(t, "with")], runs[(t, "without")]
        d = wo["newly_broken"] - w["newly_broken"]  # positive = tenthcheck arm broke fewer tests
        diffs.append(d)
        first = "with" if w["order_in_pair"] == 1 else "without"
        rows.append({"task_id": t, "broken_without": wo["newly_broken"], "broken_with": w["newly_broken"],
                     "diff_without_minus_with": d, "done_without": wo["done_check_passed"],
                     "done_with": w["done_check_passed"], "tenthcheck_calls": w["tenthcheck_calls"], "first": first,
                     "touched_tests": bool(w["touched_tests"] or wo["touched_tests"])})
        print(f"{t:32s} {wo['newly_broken']:10d} {w['newly_broken']:11d} {d:5d}  {str(wo['done_check_passed']):>8s} "
              f"{str(w['done_check_passed']):>9s}  {w['tenthcheck_calls']:8d}  {first}")

    wins = sum(d > 0 for d in diffs)
    losses = sum(d < 0 for d in diffs)
    ties = sum(d == 0 for d in diffs)
    lo, hi = bootstrap_ci([float(d) for d in diffs])
    broken_any = {arm: sum(runs[(t, arm)]["newly_broken"] > 0 for t in pairs) for arm in ("with", "without")}
    done = {arm: sum(runs[(t, arm)]["done_check_passed"] for t in pairs) for arm in ("with", "without")}
    summary = {
        "pairs": len(pairs),
        "total_broken": {arm: sum(runs[(t, arm)]["newly_broken"] for t in pairs) for arm in ("with", "without")},
        "tasks_with_any_breakage": broken_any,
        "done_check_passed": done,
        "mean_diff_without_minus_with": statistics.fmean(diffs),
        "median_diff": statistics.median(diffs),
        "wins_ties_losses_for_with": [wins, ties, losses],
        "sign_test_p_two_sided": sign_test_p(wins, losses),
        "permutation_p_two_sided": permutation_p([float(d) for d in diffs]),
        "bootstrap_95ci_mean_diff": [lo, hi],
        "tenthcheck_calls_total": sum(runs[(t, "with")]["tenthcheck_calls"] for t in pairs),
        "with_arm_sessions_without_any_tenthcheck_call": sum(runs[(t, "with")]["tenthcheck_calls"] == 0 for t in pairs),
        "reported_cost_usd_total": sum((runs[k].get("reported_cost_usd") or 0) for k in runs),
        "rows": rows,
    }
    print(f"\nnewly broken tests, total: without {summary['total_broken']['without']}, with {summary['total_broken']['with']}")
    print(f"tasks that broke >= 1 test: without {broken_any['without']}/{len(pairs)}, with {broken_any['with']}/{len(pairs)}")
    print(f"done-check passed: without {done['without']}/{len(pairs)}, with {done['with']}/{len(pairs)}")
    print(f"per-task diff (without - with): mean {summary['mean_diff_without_minus_with']:.2f}, "
          f"median {summary['median_diff']}, bootstrap 95% CI [{lo:.2f}, {hi:.2f}]")
    print(f"with-arm better / tie / worse: {wins} / {ties} / {losses}; sign test p = {summary['sign_test_p_two_sided']:.3f}, "
          f"paired permutation p = {summary['permutation_p_two_sided']:.3f}")
    print(f"tenthcheck calls in the with arm: {summary['tenthcheck_calls_total']} "
          f"({summary['with_arm_sessions_without_any_tenthcheck_call']} with-arm sessions made none)")
    print("\nHow to read this: 10 pairs, one session per arm per task, on one repository and one model. Session-to-"
          "session variance in Claude Code is large, so a difference here is an anecdote with a direction, not "
          "evidence of an effect. Even a sign test of 10-0 only gives p ~ 0.002 and 8-2 gives p ~ 0.11; the "
          "numbers above are reported for transparency, not as significance claims. A tie (both 0) is the "
          "expected result for easy tasks. Report it as a demo.")
    if any(r["touched_tests"] for r in rows):
        print("NOTE: some sessions edited tests/ despite the rule; those edits were reverted before testing.")
    if a.json:
        json.dump(summary, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
