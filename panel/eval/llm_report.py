"""Aggregates results/llm/*_rows.jsonl into results/llm/summary.json and prints a table; draws the 30-reply audit sample
(random.Random(20261123), as pre-registered). Usage: python eval/llm_report.py [--audit-labels results/llm/audit_labels.json]"""
import argparse
import glob
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tenthlook.panel.firewall.claims import find_assertions  # noqa: E402


def load():
    rows = []
    for f in sorted(glob.glob(str(ROOT / "results/llm/*_rows.jsonl"))):
        rows += [json.loads(l) for l in open(f) if l.strip()]
    return rows


def agg(rows):
    n = len(rows)
    g = lambda k: sum(r["metrics"][k] for r in rows)
    cats = lambda *c: [r for r in rows if r["category"] in c]
    blockish = [r for r in rows if r["metrics"].get("unsafe") is not None]
    out = {
        "task_runs": n,
        "unsafe_executed_task_runs": sum(r["metrics"]["unsafe"] for r in rows),
        "executed_without_consent_task_runs": sum(r["metrics"]["no_consent"] for r in rows),
        "duplicate_extra": g("duplicate_extra"), "unrequested_effects": g("unrequested"),
        "allowed_completed": g("allowed_completed"), "allowed_total": g("permitted"),
        "replies_asserting_action": sum(r["raw_asserts"] for r in rows),
        "unbacked_claims_raw": sum(r["raw_unbacked"] for r in rows),
        "tokens_in": sum(r["tokens"][0] for r in rows), "tokens_out": sum(r["tokens"][1] for r in rows),
    }
    w = [r for r in rows if r["arm"] == "with"]
    if w:
        out["unbacked_delivered_receipts"] = sum(r["unbacked_delivered_receipts"] for r in w)
        out["unbacked_delivered_regex"] = sum(r["unbacked_delivered_regex"] for r in w)
        ex = [r for r in w if r["executed_receipts"] > 0]
        out["replies_after_executed_action"] = len(ex)
        out["cited_valid_receipt"] = sum(r["cited_valid"] for r in ex)
        out["rewritten_receipts_after_executed_action"] = sum(r["rewritten_receipts"] for r in ex)
        out["rewritten_regex_after_executed_action"] = sum(r["rewritten_regex"] for r in ex)
        nc = [r for r in w if not r["raw_asserts"]]
        out["rewritten_receipts_when_no_assertion"] = sum(r["rewritten_receipts"] for r in nc)
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--audit-labels")
    a = ap.parse_args()
    rows = load()
    summ = {}
    for model in sorted({r["model"] for r in rows}):
        for arm in ("without", "with"):
            sub = [r for r in rows if r["model"] == model and r["arm"] == arm]
            if sub:
                summ[f"{model}/{arm}"] = agg(sub)
    rows_sorted = sorted(rows, key=lambda r: (r["model"], r["id"], r["arm"], r["sample"]))
    sample = random.Random(20261123).sample(rows_sorted, 30)
    audit = [{"k": i, "model": r["model"], "id": r["id"], "arm": r["arm"], "sample": r["sample"], "reply": r["reply"],
              "n_effects": len([e for e in r["effects"] if e["family"] != "select"]), "detector_asserts": r["raw_asserts"]}
             for i, r in enumerate(sample)]
    (ROOT / "results/llm/audit_sample.json").write_text(json.dumps(audit, indent=1))
    if a.audit_labels:
        lab = json.loads(Path(a.audit_labels).read_text())
        agree = sum(1 for x in audit if bool(lab[str(x["k"])]) == x["detector_asserts"])
        summ["audit"] = {"n": 30, "agree": agree, "hand_asserts": sum(bool(v) for v in lab.values()),
                         "detector_asserts": sum(x["detector_asserts"] for x in audit),
                         "disagreements": [x["k"] for x in audit if bool(lab[str(x["k"])]) != x["detector_asserts"]]}
    spend = [json.loads(l) for l in open(ROOT / "results/llm/spend.jsonl") if l.strip()]
    summ["spend_usd_with_margin"] = round(sum(s["usd"] for s in spend), 4)
    (ROOT / "results/llm/summary.json").write_text(json.dumps(summ, indent=1))
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
