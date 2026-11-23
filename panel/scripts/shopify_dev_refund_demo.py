"""Step by step against YOUR Shopify development store: read an order, then make one small real test refund.

    python scripts/shopify_dev_refund_demo.py --order 1001 [--amount 1.00] [--discount] [--db /tmp/panel_demo.sqlite]

Run it in your own terminal (it asks you to type things). Credentials come from the environment or the secret file
(SHOPIFY.md); nothing secret is printed. Safety, in this order:
  1. the store must be a *.myshopify.com domain, and you type it back to confirm it's the dev store;
  2. the order must be a Shopify TEST order (placed with the Test payment gateway); live orders are refused;
  3. the refund goes through the approval flow: it is queued, shown to you, and runs only if you type "y";
  4. the same action is then submitted again to show it is not executed twice.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenthlook.panel.execute.executor import Executor, Limits, ShopifyBackend  # noqa: E402
from tenthlook.panel.execute.handoff import notifier_from_config  # noqa: E402
from tenthlook.panel.execute.store import ActionStore  # noqa: E402
from tenthlook.panel.guard.policy import load_policy  # noqa: E402
from tenthlook.panel.shopify import API_VERSION  # noqa: E402
from tenthlook.panel.shopify.client import ShopifyClient  # noqa: E402
from tenthlook.panel.shopify.config import Config  # noqa: E402
from tenthlook.panel.shopify.orders import ShopifyOrders  # noqa: E402

POLICY = ROOT / "guard_data/policy_fictional_store.toml"


def step(n, text):
    print(f"\n== Step {n}: {text}")


def show(rec):
    print(f"   key {rec.key[:12]}…  {rec.type} {rec.amount if rec.amount is not None else rec.percent}  "
          f"status={rec.status}  {rec.decision}  {rec.error or ''} {json.dumps(rec.result) if rec.result else ''}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", required=True, help="the test order's number, e.g. 1001")
    ap.add_argument("--amount", type=float, default=1.00, help="refund amount (default $1.00)")
    ap.add_argument("--discount", action="store_true", help="also create one 10%% single-use discount code")
    ap.add_argument("--db", default=str(Path(tempfile.gettempdir()) / "panel_shopify_demo.sqlite"))
    a = ap.parse_args()

    step(1, "configuration (names only, never values)")
    cfg = Config()
    print(f"   secret file: {cfg.secrets_file}  set: {', '.join(cfg.present())}")
    print(f"   store: {cfg.store}   API version: {API_VERSION}")
    if input(f"   Type the store domain to confirm it is your DEVELOPMENT store: ").strip() != cfg.store:
        sys.exit("   Not confirmed. Nothing was done.")

    step(2, "access token (client credentials grant)")
    client = ShopifyClient(cfg)
    client.tokens.token()
    print(f"   token obtained; scopes granted: {', '.join(client.tokens.scopes) or '(static token: scopes not reported)'}")
    extra = set(client.tokens.scopes) - {"write_orders", "write_discounts", "read_orders", "read_products"}
    if extra:
        print(f"   NOTE: the app has more scopes than Panel needs: {', '.join(sorted(extra))}. Consider removing them.")

    step(3, f"read order {a.order}")
    orders = ShopifyOrders(client)
    order = orders.get_order(a.order)
    if not order:
        sys.exit("   Order not found (orders older than 60 days need read_all_orders).")
    print("   " + json.dumps({k: v for k, v in order.items() if k != "transactions"}, default=str))
    if not order["test"]:
        sys.exit("   This is NOT a test order. The demo only refunds test orders. Nothing was done.")
    if a.amount > order["amount"]:
        sys.exit(f"   ${a.amount:.2f} is more than the ${order['amount']:.2f} still refundable. Nothing was done.")

    policy = load_policy(POLICY)
    store = ActionStore(a.db)
    ex = Executor(store, ShopifyBackend(client), orders, policy, Limits(auto_refund_max=0), notifier_from_config(cfg),
                  mode="dry_run", allow_live_orders=False)
    action = {"type": "refund", "amount": round(a.amount, 2)}
    conv = f"demo-{a.order}"
    # The executor runs only actions the customer asked for (v6.3 consent). In this demo you are the customer:
    asked = [{"role": "user", "content": f"Please refund ${a.amount:.2f} for order #{a.order}."}]
    print(f'   (demo customer message: "{asked[0]["content"]}")')

    step(4, "dry run (records what would happen, calls nothing)")
    show(ex.submit(conv, order, action, transcript=asked))

    step(5, "approval mode: queue the refund")
    ex.mode = "approval"
    rec = ex.submit(conv, order, action, transcript=asked)
    show(rec)
    if rec.status == "queued":
        if input(f"   Approve a REAL test refund of ${a.amount:.2f} on {order['order_id']}? [y/N] ").strip().lower() != "y":
            show(ex.reject(rec.key, who="demo user"))
            sys.exit("   Rejected. Nothing was refunded.")
        rec = ex.approve(rec.key, who="demo user")
        show(rec)
    if rec.status == "executed":
        after = orders.get_order(a.order, fresh=True)
        print(f"   Shopify now shows ${after['amount_refunded']:.2f} refunded on {after['order_id']} "
              f"(${after['amount']:.2f} still refundable). Check the order in your dev store admin.")

    step(6, "submit the same action again: never executed twice")
    again = ex.submit(conv, order, action, transcript=asked)
    show(again)
    print("   Same key, same record: Shopify was not called again." if again.key == rec.key else "   (different key?)")

    if a.discount:
        step(7, "a 10% single-use discount code (approval)")
        d = ex.submit(conv, order, {"type": "discount", "percent": 10},
                      transcript=[{"role": "user", "content": f"Could I get 10% off my next order? Order #{a.order}."}])
        show(d)
        if d.status == "queued" and input("   Approve creating the code? [y/N] ").strip().lower() == "y":
            show(ex.approve(d.key, who="demo user"))

    print(f"\nLedger: {a.db}. Open the approval page to see it: "
          f"python -m tenthlook.panel.execute.approval_page --db {a.db} --policy {POLICY}")


if __name__ == "__main__":
    main()
