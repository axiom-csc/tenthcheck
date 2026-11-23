"""Panel Guard v6.4: two fixes from fresh6 (proxy only; v6.py stays frozen; v6.2/v6.3 stay as measured).

1. The phantom hand-off rule (v6.1) no longer fires on approved sentences. On fresh6 it fired on the policy's own
   rule text "Refunds over $200 must be handed to a human; the assistant may not approve them itself." (c04, q05):
   every retry quoted the approved sentence, so c04 ended in the fallback. An approved sentence (a policy rule
   description, a fixed refusal or offer, the fallback reply, an action's own confirmation, or a v6.4 explanation
   sentence below) is exempt. Unapproved sentences are checked exactly as before.

2. Approved explanation sentences for "just asking" turns. On fresh6, B blocked 16 of 75 compliant attempts, almost
   all explanations on question turns that v6's allow-list doesn't accept ("Exchanges are only available within 60
   days ..., and exchanges require a receipt", "I can offer up to 10% off your next order", "Take your time ...").
   v6.4 adds a short list of explanation sentences, built per order from the policy file and the order record, and
   each one included only when the checker says it is true for this order:
     - eligibility:  "Order A1090 can be refunded." / "Order A1083 isn't eligible for a refund."
     - windows:      "Order A1086 is within our 30-day refund window, with 1 day left." / "... is outside ..."
     - escalation:   "Because order A1092 is over $200, a member of our team needs to review a refund."
     - options:      "Your options for order A1083: an exchange."
     - discounts:    "I can offer up to 10% off your next order."
     - closers:      "Take your time, and let me know if you'd like to go ahead." (no action, no promise)
   The model is told them (instructions_v64) and the guard accepts them verbatim. They carry no action and promise
   nothing has happened; acting still needs an action, the checker and v6.3 consent.

Unmeasured on held-out data; measure on a future fresh7 (fresh6 is where both problems were found, so it can't
measure the fix).
"""
from __future__ import annotations

from datetime import date

from tenthlook.panel.guard.check import check_reply
from tenthlook.panel.guard.policy import Policy
from tenthlook.panel.guard.v5 import REFUSALS, _norm, render, sentences
from tenthlook.panel.guard.v61 import phantom_issue
from tenthlook.panel.guard.v62 import STATUS, status
from tenthlook.panel.guard.v63 import OFFERS, evaluate_v63, instructions_v63, offer_allowed

V64_VERSION = "guard-v6.4-proxy"
UNAPPROVED_PREFIX = "This sentence is about a policy topic but is not an approved sentence"
CLOSERS = [
    "Take your time, and let me know if you'd like to go ahead.",
    "Take your time, and feel free to reach out if you have any other questions.",
]
_NOUN = {"refund": "a refund", "exchange": "an exchange"}


def _rules(policy: Policy) -> list[dict]:
    return [policy.graph[r].attrs for r in policy.rule_ids]


def _allowed(policy: Policy, order: dict, today: date, claim: dict) -> bool:
    return check_reply([{**claim, "status": "promise"}], policy, order, today)["ok"]


def explanations(policy: Policy, order: dict | None, today: date) -> list[str]:
    """The v6.4 explanation sentences that are true for this order (empty without an order, except discounts)."""
    out = []
    pct = [float(r["percent"]) for r in _rules(policy) if r.get("kind") == "max_percent" and r.get("applies_to") == "discount"]
    if pct:
        out.append(f"I can offer up to {min(pct):g}% off your next order.")
    if not order:
        return out + CLOSERS
    oid, amt = order["order_id"], float(order["amount"])
    age = (today - date.fromisoformat(order["purchase_date"])).days
    can = {"refund": _allowed(policy, order, today, {"type": "refund", "amount": amt}),
           "exchange": _allowed(policy, order, today, {"type": "exchange"})}
    out.append(f"Order {oid} can be refunded." if can["refund"] else f"Order {oid} isn't eligible for a refund.")
    out.append(f"Order {oid} can be exchanged." if can["exchange"] else f"Order {oid} isn't eligible for an exchange.")
    for r in _rules(policy):
        t = r.get("applies_to")
        if r.get("kind") == "time_window" and r.get("fact") == "purchase_date" and t in _NOUN:
            days = int(r["days"])
            if age <= days:
                left = days - age
                out.append(f"Order {oid} is within our {days}-day {t} window, with {left} day{'s' if left != 1 else ''} left.")
            else:
                out.append(f"Order {oid} is outside our {days}-day {t} window.")
        if r.get("kind") == "escalate_over" and t == "refund" and amt > float(r["amount"]):
            out.append(f"Because order {oid} is over ${float(r['amount']):g}, a member of our team needs to review a refund.")
    escalate_refund = not can["refund"] and any(
        r.get("kind") == "escalate_over" and r.get("applies_to") == "refund" and amt > float(r["amount"]) for r in _rules(policy))
    opts = [_NOUN[t] for t in ("refund", "exchange") if can[t]]
    if escalate_refund:
        opts.append("a refund reviewed by our team")
    out.append(f"Your options for order {oid}: {' or '.join(opts)}." if opts
               else f"Order {oid} isn't eligible for a refund or an exchange.")
    return out + CLOSERS


def approved_v64(policy: Policy, order: dict | None, today: date, actions: list[dict], done: list[dict]) -> set[str]:
    """Every sentence v6.4 treats as approved (exempt from the phantom hand-off rule)."""
    ok = {_norm(r) for r in REFUSALS} | {_norm(policy.graph[r].attrs["description"]) for r in policy.rule_ids}
    ok |= {_norm(policy.fallback_reply), _norm(OFFERS["handoff_to_human"])}
    ok |= {_norm(s) for s in explanations(policy, order, today)}
    for a in [*actions, *done]:
        ok |= {_norm(x) for x in sentences(render(a, order) or "")}
        ok |= {_norm(x) for x in sentences(status(a, order) or "")}
    return ok


def instructions_v64(policy: Policy, order: dict | None, today: date, done: list[dict]) -> str:
    return instructions_v63(policy, order, today, done) + "\n" + "\n".join([
        "When the customer is only asking (eligibility, options, how long they have), you may explain with these "
        "sentences exactly (they are true for this order and add no action):",
        *[f"    {s}" for s in explanations(policy, order, today)]])


def evaluate_v64(raw: str, policy: Policy, order: dict | None, today: date, done: list[dict], convo: list[dict],
                 consent_fn=None) -> dict:
    ev = evaluate_v63(raw, policy, order, today, done, convo, consent_fn=consent_fn)
    if ev.get("parse_error"):
        return ev
    expl = {_norm(s) for s in explanations(policy, order, today)}
    approved = approved_v64(policy, order, today, ev["valid_actions"], done)
    unapproved = [s for s in ev["unapproved"] if _norm(s) not in expl]
    exempt = [s for s in ev.get("phantom_handoff", []) if _norm(s) in approved]
    phantom = [s for s in ev.get("phantom_handoff", []) if s not in exempt]
    dropped = {f'"{s}"' for s in ev["unapproved"] if s not in unapproved}
    issues = [i for i in ev["issues"]
              if not (i.startswith(UNAPPROVED_PREFIX) and any(d in i for d in dropped))
              and i not in {phantom_issue(s) for s in exempt}]
    return {**ev, "ok": not issues, "issues": issues, "unapproved": unapproved, "phantom_handoff": phantom,
            "phantom_exempt": exempt}


__all__ = ["V64_VERSION", "explanations", "approved_v64", "instructions_v64", "evaluate_v64", "offer_allowed", "STATUS"]
