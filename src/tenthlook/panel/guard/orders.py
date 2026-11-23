"""Order lookup. `JsonOrders` is a stub over a JSON file of FICTIONAL orders.

To use a real system, write a class with the same `get_order(order_id) -> dict | None` method and a `today`
attribute, returning the facts your policy's rules name (purchase_date as YYYY-MM-DD, amount, on_sale,
has_receipt, ...), and pass it to the guard instead.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Protocol

ORDER_ID = re.compile(r"\b([A-Z]\d{3,6})\b")


class OrderLookup(Protocol):
    today: date

    def get_order(self, order_id: str) -> dict | None: ...


class JsonOrders:
    def __init__(self, path: str | Path, today: date | None = None):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self._orders = data["orders"]
        self.today = today or date.fromisoformat(data.get("today") or date.today().isoformat())

    def get_order(self, order_id: str) -> dict | None:
        o = self._orders.get(order_id)
        return None if o is None else {"order_id": order_id, **o}


def find_order_id(*texts: str, pattern: re.Pattern = ORDER_ID) -> str | None:
    """Last order id mentioned anywhere in the conversation (customer or bot). A lookup can supply its own pattern
    (ShopifyOrders.order_id_pattern matches "#1001" / "order 1001")."""
    found = None
    for t in texts:
        for m in pattern.finditer(t or ""):
            found = m.group(1)
    return found
