"""Panel Guard v5 (prototype): allow-list design. The bot can only commit through structured actions.

Why: v1–v4 tried to *find* promises in free text with phrase rules, and that didn't generalise to held-out
conversations (GUARD_README.md). v5 inverts it: commitments exist only as structured actions the checker
validates exactly, and any sentence that touches a policy topic must be one of a fixed set of approved sentences.

The bot answers in JSON: {"actions": [...], "message": "..."}.
- Actions: refund {amount}, discount {percent}, exchange {}, store_credit {amount}, shipping_change {}, exception {}.
  Each is validated by the deterministic checker against the policy and the order (store credit and shipping
  changes count as compensation; this policy forbids them, as it forbids exceptions).
- Message: split into sentences. A sentence that mentions a policy topic (TOPIC: a deliberately broad keyword
  list) must exactly match an approved sentence (whitespace, case and end punctuation ignored):
  * the template for an action in this same reply that passed the checker;
  * an approved refusal / hand-off sentence;
  * one of the policy's own rule descriptions, verbatim.
  Anything else on a policy topic is "unapproved": the reply is regenerated with the reasons and the list of
  allowed sentences, up to `max_retries` times, then the store's fallback reply is sent. Sentences with no topic
  keyword pass untouched (so ordinary chat stays free-form; that is also where a promise could still slip through).
- Guard off: the message is sent as written and the actions are executed unchecked (the baseline).

Exchange was added to the action list (the brief listed refund, discount, store_credit, shipping_change,
exception) because this policy allows exchanges and leaving it out would force every allowed exchange into a
hand-off.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from tenthlook.panel.guard.check import check_reply
from tenthlook.panel.guard.orders import OrderLookup, find_order_id
from tenthlook.panel.guard.policy import Policy

V5_VERSION = "guard-v5-allowlist-1"
ACTION_TYPES = ("refund", "discount", "exchange", "store_credit", "shipping_change", "exception")
ACTION_TO_CLAIM = {"refund": "refund", "discount": "discount", "exchange": "exchange", "store_credit": "compensation",
                   "shipping_change": "compensation", "exception": "exception"}
TEMPLATES = {
    "refund": "I've issued a refund of ${amount:.2f} for order {order}. It will reach your original payment method in 3 to 5 business days.",
    "discount": "I've added a {percent:g}% discount to your next order.",
    "exchange": "I've set up an exchange for order {order}, and you'll get a prepaid return label by email.",
    "store_credit": "I've added ${amount:.2f} in store credit to your account.",
    "shipping_change": "I've upgraded the shipping on order {order}.",
    "exception": "I've made an exception to our policy for order {order}.",
}
REFUSALS = [
    "I'm sorry, but I can't do that under our store policy.",
    "I'm not able to offer that.",
    "I can't make exceptions to our store policy.",
    "I'm not able to comment on fault or liability.",
    "I can connect you with a member of our team who can review your request.",
    "Would you like me to connect you with a member of our team?",
]
TOPIC = re.compile(
    r"refund|money back|reimburs|\breturn|discount|%|percent|coupon|promo|\bcode\b|credit|voucher|gift ?card|compensat|"
    r"\bfree\b|shipping|exception|waive|\brules?\b|polic(?:y|ies)|fault|liab|responsib|admit|defect|exchange|swap|replac|"
    r"eligib|approv|receipt|\bwindow\b|\$\s?\d|\bcover\b|\bpay\b", re.I)


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("–", "-")
    return re.sub(r"\s+", " ", s).strip().strip(".!").strip().lower()


def sentences(text: str) -> list[str]:
    out = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        part = part.strip()
        if part:
            out.append(part)
    return out


def render(action: dict, order: dict | None) -> str | None:
    t = action.get("type")
    if t not in TEMPLATES:
        return None
    oid = (order or {}).get("order_id", "?")
    try:
        return TEMPLATES[t].format(amount=float(action.get("amount") or 0), percent=float(action.get("percent") or 0), order=oid)
    except (TypeError, ValueError):
        return None


def instructions(policy: Policy, order: dict | None) -> str:
    oid = (order or {}).get("order_id", "{order}")
    lines = ["Reply ONLY with a JSON object: {\"actions\": [...], \"message\": \"...\"}.",
             "To commit to anything, add an action. Allowed action shapes:",
             '  {"type": "refund", "amount": <number>}   {"type": "discount", "percent": <number>}   {"type": "exchange"}',
             '  {"type": "store_credit", "amount": <number>}   {"type": "shipping_change"}   {"type": "exception"}',
             "Every action is checked against store policy before anything is sent.",
             "In the message, any sentence about refunds, returns, discounts, exchanges, credit, compensation, shipping, "
             "exceptions, policy, fault or liability must be copied EXACTLY from this list (fill the numbers):",
             *[f"  - (only with a {t} action) " + TEMPLATES[t].replace("{order}", oid) for t in ("refund", "discount", "exchange")],
             *[f"  - {r}" for r in REFUSALS],
             *[f"  - {policy.graph[r].attrs['description']}" for r in policy.rule_ids],
             "Other sentences (greetings, empathy, questions about the order) can be written freely but must not mention those topics."]
    return "\n".join(lines)


def approved_set(policy: Policy, valid_actions: list[dict], order: dict | None) -> set[str]:
    ok = {_norm(r) for r in REFUSALS} | {_norm(policy.graph[r].attrs["description"]) for r in policy.rule_ids}
    ok.add(_norm(policy.fallback_reply))
    for a in valid_actions:
        r = render(a, order)
        if r:
            ok |= {_norm(x) for x in sentences(r)}  # templates may be two sentences; replies are checked per sentence
    return ok


def evaluate(raw: str, policy: Policy, order: dict | None, today) -> dict:
    """Parse, validate actions, filter sentences. Returns ok, issues, actions, message, topic_hits, etc."""
    issues: list[str] = []
    try:
        data = json.loads(raw)
        actions = data.get("actions") or []
        message = data.get("message") or ""
        if not isinstance(actions, list) or not isinstance(message, str):
            raise ValueError("wrong shape")
    except (json.JSONDecodeError, ValueError, AttributeError):
        return {"ok": False, "issues": ["The reply was not the required JSON object."], "actions": [], "message": raw,
                "valid_actions": [], "invalid_actions": [], "unapproved": [], "topic_sentences": 0, "parse_error": True}
    valid, invalid = [], []
    for a in actions:
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            invalid.append({"action": a, "reasons": ["Unknown action."]})
            continue
        claim = {"type": ACTION_TO_CLAIM[a["type"]], "status": "promise"}
        if a.get("amount") is not None:
            try:
                claim["amount"] = float(a["amount"])
            except (TypeError, ValueError):
                invalid.append({"action": a, "reasons": ["Amount is not a number."]})
                continue
        if a.get("percent") is not None:
            try:
                claim["percent"] = float(a["percent"])
            except (TypeError, ValueError):
                invalid.append({"action": a, "reasons": ["Percent is not a number."]})
                continue
        chk = check_reply([claim], policy, order, today)
        if chk["ok"]:
            valid.append(a)
        else:
            invalid.append({"action": a, "reasons": [v["reason"] for v in chk["violations"]] + chk["unverified"]})
    for inv in invalid:
        issues.append(f"Action {json.dumps(inv['action'])} is not allowed: " + " ".join(inv["reasons"]))
    allowed = approved_set(policy, valid, order)
    unapproved, topic_n = [], 0
    for s in sentences(message):
        if TOPIC.search(s):
            topic_n += 1
            if _norm(s) not in allowed:
                unapproved.append(s)
    for s in unapproved:
        issues.append(f'This sentence is about a policy topic but is not one of the approved sentences: "{s}"')
    return {"ok": not issues, "issues": issues, "actions": actions, "message": message, "valid_actions": valid,
            "invalid_actions": invalid, "unapproved": unapproved, "topic_sentences": topic_n, "parse_error": False}


@dataclass
class GuardedBotV5:
    client: object  # ChatClient with json_mode=True
    policy: Policy
    orders: OrderLookup
    guard_on: bool = True
    max_retries: int = 2
    history: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)

    def reply(self, customer_text: str) -> dict:
        self.history.append({"role": "user", "content": customer_text})
        oid = find_order_id(*(m["content"] for m in self.history))
        order = self.orders.get_order(oid) if oid else None
        o = ""
        if order:
            o = (f"\nThe customer's order {order['order_id']}: {order.get('item', 'item')}, bought {order['purchase_date']}, "
                 f"paid ${order['amount']:g}, on sale: {'yes' if order.get('on_sale') else 'no'}, "
                 f"receipt: {'yes' if order.get('has_receipt') else 'no'}.")
        sys_msg = {"role": "system", "content": f"You are the customer-support assistant for {self.policy.name}. Be friendly and brief. "
                   f"Today's date is {self.orders.today.isoformat()}.{o}\n\n" + instructions(self.policy, order)}
        attempts, extra = [], []
        sent, outcome, executed = None, None, []
        for i in range(1 + (self.max_retries if self.guard_on else 0)):
            t0 = time.perf_counter()
            raw, meta = self.client.complete([sys_msg, *self.history, *extra])
            gen_s = time.perf_counter() - t0
            t1 = time.perf_counter()
            ev = evaluate(raw, self.policy, order, self.orders.today)
            chk_s = time.perf_counter() - t1
            attempts.append({"attempt": i, "raw": raw, "text": ev["message"], "eval": ev, "gen_s": round(gen_s, 3),
                             "check_s": round(chk_s, 4), **meta})
            if not self.guard_on:
                sent, outcome, executed = ev["message"], "unguarded", ev["actions"]
                break
            if ev["ok"]:
                sent, outcome, executed = ev["message"], ("passed" if i == 0 else "passed_after_regeneration"), ev["valid_actions"]
                break
            self.log.append({"event": "blocked", "attempt": i, "issues": ev["issues"]})
            extra = [{"role": "assistant", "content": raw}, {"role": "user", "content":
                      "Your reply was blocked before it reached the customer:\n- " + "\n- ".join(ev["issues"]) +
                      "\nAnswer again as the JSON object. Use only allowed actions, and copy policy sentences exactly from the list."}]
            if i < self.max_retries:
                self.log.append({"event": "regenerate", "attempt": i + 1})
        if sent is None:
            sent, outcome, executed = self.policy.fallback_reply, "fallback", []
            self.log.append({"event": "fallback"})
        self.history.append({"role": "assistant", "content": sent})
        return {"customer": customer_text, "order_id": oid, "sent": sent, "executed_actions": executed, "outcome": outcome,
                "attempts": attempts, "regenerations": max(0, len(attempts) - 1) if self.guard_on else 0,
                "added_s": round(sum(a["check_s"] for a in attempts) + sum(a["gen_s"] for a in attempts[1:]), 3) if self.guard_on else 0.0}
