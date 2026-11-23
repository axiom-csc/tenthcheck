"""Shopify-backed order lookup behind Panel's OrderLookup interface (get_order(order_id) -> dict | None, today).

The fake JsonOrders (src/tenthlook/panel/guard/orders.py) stays for tests and the measured runs.

Facts returned (what the policy rules name), from the Admin GraphQL Order object:
- order_id: the order name as Shopify shows it, e.g. "#1001"
- purchase_date: processedAt (UTC date)
- amount: what can still be refunded: totalPriceSet - totalRefundedSet (shop currency). The policy's "never more
  than paid" rule therefore also stops a second refund after a partial one. amount_paid keeps the original total.
- on_sale: true if any line item's unit price is below its variant's compareAtPrice. That is the variant's CURRENT
  compare-at price, not necessarily the one at purchase time (Shopify doesn't store it on the order); a store that
  marks sales differently should override `is_on_sale`.
- has_receipt: true (an online order always has its order confirmation)
- item: the first line item (+ "and N more")
- test, shopify_id, currency, transactions: used by the executor (test-orders-only guard, refunds)
Orders older than 60 days are not visible with write_orders alone (Shopify needs read_all_orders); they come back
as None, so the guard treats them as unknown and won't commit to anything.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone

from tenthlook.panel.shopify.client import ShopifyClient

# "order #1001", "#1001", "order 1001", "order number 1001". A bare number is never taken as an order id.
SHOPIFY_ORDER_ID = re.compile(r"(?:#|\border\s*(?:number|no\.?|num\.?)?\s*#?\s*)(\d{3,10})\b", re.I)

ORDER_FIELDS = """
  id name test processedAt createdAt cancelledAt displayFinancialStatus
  totalPriceSet { shopMoney { amount currencyCode } }
  totalRefundedSet { shopMoney { amount currencyCode } }
  lineItems(first: 20) { nodes { name quantity originalUnitPriceSet { shopMoney { amount } } variant { compareAtPrice } } }
  transactions(first: 20) { id kind status gateway test amountSet { shopMoney { amount currencyCode } }
                            parentTransaction { id } maximumRefundableV2 { amount currencyCode } }
"""
ORDER_BY_NAME = "query OrderByName($q: String!) { orders(first: 1, query: $q) { nodes {" + ORDER_FIELDS + "} } }"


def _money(m) -> float:
    return float(((m or {}).get("shopMoney") or m or {}).get("amount") or 0)


def is_on_sale(order: dict) -> bool:
    for li in (order.get("lineItems") or {}).get("nodes") or []:
        cap = (li.get("variant") or {}).get("compareAtPrice")
        if cap is not None and float(cap) > _money(li.get("originalUnitPriceSet")) + 1e-9:
            return True
    return False


def to_facts(o: dict) -> dict:
    items = (o.get("lineItems") or {}).get("nodes") or []
    item = items[0]["name"] if items else "item"
    if len(items) > 1:
        item += f" and {len(items) - 1} more"
    paid, refunded = _money(o.get("totalPriceSet")), _money(o.get("totalRefundedSet"))
    when = (o.get("processedAt") or o.get("createdAt") or "")[:10]
    return {"order_id": o["name"], "purchase_date": when, "amount": round(max(0.0, paid - refunded), 2),
            "amount_paid": paid, "amount_refunded": refunded, "on_sale": is_on_sale(o), "has_receipt": True,
            "item": item, "test": bool(o.get("test")), "cancelled": bool(o.get("cancelledAt")),
            "shopify_id": o["id"], "currency": ((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("currencyCode"),
            "transactions": o.get("transactions") or []}


class ShopifyOrders:
    order_id_pattern = SHOPIFY_ORDER_ID

    def __init__(self, client: ShopifyClient, today: date | None = None, cache_s: float = 60.0, clock=time.time):
        self.client, self._today, self.cache_s, self.clock = client, today, cache_s, clock
        self._cache: dict[str, tuple[float, dict | None]] = {}

    @property
    def today(self) -> date:
        return self._today or datetime.now(timezone.utc).date()

    def get_order(self, order_id: str, fresh: bool = False) -> dict | None:
        digits = re.sub(r"\D", "", order_id or "")
        if not digits:
            return None
        hit = self._cache.get(digits)
        if hit and not fresh and self.clock() - hit[0] < self.cache_s:
            return hit[1]
        found = None
        for q in (f"name:#{digits}", f"name:{digits}"):  # docs don't say whether the filter wants the '#'
            nodes = (self.client.graphql(ORDER_BY_NAME, {"q": q}, "order lookup").get("orders") or {}).get("nodes") or []
            exact = [n for n in nodes if re.sub(r"\D", "", n.get("name", "")) == digits]
            if exact:
                found = to_facts(exact[0])
                break
        self._cache[digits] = (self.clock(), found)
        return found
