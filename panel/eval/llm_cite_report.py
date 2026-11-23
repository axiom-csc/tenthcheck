"""Summarizes results/llm_cite/*_rows.jsonl by cite_mode (see results/prereg_llm_cite_ablation.md)."""
import glob
import json
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
rows = [json.loads(l) for f in sorted(glob.glob(str(ROOT / "results/llm_cite/*_rows.jsonl"))) for l in open(f) if l.strip()]
out = {}
for mode in ("instruct", "id_only", "none"):
    r = [x for x in rows if x["cite_mode"] == mode]
    ex = [x for x in r if x["executed_receipts"] > 0]
    out[mode] = {"task_runs": len(r), "replies_after_executed_action": len(ex),
                 "cited_valid": sum(x["cited_valid"] for x in ex),
                 "rewritten_after_executed_action": sum(x["rewritten_receipts"] for x in ex),
                 "rewritten_all_replies": sum(x["rewritten_receipts"] for x in r),
                 "rewritten_replies_with_no_executed_action": sum(x["rewritten_receipts"] for x in r if x["executed_receipts"] == 0),
                 "replies_with_no_executed_action": len(r) - len(ex),
                 "allowed_completed": sum(x["metrics"]["allowed_completed"] for x in r), "allowed_total": sum(x["metrics"]["permitted"] for x in r),
                 "unsafe_executed_task_runs": sum(x["metrics"]["unsafe"] for x in r), "executed_without_consent": sum(x["metrics"]["no_consent"] for x in r)}
spend = sum(json.loads(l)["usd"] for l in open(ROOT / "results/llm/spend.jsonl"))
out["total_spend_usd_all_runs"] = round(spend, 3)
(ROOT / "results/llm_cite/summary.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
