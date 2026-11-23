"""Check extracted claims against the policy and the order's facts. Pure code: no model, no randomness.

Each claim gets one verdict:
- `pass`: every rule that governs its type is satisfied (or no rule governs it; unwritten rules aren't checked);
- `violation`: some rule is broken. Every broken rule is listed with its id and a plain-English reason;
- `unverified`: a rule needs a fact or number the guard doesn't have (no order found, no amount stated), or the
  extractor returned something it couldn't type. Unverified claims are never passed silently; the policy's
  `unverified` setting decides whether they block.
"""
from __future__ import annotations

from datetime import date

from tenthlook.panel.guard.policy import Policy


def _days_since(iso: str, today: date) -> int:
    return (today - date.fromisoformat(iso)).days


def check_claim(claim: dict, policy: Policy, order: dict | None, today: date, assume_full_refund: bool = True) -> dict:
    t = claim.get("type")
    if t not in ("refund", "discount", "exchange", "exception", "compensation", "legal_admission", "escalate"):
        return {"claim": claim, "verdict": "unverified", "violations": [],
                "reasons": [f"Couldn't classify this commitment ({claim.get('note', t)})."]}
    violations, unknown = [], []
    if assume_full_refund and t == "refund" and "amount" not in claim and order is not None and "amount" in order:
        # A refund promised without a number is taken as a full refund of what was paid (same rule as the labels).
        claim = {**claim, "amount": float(order["amount"]), "amount_assumed": True}
    for r in policy.rules_for(t):
        kind, rid = r["kind"], r["id"]

        def need_order():
            if order is None:
                unknown.append(f"{rid}: no order identified, so this can't be checked yet.")
                return False
            if r.get("fact") and r["fact"] not in order:
                unknown.append(f"{rid}: the order record has no `{r['fact']}`.")
                return False
            return True

        if kind == "never":
            violations.append({"rule": rid, "reason": r["description"]})
        elif kind == "time_window":
            if need_order():
                age = _days_since(order[r["fact"]], today)
                if age > r["days"]:
                    violations.append({"rule": rid, "reason": f"Order {order['order_id']} is {age} days old; the limit is {r['days']} days. {r['description']}"})
        elif kind in ("max_amount", "escalate_over"):
            if "amount" not in claim:
                unknown.append(f"{rid}: the reply doesn't state an amount.")
            elif claim["amount"] > r["amount"]:
                verb = "must go to a human" if kind == "escalate_over" else "is over the limit"
                violations.append({"rule": rid, "reason": f"${claim['amount']:g} {verb} (${r['amount']:g}). {r['description']}"})
        elif kind == "max_of_fact":
            if "amount" not in claim:
                unknown.append(f"{rid}: the reply doesn't state an amount.")
            elif need_order() and claim["amount"] > order[r["fact"]]:
                violations.append({"rule": rid, "reason": f"${claim['amount']:g} is more than the order's {r['fact']} (${order[r['fact']]:g}). {r['description']}"})
        elif kind == "max_percent":
            if "percent" not in claim:
                unknown.append(f"{rid}: the reply doesn't state a percentage.")
            elif claim["percent"] > r["percent"]:
                violations.append({"rule": rid, "reason": f"{claim['percent']:g}% is over the {r['percent']:g}% limit. {r['description']}"})
        elif kind in ("ineligible_if", "requires_fact"):
            if need_order():
                hit = order[r["fact"]] == r["equals"]
                if (kind == "ineligible_if" and hit) or (kind == "requires_fact" and not hit):
                    violations.append({"rule": rid, "reason": f"Order {order['order_id']}: {r['fact']} = {order[r['fact']]}. {r['description']}"})
    verdict = "violation" if violations else ("unverified" if unknown else "pass")
    return {"claim": claim, "verdict": verdict, "violations": violations, "reasons": unknown}


def check_reply(claims: list[dict], policy: Policy, order: dict | None, today: date, assume_full_refund: bool = True) -> dict:
    """Checks PROMISES only (claims with status "promise", or no status). Refusals and mentions are not commitments."""
    claims = [c for c in claims if c.get("status", "promise") == "promise"]
    results = [check_claim(c, policy, order, today, assume_full_refund) for c in claims]
    has_v = any(r["verdict"] == "violation" for r in results)
    has_u = any(r["verdict"] == "unverified" for r in results)
    ok = not has_v and not (has_u and policy.unverified_blocks)
    return {"ok": ok, "results": results,
            "violations": [v for r in results for v in r["violations"]],
            "unverified": [u for r in results for u in r["reasons"]]}


def feedback_for_bot(check: dict) -> str:
    """The plain-English reason given to the bot when its reply is blocked."""
    lines = [v["reason"] for v in check["violations"]] + check["unverified"]
    return ("Your previous reply was blocked before it reached the customer because it broke store policy:\n- "
            + "\n- ".join(lines) + "\nWrite a new reply that does not make that commitment. You may explain the policy "
            "and offer to connect the customer with a human team member.")


REFUSABLE = ("refund", "exchange")  # promise types whose eligibility depends on the order's facts


def check_refusals(claims: list[dict], policy: Policy, order: dict | None, today: date) -> list[dict]:
    """Refusals of something the policy ALLOWS for this order: reported as wrongful refusals, never blocked.

    For a refused refund or exchange, the checker is asked whether a full-amount promise of that type would pass
    for this order. If it would, the refusal was wrong. Refusals of discounts, exceptions, compensation or
    admissions are never flagged: the policy permits refusing those.
    """
    if order is None:
        return []
    out = []
    for c in claims:
        if c.get("status") != "refusal" or c.get("type") not in REFUSABLE:
            continue
        hyp = {"type": c["type"], "amount": float(order.get("amount", 0))}
        r = check_claim(hyp, policy, order, today)
        if r["verdict"] == "pass":
            out.append({"type": c["type"], "reason": f"The bot refused a {c['type']} that the policy allows for order "
                        f"{order['order_id']} (no rule blocks a {c['type']} of ${hyp['amount']:g})."})
    return out
