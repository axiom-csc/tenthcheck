"""Connector #1: Shopify (refunds and discount codes). Behaviour is the existing executor's:

validate = the generic rule engine + a fresh order read + the store's TOML policy re-check (same deterministic checker the
guard uses) + the test-order restriction; execute = tenthlook.panel.execute.executor.ShopifyBackend (same Shopify calls, same
Shopify-side idempotency key). The legacy support-bot path (src/tenthlook/panel/execute/executor.py + ActionStore) is untouched.
"""
from __future__ import annotations

from tenthlook.panel.execute.executor import CLAIM
from tenthlook.panel.firewall.connector import Connector
from tenthlook.panel.firewall.policy import Policy
from tenthlook.panel.firewall.types import Action, ActionSchema, Result, sha256_hex
from tenthlook.panel.guard.check import check_reply
from tenthlook.panel.guard.policy import Policy as StorePolicy


class ShopifyConnector(Connector):
    name = "shopify"

    def __init__(self, backend, orders, store_policy: StorePolicy | None = None, allow_live_orders: bool = False):
        self.backend, self.orders, self.store_policy, self.allow_live = backend, orders, store_policy, allow_live_orders

    def describe_actions(self) -> list[ActionSchema]:
        return [
            ActionSchema("shopify.refund", "Refund part or all of a Shopify order (amount in the shop currency).",
                         {"order_id": {"type": "string", "description": "e.g. #1001"},
                          "amount": {"type": "number", "description": "refund amount"}}, ("order_id", "amount"), "write",
                          ("refund", "refunded", "money back")),
            ActionSchema("shopify.discount", "Create a single-use percentage discount code for an order's customer.",
                         {"order_id": {"type": "string"}, "percent": {"type": "number", "description": "1-100"}},
                         ("order_id", "percent"), "write", ("discount", "coupon", "promo code")),
        ]

    def _order(self, order_id):
        try:
            return self.orders.get_order(order_id, fresh=True)
        except TypeError:
            return self.orders.get_order(order_id)

    def facts(self, action: Action, policy: Policy) -> dict:
        a, block = action.args, []
        oid = a.get("order_id")
        if not isinstance(oid, str) or not oid:
            return {"block_reasons": ["order_id must be a non-empty string"]}
        order = self._order(oid)
        if order is None:
            return {"block_reasons": [f"order {oid} not found"]}
        if not order.get("test") and not self.allow_live:
            block.append("not a Shopify test order (live orders need PANEL_ALLOW_LIVE_ORDERS)")
        if self.store_policy is not None:
            kind = "refund" if action.tool == "shopify.refund" else "discount"
            claim = {"type": CLAIM[kind], "status": "promise"}
            if kind == "refund" and isinstance(a.get("amount"), (int, float)):
                claim["amount"] = float(a["amount"])
            if kind == "discount" and isinstance(a.get("percent"), (int, float)):
                claim["percent"] = float(a["percent"])
            chk = check_reply([claim], self.store_policy, order, self.orders.today)
            if not chk["ok"]:
                block += [v["reason"] for v in chk["violations"]] + list(chk.get("unverified") or [])
        elif action.tool == "shopify.refund" and isinstance(a.get("amount"), (int, float)) and a["amount"] > order.get("amount", 0):
            block.append(f"amount {a['amount']} is more than the {order.get('amount')} still refundable")
        return {"block_reasons": block}

    def execute(self, action: Action, mode: str) -> Result:
        if mode == "dry_run":
            return Result(True, None, {"would": action.tool, "args": action.args}, simulated=True)
        order = self._order(action.args["order_id"])
        if order is None:
            return Result(False, error="order not found when executing")
        key = sha256_hex(action.key())[:32]
        kind = "refund" if action.tool == "shopify.refund" else "discount"
        payload = {"type": kind, "amount": action.args.get("amount"), "percent": action.args.get("percent")}
        out = self.backend.execute(payload, order, key)
        rid = out.get("refund_id") or out.get("discount_id") or out.get("id") or key
        return Result(True, str(rid), out)
