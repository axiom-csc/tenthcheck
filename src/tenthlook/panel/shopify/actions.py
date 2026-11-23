"""The two Shopify writes Panel makes: a money refund and a single-use discount code.

refundCreate requires an idempotency key in 2026-10 (the @idempotent directive; Shopify keeps keys 24 hours). The
executor derives it from Panel's own action key, so a retry of the same action can never refund twice inside that
window; Panel's own ledger (src/tenthlook/panel/execute/store.py) enforces never-twice beyond it.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from tenthlook.panel.shopify.client import ShopifyClient, ShopifyError

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PANEL_NS = uuid.UUID("8f0a6c1e-6b5f-4f3c-9d8e-6a1b2c3d4e5f")

REFUND = """mutation PanelRefund($input: RefundInput!) {
  refundCreate(input: $input) @idempotent(key: "%s") {
    refund { id totalRefundedSet { shopMoney { amount currencyCode } } }
    userErrors { field message }
  }
}"""
DISCOUNT = """mutation PanelDiscount($d: DiscountCodeBasicInput!) {
  discountCodeBasicCreate(basicCodeDiscount: $d) {
    codeDiscountNode { id }
    userErrors { field message }
  }
}"""


def shopify_key(panel_key: str) -> str:
    return str(uuid.uuid5(PANEL_NS, panel_key))


def _amount(t: dict) -> float:
    return float(((t.get("amountSet") or {}).get("shopMoney") or {}).get("amount") or 0)


def refundable_on(t: dict, transactions: list[dict]) -> float:
    """What is left to refund on a SALE/CAPTURE: its amount minus the successful refunds whose parent it is.

    maximumRefundableV2 is "only available for transactions of type SuggestedRefund" (Admin API 2026-10), so on a real
    SALE or CAPTURE it is null; it is used only when Shopify does send it."""
    reported = (t.get("maximumRefundableV2") or {}).get("amount")
    if reported is not None:
        return float(reported)
    refunded = sum(_amount(r) for r in transactions
                   if r.get("kind") == "REFUND" and r.get("status") == "SUCCESS"
                   and (r.get("parentTransaction") or {}).get("id") == t.get("id"))
    return max(0.0, _amount(t) - refunded)


def refund_transaction(order: dict, amount: float) -> dict:
    """The successful SALE or CAPTURE transaction a refund of `amount` is made against (its parentId).

    For authorize-then-capture orders that is the CAPTURE, which holds the captured funds; an AUTHORIZATION is never a
    refund parent (a refund "can happen only after a capture is processed")."""
    txs = order.get("transactions") or []
    for t in txs:
        if t.get("kind") in ("SALE", "CAPTURE") and t.get("status") == "SUCCESS":
            if refundable_on(t, txs) + 1e-9 >= amount:
                return t
    raise ShopifyError(f"no single successful payment on {order.get('order_id')} can cover a ${amount:.2f} refund")


def create_refund(client: ShopifyClient, order: dict, amount: float, idem_key: str, note: str) -> dict:
    if not UUID_RE.match(idem_key):
        raise ValueError("idempotency key must be a UUID")
    parent = refund_transaction(order, amount)
    data = client.graphql(REFUND % idem_key, {"input": {
        "orderId": order["shopify_id"], "notify": True, "note": note,
        "transactions": [{"orderId": order["shopify_id"], "parentId": parent["id"], "amount": f"{amount:.2f}",
                          "gateway": parent.get("gateway"), "kind": "REFUND"}]}}, "refundCreate")
    out = data.get("refundCreate") or {}
    if out.get("userErrors"):
        raise ShopifyError("refundCreate: " + "; ".join(e.get("message", "") for e in out["userErrors"]), out["userErrors"])
    return {"refund_id": (out.get("refund") or {}).get("id"),
            "refunded": ((out.get("refund") or {}).get("totalRefundedSet") or {}).get("shopMoney")}


def create_discount_code(client: ShopifyClient, order: dict, percent: float, code: str, days_valid: int = 90,
                         now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    data = client.graphql(DISCOUNT, {"d": {
        "title": f"Panel {percent:g}% for order {order.get('order_id')}", "code": code,
        "startsAt": now.isoformat(), "endsAt": (now + timedelta(days=days_valid)).isoformat(),
        "context": {"all": "ALL"},
        "customerGets": {"value": {"percentage": round(percent / 100.0, 4)}, "items": {"all": True}},
        "usageLimit": 1, "appliesOncePerCustomer": True}}, "discountCodeBasicCreate")
    out = data.get("discountCodeBasicCreate") or {}
    if out.get("userErrors"):
        raise ShopifyError("discountCodeBasicCreate: " + "; ".join(e.get("message", "") for e in out["userErrors"]),
                           out["userErrors"])
    return {"discount_id": (out.get("codeDiscountNode") or {}).get("id"), "code": code}
