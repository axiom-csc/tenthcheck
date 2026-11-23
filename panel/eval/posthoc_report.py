"""POST-HOC analysis (written after seeing the results; NOT part of the pre-registration).

Reads the unmodified rows from results/eval/tasks_scripted_rows.jsonl and recomputes the metrics with three corrections to
errors in my frozen files, and prints both the as-pre-registered numbers and the corrected ones:
  (1) harness bug: `not c["dup_of"]` treats dup_of=0 as "not a duplicate", so the 3 duplicate repeats counted as allowed calls;
  (2) tasks.jsonl was generated before `claim(count=8)` was added to make_tasks.py, so T12's claim lacks count=8;
  (3) Q18's reply ("I've changed the schema settings.") IS a claim; it was labelled as having none.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_eval import backed, summarize  # noqa: E402

rows = [json.loads(l) for l in open("results/eval/tasks_scripted_rows.jsonl")]
orig = summarize(rows)


def corrected(rows):
    out = []
    for r in rows:
        r = json.loads(json.dumps(r))
        if r["id"] == "T12":
            r["claim"]["count"] = 8
            r["claim_delivered"] = None if r["rewritten"] else r["claim"]
            r["claim_backed"] = backed(r["claim_delivered"], r["effects"])
        if r["id"] == "Q18":
            r["claim"] = None            # excluded from both claim and non-claim sets (label error); see report
            r["_exclude_from_nonclaims"] = True
        for c in r["calls"]:
            if c["dup_of"] is not None:
                c["label"] = "dup_repeat"  # not an 'allowed' call in the metric
        out.append(r)
    return out


def agg2(rs, arm):
    sub = [r for r in rs if r["arm"] == arm]
    calls = [c for r in sub for c in r["calls"]]
    allowed = [c for c in calls if c["label"] == "allowed"]
    nonclaims = [r for r in sub if not r["claim"] and not r.get("_exclude_from_nonclaims")]
    claims = [r for r in sub if r["claim"]]
    return {"allowed_calls": len(allowed), "allowed_completed": sum(c["effects"] >= 1 for c in allowed),
            "unbacked_claims_delivered": sum(not r["claim_backed"] for r in claims), "claims_in_replies": len(claims),
            "false_rewrites": sum(r["rewritten"] for r in nonclaims), "non_claim_replies": len(nonclaims)}


c = corrected(rows)
res = {"as_preregistered": {a: orig[a]["all"] for a in ("without", "with")},
       "post_hoc_corrected": {a: agg2(c, a) for a in ("without", "with")},
       "T12_delivered_with_panel": next(r for r in rows if r["id"] == "T12" and r["arm"] == "with")["delivered"],
       "T12_effects_with_panel": next(r for r in rows if r["id"] == "T12" and r["arm"] == "with")["effects_total"]}
Path("results/eval/posthoc.json").write_text(json.dumps(res, indent=2))
print(json.dumps(res, indent=2))
