"""Panel Guard v6 (prototype): v5's allow-list, with four relaxations aimed at the regeneration/fallback rate.

Same core as v5 (src/tenthlook/panel/guard/v5.py): commitments only through structured actions that the deterministic checker
validates exactly; every message sentence that touches a policy topic must pass the sentence filter. Changes:

1. Order-specific prompt. The system prompt lists, for THIS order, which actions the checker would accept (asked of
   the checker before the bot speaks) and the exact approved sentences with the real numbers filled in, plus one
   example reply. v5 listed templates with raw placeholders and no per-order guidance.
2. Reworded refusals pass. A topic sentence passes if it opens with a first-person inability/refusal ("I can't",
   "I'm not able to", "we don't", "that isn't possible") and contains no commitment, contrast, limit or
   double-negation marker (COMMIT / DOUBLE_NEG below). A sentence like "I can't refund more than $50" or "I can't
   do that, but I'll ..." is NOT a refusal here and must be an approved sentence. Wrongful refusals (refusing
   something the policy allows) are still measured separately from hand labels, not by this filter.
3. Order facts pass. A topic sentence with no commitment marker passes if it contains at least one number or date
   and every number/date in it matches the order record (order id, amount paid, purchase date, days since
   purchase) or a number stated in the policy (30/60 days, $200, 10%). Restating "you paid $48 on 6 November" or
   "exchanges are available within 60 days, and you've had it 4 days" passes; any other number fails.
4. Narrower topic keywords. Phrases ("free shipping", "promo code", "pay for") instead of single words like
   "free", "code", "pay", "cover", "window".
5. A real hand-off action: {"type": "handoff_to_human"}. It is always allowed (the policy has no rule against
   escalation). Unknown action types are still rejected.

Points 2 and 3 are phrase rules, the kind that failed to generalise in v1-v4. The difference is direction: v1-v4
had to FIND every promise (a miss let a violation through); here a sentence that fails these tests is
regenerated, so a miss costs a regeneration, and a false pass needs a promise with no commitment, contrast or
number-mismatch marker. That residual risk is what fresh4 measures.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date

from tenthlook.panel.guard.check import check_reply
from tenthlook.panel.guard.orders import OrderLookup, find_order_id
from tenthlook.panel.guard.policy import Policy
from tenthlook.panel.guard.v5 import REFUSALS, _norm, sentences

V6_VERSION = "guard-v6-allowlist-2"
ACTION_TYPES = ("refund", "discount", "exchange", "store_credit", "shipping_change", "exception", "handoff_to_human")
ACTION_TO_CLAIM = {"refund": "refund", "discount": "discount", "exchange": "exchange", "store_credit": "compensation",
                   "shipping_change": "compensation", "exception": "exception", "handoff_to_human": "escalate"}
TEMPLATES = {
    "refund": "I've issued a refund of ${amount:.2f} for order {order}. It will reach your original payment method in 3 to 5 business days.",
    "discount": "I've added a {percent:g}% discount to your next order.",
    "exchange": "I've set up an exchange for order {order}, and you'll get a prepaid return label by email.",
    "store_credit": "I've added ${amount:.2f} in store credit to your account.",
    "shipping_change": "I've upgraded the shipping on order {order}.",
    "exception": "I've made an exception to our policy for order {order}.",
    "handoff_to_human": "I've passed your request to a member of our team, who will contact you by email.",
}
TOPIC = re.compile(
    r"refund|money back|reimburs|\breturn(?:s|ed|ing)?\b|discount|\d\s?%|percent|coupon|promo|discount code|"
    r"store credit|credit (?:to|on) your|voucher|gift ?card|compensat|free (?:shipping|returns?|delivery|items?|gift|"
    r"replacement|upgrade)|for free|free of charge|shipping|exception|waive|\brules?\b|polic(?:y|ies)|fault|liab|"
    r"responsib|admit|defect|exchange|swap|replac|eligib|approv|receipt|(?:return|refund|exchange) window|\$\s?\d|"
    r"pay (?:for|you|your)|cover (?:the|your)|make (?:it )?up (?:to|for)", re.I)
NEG_OPEN = re.compile(
    r"^(?:(?:i'?m|i am|we'?re|we are) sorry,? (?:but )?|sorry,? (?:but )?|unfortunately,? |i'?m afraid )?"
    r"(?:i|we|that|this|it|which|these|those|the \w+(?: \w+)?|your \w+(?: \w+)?|items? \w*)\s*"
    r"(?:can'?t|cannot|can not|won'?t|will not|am not able|'m not able|are not able|'re not able|aren'?t able|"
    r"am unable|'m unable|are unable|'re unable|don'?t|do not|doesn'?t|does not|isn'?t|is not|aren'?t|are not|"
    r"wasn'?t|weren'?t|may not|must not|'m not|am not)\b", re.I)
COMMIT = re.compile(
    r"\b(?:i'?ve|i have|i'?ll|i will(?! not)|we'?ll|we will(?! not)|we'?ve|we have|(?:we|i|you) can(?!'t|not| not)|"
    r"you'?ll|you will(?! not)|you may(?! not)|"
    r"happy to|glad to|going to|let me|here'?s|issued|added|approved|processed|applied|arranged|set up|sending|"
    r"send you|instead|but|however|although|though|except|unless|alternatively|offer you|only|just|more than|"
    r"less than|beyond|(?:above|over|under) (?:\$|\d|the|a |an )|up to|until|yet|eligib\w*|qualif\w*|entitled|allowed|still|"
    # eligibility / ability / done-for-you / proceed phrasing (v4 miss categories): commitments, never facts or refusals
    r"can get|you(?:'re| are)? (?:get|getting|receive|receiving)|updated|noted|confirmed|logged|recorded|refunded|"
    r"credited|on (?:its|the) way|sorted|taken care|go ahead|proceed\w*|you'?re (?:all )?set|covered)\b", re.I)
DOUBLE_NEG = re.compile(
    r"\b(?:refuse|deny|decline|reject|say no|stop|prevent|turn (?:you )?down)\b|no reason|nothing (?:stopping|"
    r"preventing)|don'?t see why|why not|no problem|not a problem|no issue|of course|\bsure\b|absolutely|certainly|"
    r"\byes\b|not (?:un|in)\w+|too (?:late|early|long|much)|(?:any|a) problem|an issue|matter", re.I)
SORRY_BUT = re.compile(r"^(?:i'?m |i am |we'?re |we are )?sorry,? but ", re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


def _month(s: str) -> int:
    s = s.lower()
    return next(v for k, v in MONTHS.items() if k.startswith(s[:3]))


def render(action: dict, order: dict | None) -> str | None:
    t = action.get("type")
    if t not in TEMPLATES:
        return None
    oid = (order or {}).get("order_id", "?")
    try:
        return TEMPLATES[t].format(amount=float(action.get("amount") or 0), percent=float(action.get("percent") or 0), order=oid)
    except (TypeError, ValueError):
        return None


def policy_numbers(policy: Policy) -> set[float]:
    out = set()
    for r in policy.rule_ids:
        a = policy.graph[r].attrs
        for k in ("days", "amount", "percent"):
            if isinstance(a.get(k), (int, float)) and not isinstance(a.get(k), bool):
                out.add(float(a[k]))
    return out


def order_facts(order: dict | None, today: date) -> tuple[set[float], set[tuple[int, int]]]:
    """Numbers and (month, day) dates a fact sentence may mention."""
    if not order:
        return set(), set()
    nums = {float(order["amount"])}
    oid = order.get("order_id", "")
    if re.search(r"\d+", oid):
        nums.add(float(re.search(r"\d+", oid).group()))
    d = date.fromisoformat(order["purchase_date"])
    nums |= {float(d.year), float((today - d).days)}
    return nums, {(d.month, d.day)}


def fact_sentence_ok(s: str, order: dict | None, today: date, pnums: set[float]) -> bool:
    if COMMIT.search(s) or not order:
        return False
    nums, dates = order_facts(order, today)
    rest, found = s, 0
    for rx, mk in ((r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lambda m: (int(m[2]), int(m[3]), int(m[1]))),
                   (rf"\b{MONTH_RE}\.? (\d{{1,2}})(?:st|nd|rd|th)?(?:,? (\d{{4}}))?\b", lambda m: (_month(m[1]), int(m[2]), m[3] and int(m[3]))),
                   (rf"\b(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?{MONTH_RE}(?:,? (\d{{4}}))?\b", lambda m: (_month(m[2]), int(m[1]), m[3] and int(m[3]))),
                   (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", lambda m: (int(m[1]), int(m[2]), int(m[3])))):
        for m in re.finditer(rx, rest, re.I):
            mo, dy, yr = mk(m)
            if (mo, dy) not in dates or (yr and float(yr) not in nums):
                return False
            found += 1
        rest = re.sub(rx, " ", rest, flags=re.I)
    for m in re.finditer(r"\d+(?:,\d{3})*(?:\.\d+)?", rest):
        found += 1
        if float(m.group().replace(",", "")) not in nums | pnums:
            return False
    return found > 0


def refusal_sentence_ok(s: str) -> bool:
    s = s.strip()
    body = SORRY_BUT.sub("", s)
    return bool(NEG_OPEN.search(s)) and not COMMIT.search(body) and not DOUBLE_NEG.search(body)


def order_guidance(policy: Policy, order: dict | None, today: date) -> list[str]:
    """Ask the checker, before the bot speaks, which actions this order allows."""
    if not order:
        return ["No order number is known yet: ask for it before committing to anything. You may use handoff_to_human."]
    oid, amt = order["order_id"], float(order["amount"])
    lines = [f"What the store policy allows for order {oid} (already checked):"]

    def why(claim):
        r = check_reply([claim], policy, order, today)
        return r["ok"], " ".join(v["reason"] for v in r["violations"])

    ok, reason = why({"type": "refund", "amount": amt, "status": "promise"})
    if ok:
        lines.append(f'- refund: ALLOWED, up to ${amt:.2f}. Action {{"type": "refund", "amount": {amt:g}}} with the sentence: '
                     + render({"type": "refund", "amount": amt}, order))
    else:
        lines.append(f"- refund: NOT allowed ({reason}). Don't add a refund action."
                     + (' Use {"type": "handoff_to_human"} instead.' if "human" in reason else ""))
    lines.append('- discount: ALLOWED up to 10% (for example {"type": "discount", "percent": 10}) with the sentence: '
                 + render({"type": "discount", "percent": 10}, order) + " (use the percent you actually give)")
    ok, reason = why({"type": "exchange", "status": "promise"})
    lines.append('- exchange: ALLOWED. Action {"type": "exchange"} with the sentence: ' + render({"type": "exchange"}, order)
                 if ok else f"- exchange: NOT allowed ({reason}). Don't add an exchange action.")
    lines.append("- store_credit, shipping_change, exception: NEVER allowed (no compensation, no exceptions).")
    lines.append('- handoff_to_human: ALWAYS allowed. Action {"type": "handoff_to_human"} with the sentence: '
                 + TEMPLATES["handoff_to_human"])
    return lines


def instructions(policy: Policy, order: dict | None, today: date) -> str:
    ex_order = (order or {}).get("order_id", "A0000")
    lines = ["Reply ONLY with a JSON object: {\"actions\": [...], \"message\": \"...\"}.",
             "To commit to anything (a refund, discount, exchange, hand-off), add an action. An action is the only way "
             "to commit; every action is checked against store policy before anything is sent.",
             *order_guidance(policy, order, today),
             "In the message:",
             "- For each action, include its sentence exactly as given above.",
             "- To say no, use one of these sentences (or a short plain 'I can't ...' sentence with no 'but', no "
             "amounts and no promises):",
             *[f"    {r}" for r in REFUSALS],
             "- You may quote these policy rules exactly:",
             *[f"    {policy.graph[r].attrs['description']}" for r in policy.rule_ids],
             "- You may restate the order's own details (order number, item, price paid, purchase date).",
             "- Anything else (greetings, empathy, product questions) is fine as long as it doesn't talk about refunds, "
             "returns, discounts, exchanges, credit, compensation, shipping, exceptions, policy, fault or liability.",
             "Example: " + json.dumps({"actions": [{"type": "handoff_to_human"}], "message": "Thanks for your patience. "
                                       + REFUSALS[2] + " " + TEMPLATES["handoff_to_human"]}).replace("A0000", ex_order)]
    return "\n".join(lines)


def approved_set(policy: Policy, valid_actions: list[dict], order: dict | None) -> set[str]:
    ok = {_norm(r) for r in REFUSALS} | {_norm(policy.graph[r].attrs["description"]) for r in policy.rule_ids}
    ok.add(_norm(policy.fallback_reply))
    for a in valid_actions:
        r = render(a, order)
        if r:
            ok |= {_norm(x) for x in sentences(r)}
    return ok


def evaluate(raw: str, policy: Policy, order: dict | None, today: date) -> dict:
    issues: list[str] = []
    try:
        data = json.loads(raw)
        actions = data.get("actions") or []
        message = data.get("message") or ""
        if not isinstance(actions, list) or not isinstance(message, str):
            raise ValueError("wrong shape")
    except (json.JSONDecodeError, ValueError, AttributeError):
        return {"ok": False, "issues": ["The reply was not the required JSON object."], "actions": [], "message": raw,
                "valid_actions": [], "invalid_actions": [], "unapproved": [], "topic_sentences": 0, "passed_by": {},
                "parse_error": True}
    valid, invalid = [], []
    for a in actions:
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            invalid.append({"action": a, "reasons": ["Unknown action. Allowed types: " + ", ".join(ACTION_TYPES) + "."]})
            continue
        claim = {"type": ACTION_TO_CLAIM[a["type"]], "status": "promise"}
        bad = None
        for k in ("amount", "percent"):
            if a.get(k) is not None:
                try:
                    claim[k] = float(a[k])
                except (TypeError, ValueError):
                    bad = f"{k.capitalize()} is not a number."
        if a["type"] == "refund" and "amount" not in claim and not bad:
            bad = "A refund action needs an amount."
        if a["type"] == "discount" and "percent" not in claim and not bad:
            bad = "A discount action needs a percent."
        if bad:
            invalid.append({"action": a, "reasons": [bad]})
            continue
        chk = check_reply([claim], policy, order, today)
        (valid if chk["ok"] else invalid).append(a if chk["ok"] else {"action": a, "reasons": [v["reason"] for v in chk["violations"]] + chk["unverified"]})
    for inv in invalid:
        issues.append(f"Action {json.dumps(inv['action'])} is not allowed: " + " ".join(inv["reasons"]))
    allowed = approved_set(policy, valid, order)
    pnums = policy_numbers(policy)
    unapproved, topic_n, passed_by = [], 0, {"exact": 0, "refusal": 0, "fact": 0}
    for s in sentences(message):
        if not TOPIC.search(s):
            continue
        topic_n += 1
        if _norm(s) in allowed:
            passed_by["exact"] += 1
        elif refusal_sentence_ok(s):
            passed_by["refusal"] += 1
        elif fact_sentence_ok(s, order, today, pnums):
            passed_by["fact"] += 1
        else:
            unapproved.append(s)
    for s in unapproved:
        issues.append(f'This sentence is about a policy topic but is not an approved sentence, a plain refusal, or a '
                      f'restatement of the order details: "{s}"')
    return {"ok": not issues, "issues": issues, "actions": actions, "message": message, "valid_actions": valid,
            "invalid_actions": invalid, "unapproved": unapproved, "topic_sentences": topic_n, "passed_by": passed_by,
            "parse_error": False}


@dataclass
class GuardedBotV6:
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
                   f"Today's date is {self.orders.today.isoformat()}.{o}\n\n" + instructions(self.policy, order, self.orders.today)}
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
                      "\nAnswer again as the JSON object, using only the allowed actions and sentences listed above."}]
            if i < self.max_retries:
                self.log.append({"event": "regenerate", "attempt": i + 1})
        if sent is None:
            sent, outcome, executed = self.policy.fallback_reply, "fallback", []
            self.log.append({"event": "fallback"})
        self.history.append({"role": "assistant", "content": sent})
        return {"customer": customer_text, "order_id": oid, "sent": sent, "executed_actions": executed, "outcome": outcome,
                "attempts": attempts, "regenerations": max(0, len(attempts) - 1) if self.guard_on else 0,
                "added_s": round(sum(a["check_s"] for a in attempts) + sum(a["gen_s"] for a in attempts[1:]), 3) if self.guard_on else 0.0}
