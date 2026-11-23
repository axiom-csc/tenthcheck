"""Runs the claim blocker over eval/claims_heldout_v1.json. No model. Usage:
  python eval/run_claims_eval.py --mode regex|receipts --out results/claims/<name>.json [--set heldout|old]
Scenario ledgers are built with the real Firewall (stripe mock + sqlite), so receipts are real signed receipts.
`regex` = the claim blocker as of the Phase 6 commit (check_reply default). `receipts` = receipts mode (added later)."""
import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tenthlook.core import receipt as core  # noqa: E402

from tenthlook.panel.firewall.claims import check_reply  # noqa: E402
from tenthlook.panel.firewall.connectors.sqlite_db import SqliteConnector  # noqa: E402
from tenthlook.panel.firewall.connectors.stripe import StripeConnector  # noqa: E402
from tenthlook.panel.firewall.engine import Firewall  # noqa: E402
from tenthlook.panel.firewall.ledger import Ledger  # noqa: E402
from tenthlook.panel.firewall.policy import parse_policy  # noqa: E402
from tenthlook.panel.firewall.receipts import Signer, generate_key  # noqa: E402
from tenthlook.panel.firewall.types import Action  # noqa: E402

CONV = "conv-1"


def rid(receipt: dict) -> str:                       # the receipt id formula (frozen): "rcpt_" + first 12 hex of the receipt hash
    return "rcpt_" + core.receipt_hash(receipt)[:12]


def build(scn: str, keydir: Path):
    db = keydir / f"{scn}.db"
    con = sqlite3.connect(db); con.execute("CREATE TABLE users(id INTEGER, n TEXT)")
    con.executemany("INSERT INTO users VALUES (?,?)", [(i, "x") for i in range(20)]); con.commit(); con.close()
    pol = parse_policy({"version": "c1", "mode": "auto", "actions": {
        "stripe.refund": {"amount_field": "amount_usd", "max_amount": 500, "consent_over_amount": 50},
        "sql.query": {"max_rows": 10, "consent_over_rows": 5}}, "sql": {"protected_tables": []}}, "0" * 64)
    stripe = StripeConnector("sk_test_x", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": "re_1", "livemode": False})))
    fw = Firewall(pol, Ledger(":memory:"), Signer.load(generate_key(keydir / f"{scn}.pem")), [SqliteConnector(db, pol), stripe])
    ids = {}
    o = fw.handle(Action("stripe.refund", {"payment_intent": "pi_3Abcdefghij", "amount_usd": 10}, "other-conv"))
    assert o.status == "executed"; ids["OTHER_RID"] = rid(o.receipt)
    if scn == "S1":
        o = fw.handle(Action("stripe.refund", {"payment_intent": "pi_3Abcdefghij", "amount_usd": 250}, CONV)); assert o.status == "needs_consent"
    elif scn == "S2":
        o = fw.handle(Action("sql.query", {"sql": "DELETE FROM users"}, CONV)); assert o.status == "blocked"
        ids["BLOCKED_RID"] = rid(o.receipt)
    elif scn in ("S3", "S4"):
        o = fw.handle(Action("stripe.refund", {"payment_intent": "pi_3Abcdefghij", "amount_usd": 10}, CONV)); assert o.status == "executed"
        ids["RID"] = ids["SCENARIO_RID"] = rid(o.receipt)
    return fw, ids


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--mode", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--data", default=str(ROOT / "eval/claims_heldout_v1.json"))
    a = ap.parse_args()
    data = json.loads(Path(a.data).read_text())
    keydir = Path(tempfile.mkdtemp(prefix="panel-claims-key-"))
    worlds = {s: build(s, keydir) for s in ("S0", "S1", "S2", "S3", "S4")}
    kw = {} if a.mode == "regex" else {"mode": a.mode}
    res = {"mode": a.mode, "data": a.data, "data_sha256": hashlib.sha256(Path(a.data).read_bytes()).hexdigest()}
    for group in ("false_claims", "nonclaims", "true_cited", "true_uncited", "citation_attacks"):
        rows = []
        for it in data[group]:
            fw, ids = worlds[it["scenario"]]
            text = it["text"].format(**ids) if "{" in it["text"] else it["text"]
            r = check_reply(text, fw.ledger, CONV, fw.policy, **kw)
            rows.append({"id": it["id"], "scenario": it["scenario"], "text": text, "rewritten": r.rewritten})
        res[group] = {"n": len(rows), "rewritten": sum(r["rewritten"] for r in rows), "rows": rows}
    fc = res["false_claims"]
    res["false_claims_by_scenario"] = {s: [sum(r["rewritten"] for r in fc["rows"] if r["scenario"] == s),
                                           sum(1 for r in fc["rows"] if r["scenario"] == s)] for s in ("S0", "S1", "S2", "S3")}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: (v if not isinstance(v, dict) else {x: v[x] for x in ("n", "rewritten") if x in v}) for k, v in res.items()
                      if k not in ("data", "data_sha256")}, indent=1))


if __name__ == "__main__":
    main()
