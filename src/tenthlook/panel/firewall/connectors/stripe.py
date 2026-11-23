"""Connector #2: Stripe, TEST MODE ONLY. Refunds and customer credits with dollar caps.

The constructor refuses any key that does not start with `sk_test_`, so a live key can never be used by accident.
Plain HTTPS through httpx (injectable transport for tests); the Idempotency-Key header is derived from Panel's key.
Amounts are decimal dollars with at most 2 places, converted to integer cents with Decimal (no float arithmetic).
"""
from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from tenthlook.panel.firewall.connector import Connector
from tenthlook.panel.firewall.policy import Policy
from tenthlook.panel.firewall.types import Action, ActionSchema, FirewallError, Result, sha256_hex

API = "https://api.stripe.com"
PI_RE = re.compile(r"^(pi|ch)_[A-Za-z0-9]{6,60}$")
CUS_RE = re.compile(r"^cus_[A-Za-z0-9]{6,60}$")


def load_key(env_file: str | Path = "~/.config/panel/stripe.env") -> str | None:
    if os.environ.get("STRIPE_API_KEY"):
        return os.environ["STRIPE_API_KEY"]
    p = Path(env_file).expanduser()
    if p.is_file():
        for line in p.read_text().splitlines():
            if line.startswith("STRIPE_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return None


def to_cents(v) -> int | None:
    """Exact cents for a number with <= 2 decimal places; None otherwise."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        d = Decimal(repr(v))
    except InvalidOperation:
        return None
    cents = d * 100
    return int(cents) if cents == cents.to_integral_value() and cents > 0 else None


class StripeConnector(Connector):
    name = "stripe"

    def __init__(self, api_key: str | None, base_url: str = API, transport: httpx.BaseTransport | None = None):
        if not isinstance(api_key, str) or not api_key.startswith("sk_test_"):
            raise FirewallError("Stripe connector refuses to start: the key must be a TEST key starting with sk_test_ "
                                "(live keys are never accepted)")
        self._key = api_key
        self.http = httpx.Client(base_url=base_url, transport=transport, timeout=20.0)

    def describe_actions(self) -> list[ActionSchema]:
        return [
            ActionSchema("stripe.refund", "Refund a Stripe payment (test mode). amount_usd is in dollars, max 2 decimals.",
                         {"payment_intent": {"type": "string", "description": "pi_... or ch_..."},
                          "amount_usd": {"type": "number"}, "reason": {"type": "string"}},
                         ("payment_intent", "amount_usd"), "write", ("refund", "refunded", "money back")),
            ActionSchema("stripe.customer_credit", "Add account credit to a Stripe customer (test mode).",
                         {"customer": {"type": "string", "description": "cus_..."}, "amount_usd": {"type": "number"},
                          "description": {"type": "string"}}, ("customer", "amount_usd"), "write",
                         ("credit", "credited", "account credit")),
        ]

    def facts(self, action: Action, policy: Policy) -> dict:
        a, block = action.args, []
        if to_cents(a.get("amount_usd")) is None:
            block.append("amount_usd must be a positive number with at most 2 decimal places")
        if action.tool == "stripe.refund" and not (isinstance(a.get("payment_intent"), str) and PI_RE.match(a["payment_intent"])):
            block.append("payment_intent must look like pi_... or ch_...")
        if action.tool == "stripe.customer_credit" and not (isinstance(a.get("customer"), str) and CUS_RE.match(a["customer"])):
            block.append("customer must look like cus_...")
        for k in ("reason", "description"):
            if k in a and not isinstance(a[k], str):
                block.append(f"{k} must be a string")
        return {"block_reasons": block}

    def execute(self, action: Action, mode: str) -> Result:
        a = action.args
        cents = to_cents(a["amount_usd"])
        if action.tool == "stripe.refund":
            path, form = "/v1/refunds", {"amount": cents, "currency": "usd"}
            form["payment_intent" if a["payment_intent"].startswith("pi_") else "charge"] = a["payment_intent"]
            if a.get("reason"):
                form["metadata[reason]"] = a["reason"][:200]
        else:
            path, form = f"/v1/customers/{a['customer']}/balance_transactions", {"amount": -cents, "currency": "usd"}
            if a.get("description"):
                form["description"] = a["description"][:200]
        if mode == "dry_run":
            return Result(True, None, {"would_post": path, "form": form}, simulated=True)
        try:
            r = self.http.post(path, data=form, auth=(self._key, ""),
                               headers={"Idempotency-Key": "panel-" + sha256_hex(action.key())[:40]})
        except httpx.HTTPError as e:
            return Result(False, error=f"Stripe request failed ({type(e).__name__})")
        try:
            body = r.json()
        except ValueError:
            return Result(False, error=f"Stripe returned HTTP {r.status_code} with a non-JSON body")
        if r.status_code >= 400:
            err = (body.get("error") or {})
            return Result(False, error=f"Stripe error {r.status_code}: {err.get('code') or err.get('type')}: {err.get('message')}"[:400])
        if body.get("livemode") is True:  # cannot happen with sk_test_, but never report success for a live object
            return Result(False, error="Stripe reported livemode=true; refusing")
        return Result(True, body.get("id"), {"id": body.get("id"), "status": body.get("status"), "amount": body.get("amount")})
