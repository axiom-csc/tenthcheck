"""Score `blast` hop 1 against hand-labelled true callers.

    python scripts/accuracy_eval.py ~/tenthcheck-corpus [--out results/accuracy_eval.json]

Sets: httpx (results/accuracy_httpx.json; used to design the parser fixes = TUNED-ON) and click
(results/accuracy_click/truth.json; labelled before the fixes, then used for the overload/super fixes = now TUNED-ON),
starlette (results/accuracy_starlette/truth.json; labelled 2026-11-17 after all fixes = HELD OUT, measured once).
"""
import json
import sys
from pathlib import Path

from tenthcheck.parser import parse_package
from tenthcheck.topology import dependents_radius

ROOT = Path(__file__).resolve().parents[1]


def score(name, root, prefix, truth):
    g = parse_package(str(root), skip_syntax_errors=True)
    tp = fn = fp = 0
    rows = []
    for sym, want in truth.items():
        if sym not in g.entities:
            raise SystemExit(f"{name}: {sym} not in graph")
        got = {d for d in dependents_radius(g, sym, max_depth=1)["by_depth"].get(1, []) if d.startswith(prefix + ".")}
        want = set(want)
        tp += len(got & want); fn += len(want - got); fp += len(got - want)
        rows.append({"symbol": sym, "true": len(want), "found": sorted(got & want), "missed": sorted(want - got),
                     "false_positives": sorted(got - want)})
    return {"set": name, "symbols": len(truth), "true_callers": tp + fn, "found": tp, "missed": fn, "false_positives": fp,
            "recall": round(tp / (tp + fn), 4), "rows": rows}


def main():
    corpus = Path(sys.argv[1]).expanduser()
    httpx = json.loads((ROOT / "results/accuracy_httpx.json").read_text())
    click = json.loads((ROOT / "results/accuracy_click/truth.json").read_text())
    starlette = json.loads((ROOT / "results/accuracy_starlette/truth.json").read_text())
    res = [score("httpx (tuned-on)", corpus / "httpx", "httpx", {r["symbol"]: r["true_callers"] for r in httpx["rows"]}),
           score("click (tuned-on since the overload fixes)", corpus / "click/src", "click",
                 {k: v["callers"] for k, v in click["symbols"].items()}),
           score("starlette (held out)", corpus / "starlette", "starlette",
                 {k: v["callers"] for k, v in starlette["symbols"].items()})]
    for r in res:
        print(f"{r['set']}: found {r['found']}/{r['true_callers']} (recall {r['recall']:.0%}), false positives {r['false_positives']}")
        for row in r["rows"]:
            if row["missed"] or row["false_positives"]:
                print("   ", row["symbol"], "missed", row["missed"], "FP", row["false_positives"])
    if "--out" in sys.argv:
        Path(sys.argv[sys.argv.index("--out") + 1]).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
