"""Policy setup form backend: turn a business's answers into a valid policy file plus a plain-English summary.

build(fields) -> {"ok", "errors": {field: message}, "warnings": [...], "toml", "summary": [...], "rules"}
The TOML is loaded back with load_policy before it is returned, so "ok" means the guard will accept the file.
Rules only use the order facts the guard knows about: purchase_date, amount, on_sale, has_receipt.
"""
from __future__ import annotations

import json
import tempfile

from tenthlook.panel.guard.policy import PolicyError, load_policy

DEFAULT_FALLBACK = ("I'm not able to help with that request myself, but I can connect you with a member of our team "
                    "who can review it. Would you like me to do that?")
DEFAULTS = {"store_name": "", "fallback_reply": DEFAULT_FALLBACK, "unverified": "block",
            "refunds_allowed": True, "refund_window_days": 30, "refund_max_amount": "", "refund_not_over_paid": True,
            "refund_sale_items": False, "escalate_over": "",
            "discount_max_percent": 10,
            "exchanges_allowed": True, "exchange_window_days": 60, "exchange_needs_receipt": True, "exchange_sale_items": True,
            "no_exceptions": True, "no_compensation": True, "no_legal_admissions": True}


def _q(s: str) -> str:
    return json.dumps(s)  # JSON string escapes are valid TOML basic-string escapes


def _money(x: float) -> str:
    return f"${x:,.2f}".replace(".00", "")


def _bool(v) -> bool:
    return v in (True, "true", "on", "1", 1, "yes")


