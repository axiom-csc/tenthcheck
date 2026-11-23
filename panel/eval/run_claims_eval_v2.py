"""Claim blocker v2 held-out evaluation. Every item's `calls` are replayed through the real with-Panel eval world (eval/run_eval.py's World/Arm,
policy_eval.yaml), so ledgers and receipts are real. An item is VALID only if the replay produced exactly `expect_executed` executed receipts for
its conversation (otherwise e.g. a read cited as an action, or a call Panel did not block); invalid items are excluded and counted.
Placeholders in text: {RID} first executed receipt, {BLOCKED_RID} first blocked receipt, {OTHER_RID} a receipt from another conversation.
Usage: python eval/run_claims_eval_v2.py --mode receipts|regex --out results/claims/<name>.json
Headline metric: false-rewrite rate = honest replies rewritten / valid honest replies."""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
import run_eval as RE  # noqa: E402
from tenthlook.panel.firewall.claims import _executed_receipts, check_reply  # noqa: E402
from tenthlook.panel.firewall.receipts import generate_key, receipt_id  # noqa: E402


def world(item, keydir: Path):
    tmp = Path(tempfile.mkdtemp(prefix="panel-v2-"))
    RE._KEY = generate_key(keydir / f"{item['id']}.pem")
    w = RE.World(tmp)
    arm = RE.Arm("with", w, tmp)
    outs = []
    inner = arm.fw.handle
    arm.fw.handle = lambda a: (outs.append((a.conversation_id, inner(a))) or outs[-1][1])
    conv = f"v2-{item['id']}"
    arm.call("other", {"tool": "shopify.refund", "args": {"order_id": "#1002", "amount": 20}})
    for c in item["calls"]:
        arm.call(conv, c)
    ids = {}
    for cid, o in outs:
        if cid == "other" and o.receipt and o.status == "executed":
            ids["{OTHER_RID}"] = receipt_id(o.receipt)
        if cid == conv and o.receipt and o.status == "blocked":
            ids.setdefault("{BLOCKED_RID}", receipt_id(o.receipt))
    good = _executed_receipts(arm.fw.ledger, conv, arm.fw.policy)
    if good:
        ids["{RID}"] = next(iter(good))
    return arm.fw, conv, ids, len(good)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--mode", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--data", default=str(ROOT / "eval/claims_heldout_v2.json"))
    a = ap.parse_args()
    data = json.loads(Path(a.data).read_text())
    keydir = Path(tempfile.mkdtemp(prefix="panel-v2-key-"))
    kw = {} if a.mode == "regex" else {"mode": a.mode}
    rows, invalid = [], []
    for it in data["items"]:
        fw, conv, ids, n_exec = world(it, keydir)
        if n_exec != it["expect_executed"]:
            invalid.append({"id": it["id"], "kind": it["kind"], "expected": it["expect_executed"], "got": n_exec})
            continue
        text = it["text"]
        for k, v in ids.items():
            text = text.replace(k, v)
        r = check_reply(text, fw.ledger, conv, fw.policy, **kw)
        rows.append({"id": it["id"], "kind": it["kind"], "source": it["source"], "text": text, "rewritten": r.rewritten})
    res = {"mode": a.mode, "data_sha256": hashlib.sha256(Path(a.data).read_bytes()).hexdigest(), "invalid": invalid, "summary": {}, "rows": rows}
    for kind in ("honest", "true_cited", "false", "attack"):
        for src in ("all", "model", "hand"):
            sub = [r for r in rows if r["kind"] == kind and (src == "all" or r["source"] == src)]
            res["summary"][f"{kind}/{src}"] = {"n": len(sub), "rewritten": sum(r["rewritten"] for r in sub)}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({"invalid": len(invalid), **res["summary"]}, indent=1))


if __name__ == "__main__":
    main()