def build(fields: dict) -> dict:
    f = {**DEFAULTS, **{k: v for k, v in (fields or {}).items() if k in DEFAULTS}}
    errors: dict[str, str] = {}
    warnings: list[str] = []

    def text(k, maxlen):
        v = str(f[k] or "").strip()
        if not v:
            errors[k] = "Required."
        elif len(v) > maxlen:
            errors[k] = f"Keep it under {maxlen} characters."
        return v

    def integer(k, lo, hi, label):
        try:
            v = float(f[k])
            if v != int(v):
                raise ValueError
            v = int(v)
        except (TypeError, ValueError):
            errors[k] = f"{label} must be a whole number."
            return None
        if not lo <= v <= hi:
            errors[k] = f"{label} must be between {lo} and {hi}."
            return None
        return v

    def amount(k, label):
        raw = str(f[k] if f[k] is not None else "").strip().lstrip("$").replace(",", "")
        if raw == "":
            return None
        try:
            v = float(raw)
        except ValueError:
            errors[k] = f"{label} must be a number of dollars, or blank for none."
            return None
        if not 0 < v <= 1_000_000:
            errors[k] = f"{label} must be more than $0."
            return None
        return round(v, 2)

    name = text("store_name", 80)
    fallback = text("fallback_reply", 400)
    if f["unverified"] not in ("block", "allow"):
        errors["unverified"] = "Choose block or allow."
    refunds, exchanges = _bool(f["refunds_allowed"]), _bool(f["exchanges_allowed"])
    rules: list[dict] = []

    def rule(id_, kind, applies_to, description, **params):
        rules.append({"id": id_, "kind": kind, "applies_to": applies_to, **params, "description": description})

    if refunds:
        d = integer("refund_window_days", 1, 365, "Refund window")
        mx = amount("refund_max_amount", "Largest refund")
        esc = amount("escalate_over", "Escalation threshold")
        if d:
            rule("refund-window", "time_window", "refund", f"Refunds are only available within {d} days of purchase.",
                 days=d, fact="purchase_date")
        if not _bool(f["refund_sale_items"]):
            rule("refund-not-on-sale", "ineligible_if", "refund",
                 "Items bought on sale are final sale and cannot be refunded.", fact="on_sale", equals=True)
        if _bool(f["refund_not_over_paid"]):
            rule("refund-not-more-than-paid", "max_of_fact", "refund",
                 "A refund can never be more than the customer paid for the order.", fact="amount")
        if mx is not None:
            rule("refund-max-amount", "max_amount", "refund", f"A single refund can be at most {_money(mx)}.", amount=mx)
        if esc is not None:
            rule("refund-escalate-over", "escalate_over", "refund",
                 f"Refunds over {_money(esc)} must be handed to a human; the assistant may not approve them itself.", amount=esc)
            if mx is not None and esc >= mx:
                warnings.append(f"The escalation threshold ({_money(esc)}) is at or above the largest refund "
                                f"({_money(mx)}), so no refund will ever be escalated.")
    else:
        rule("no-refunds", "never", "refund", "The assistant may not offer refunds.")

    p = integer("discount_max_percent", 0, 100, "Largest discount")
    if p == 0:
        rule("no-discounts", "never", "discount", "The assistant may not offer discounts.")
    elif p is not None:
        rule("discount-max", "max_percent", "discount", f"The assistant may offer at most {p}% off.", percent=p)
        if p > 50:
            warnings.append(f"A {p}% maximum discount is unusually high; check it's what you mean.")

    if exchanges:
        d = integer("exchange_window_days", 1, 365, "Exchange window")
        if _bool(f["exchange_needs_receipt"]):
            rule("exchange-needs-receipt", "requires_fact", "exchange", "Exchanges require a receipt.",
                 fact="has_receipt", equals=True)
        if d:
            rule("exchange-window", "time_window", "exchange", f"Exchanges are only available within {d} days of purchase.",
                 days=d, fact="purchase_date")
        if not _bool(f["exchange_sale_items"]):
            rule("exchange-not-on-sale", "ineligible_if", "exchange", "Items bought on sale cannot be exchanged.",
                 fact="on_sale", equals=True)
    else:
        rule("no-exchanges", "never", "exchange", "The assistant may not offer exchanges.")

    if _bool(f["no_exceptions"]):
        rule("no-exceptions", "never", "exception", "The assistant may not make exceptions to or waive any policy.")
    if _bool(f["no_compensation"]):
        rule("no-compensation", "never", "compensation",
             "The assistant may not offer compensation: no cash, gift cards, vouchers, goodwill credit or free items.")
    if _bool(f["no_legal_admissions"]):
        rule("no-legal-admissions", "never", "legal_admission", "The assistant may not admit fault or legal liability.")

    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "toml": "", "summary": [], "rules": 0}

    lines = ["# Policy generated by the Panel Guard policy setup form. Edit freely; the guard enforces exactly this file.",
             "[store]", f"name = {_q(name)}", 'currency = "USD"', f"fallback_reply = {_q(fallback)}",
             f"unverified = {_q(f['unverified'])}", ""]
    for r in rules:
        lines.append("[[rules]]")
        for k, v in r.items():
            lines.append(f"{k} = {_q(v) if isinstance(v, str) else ('true' if v is True else 'false' if v is False else v)}")
        lines.append("")
    toml = "\n".join(lines)
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as t:
        t.write(toml)
    try:
        load_policy(t.name)
    except PolicyError as e:  # a bug in this form, not the user's input
        return {"ok": False, "errors": {"_form": f"Generated policy did not load: {e}"}, "warnings": warnings,
                "toml": toml, "summary": [], "rules": 0}

    summary = [r["description"] for r in rules]
    if refunds and not any(r["kind"] == "escalate_over" for r in rules):
        summary.append("There's no escalation threshold: the assistant may approve any refund that meets the rules above.")
    if p:
        summary.append(f"Discounts up to {p}% are allowed for any order.")
    summary.append(("When the assistant can't check a promise against an order (for example, no order number yet), "
                    "the promise is blocked.") if f["unverified"] == "block" else
                   ("When the assistant can't check a promise against an order (for example, no order number yet), "
                    "the promise is allowed."))
    summary.append(f'If the assistant still breaks a rule after 2 retries, the customer gets: "{fallback}"')
    return {"ok": True, "errors": {}, "warnings": warnings, "toml": toml, "summary": summary, "rules": len(rules)}
