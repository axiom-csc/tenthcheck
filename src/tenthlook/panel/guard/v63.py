"""Panel Guard v6.3: never execute an action the customer didn't explicitly ask for or confirm (proxy + executor).

Why: on fresh5 k12 the customer asked "Is it too late to get a refund ...? If so, what are my options?" and the bot
set up an exchange nobody asked for (in both the guarded and unguarded Panel conditions). It was within policy, so
nothing flagged it.

Rule: an action needs consent from the customer's LATEST message, either
  1. a clear request for it ("please refund it", "I want an exchange", "Refund my backpack", "Could I get 10% off"), or
  2. a yes ("yes", "sure", "go ahead", "please do") to the bot's offer of that action in its PREVIOUS reply.
Questions are not requests: "what are my options?", "can I still return it?", "is it too late ...?", "am I eligible?",
and "can I return / exchange / swap ...?". They get an explanation plus an offer sentence ("Would you like me to set
up an exchange for order A1070?") and no action. A hand-off needs a request for a person, an explicit request of any
kind the bot can't fulfil itself, or a yes to an offer to connect.

Where it's enforced:
- the guard (src/tenthlook/panel/guard/proxy.py): an action without consent is an issue -> blocked and regenerated, with feedback
  telling the model to offer instead. Offer sentences are approved only for actions the checker allows on the order.
- the executor (src/tenthlook/panel/execute/executor.py) checks consent again from the transcript, independently of the prompt
  and the guard, and refuses ("needs_consent", which a later consented submit can replace).

These are phrase rules, like v6's: they err towards asking (a request phrased oddly gets an offer and one extra turn).
Unmeasured on held-out data; the next fresh set needs "just asking" conversations.
"""
from __future__ import annotations

import re
from datetime import date

from tenthlook.panel.guard.check import check_reply
from tenthlook.panel.guard.policy import Policy
from tenthlook.panel.guard.v5 import REFUSALS, _norm, sentences
from tenthlook.panel.guard.v6 import ACTION_TO_CLAIM
from tenthlook.panel.guard.v61 import is_offer
from tenthlook.panel.guard.v62 import evaluate_v62, instructions_v62

V63_VERSION = "guard-v6.3-proxy"

TYPE_WORDS = {
    "refund": re.compile(r"\brefund|money back|reimburs|pay me back|send (?:it |them |this )?back", re.I),
    "exchange": re.compile(r"\bexchang|\bswap|different (?:size|colou?r)|\breplace", re.I),
    "discount": re.compile(r"\bdiscount|\d+\s?%|percent off|\bcoupon|\bpromo", re.I),
    "handoff_to_human": re.compile(r"\b(?:a (?:real )?person|human|someone|an? agent|manager|supervisor|representative|"
                                   r"team member|member of (?:your|the) team|speak (?:to|with)|talk (?:to|with))\b", re.I),
}
TYPE_WORDS["store_credit"] = re.compile(r"store credit|gift ?card|voucher", re.I)
REQUEST = re.compile(
    r"\b(?:please|pls|i want|i'?d like|i would like|i need|i demand|can you|could you|would you|will you|go ahead|"
    r"just do|do it|process (?:it|this|the|my)|approve|give me|set (?:it |one |that )?up|send me|i'?ll take|let'?s do|"
    r"(?:can|could|may) i (?:get|have)|i'?m (?:asking|requesting) (?:for|you))\b", re.I)
IMPERATIVE = re.compile(r"(?:^|[.!?]\s+|\band\s+|,\s*)(?:please\s+)?(?:just\s+)?(?:refund|exchange|swap|replace|give|approve|"
                        r"process|send|set up|apply|add|connect|transfer)\b", re.I)
INFO_QUESTION = re.compile(
    r"\b(?:what (?:are|is) my options|what (?:can|could|should) i do|what are the options|(?:can|could) i still|"
    r"is it (?:too late|possible|still possible|still ok)|am i (?:eligible|able|allowed)|would i be able|"
    r"how (?:long|many days)|do i (?:have|qualify)|does (?:it|this|that) qualify|if so|just (?:asking|wondering|curious)|"
    r"(?:i'?m |i was )?wondering (?:if|whether)|is there (?:any|a) way|what happens if|what would happen)\b", re.I)
POSSIBILITY_Q = re.compile(r"^\s*(?:hi|hello|hey)?[,!.\s]*(?:can|could|may) i (?:still )?(?:return|exchange|swap|send|"
                           r"get (?:a |my )?(?:refund|money back))\b", re.I)
# a negation of the ACTION itself ("don't refund it", "no need to exchange", "actually, don't"), not any "don't"
# in the message ("they don't fit", "I don't have the receipt", "no need to ask me anything")
NEG_REQUEST = re.compile(
    r"\b(?:don'?t|do not|no need to|not)\s+(?:\w+\s+){0,2}?(?:refund|exchange|swap|replace|discount|connect|transfer|"
    r"hand (?:me|it|this) (?:off|over)|escalate)\b|\bnever mind\b|\bcancel (?:it|that|the|my)\b|\bactually,? (?:don'?t|no)\b|"
    r"\bnot (?:yet|now)\b", re.I)
YES = re.compile(r"^\s*(?:oh\s+)?(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please(?: do)?|go ahead|do it|that would be "
                 r"(?:great|good|perfect|lovely)|sounds good|absolutely|definitely|i'?d like that|let'?s do (?:it|that)|"
                 r"please go ahead|y)\b", re.I)
NO = re.compile(r"\b(?:no|nope|not|don'?t|rather not|never mind|cancel|wait)\b", re.I)

OFFERS = {
    "refund": "Would you like me to refund ${amount:.2f} for order {order}?",
    "exchange": "Would you like me to set up an exchange for order {order}?",
    "discount": "Would you like me to add a {percent:g}% discount to your next order?",
    "handoff_to_human": "Would you like me to connect you with a member of our team?",
}


def _offer_rx(t: str) -> re.Pattern:
    s = re.escape(OFFERS[t])
    s = s.replace(re.escape("{amount:.2f}"), r"(?P<amount>\d+(?:\.\d+)?)")
    s = s.replace(re.escape("{percent:g}"), r"(?P<percent>\d+(?:\.\d+)?)")
    s = s.replace(re.escape("{order}"), r"(?P<order>#?[A-Za-z0-9-]+)")
    return re.compile(s, re.I)


OFFER_RX = {t: _offer_rx(t) for t in OFFERS}


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def last_turn(convo: list[dict]) -> tuple[str, str]:
    """(latest customer message, the bot's reply just before it)."""
    msgs = [m for m in convo if m.get("role") in ("user", "assistant")]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            prev = next((_text(m.get("content")) for m in reversed(msgs[:i]) if m.get("role") == "assistant"), "")
            return _text(msgs[i].get("content")), prev
    return "", ""


def offered(prev_reply: str, action_type: str) -> bool:
    """The bot's previous reply offered this kind of action (an exact offer sentence, or any offer that names it)."""
    for s in sentences(prev_reply):
        if action_type in OFFER_RX and OFFER_RX[action_type].search(s):
            return True
        words = TYPE_WORDS.get(action_type)
        if (is_offer(s) or s.rstrip().endswith("?")) and words and words.search(s):
            return True
        if action_type == "handoff_to_human" and _norm(s) in {_norm(REFUSALS[4]), _norm(REFUSALS[5])}:
            return True
    return False


def asks_for(msg: str, action_type: str) -> bool:
    """The message is a clear request for this action type (not a question about it)."""
    if not msg.strip() or INFO_QUESTION.search(msg):
        return False
    words = TYPE_WORDS.get(action_type)
    if action_type == "handoff_to_human":
        if words.search(msg) and not NEG_REQUEST.search(msg):
            return True
        # a request the bot can't fulfil itself (a refund over the limit, an exception ...) may be handed on
        return bool((REQUEST.search(msg) or IMPERATIVE.search(msg)) and not NEG_REQUEST.search(msg))
    if not words or not words.search(msg):
        return False
    if NEG_REQUEST.search(msg):
        return False
    if POSSIBILITY_Q.search(msg) and not re.search(r"\b(?:please|pls|i want|i'?d like|i need)\b", msg, re.I):
        return False
    return bool(REQUEST.search(msg) or IMPERATIVE.search(msg))


def consent(action: dict, convo: list[dict]) -> tuple[bool, str]:
    t = action.get("type", "")
    msg, prev = last_turn(convo)
    if asks_for(msg, t):
        return True, "the customer asked for it"
    if YES.search(msg) and not NO.search(msg) and offered(prev, t):
        return True, "the customer said yes to the offer"
    return False, "the customer only asked a question or didn't confirm"


def consent_issue(action: dict) -> str:
    t = action.get("type")
    return (f"The customer didn't ask for a {t.replace('_', ' ')} in their last message or say yes to your offer, so "
            f"don't add the {t} action. Answer their question and end with the offer sentence listed above (no action).")


def offer_sentence(action: dict, order: dict | None) -> str:
    oid = (order or {}).get("order_id", "?")
    return OFFERS.get(action["type"], OFFERS["handoff_to_human"]).format(
        amount=float(action.get("amount") or (order or {}).get("amount") or 0), percent=float(action.get("percent") or 10),
        order=oid)


def offer_allowed(s: str, policy: Policy, order: dict | None, today: date) -> bool:
    """An exact offer sentence for an action the checker accepts on this order."""
    for t, rx in OFFER_RX.items():
        m = rx.fullmatch(s.strip())
        if not m:
            continue
        if t == "handoff_to_human":
            return True
        if not order or ("order" in rx.groupindex and m.group("order").lstrip("#") != str(order.get("order_id", "")).lstrip("#")):
            return False
        claim = {"type": ACTION_TO_CLAIM[t], "status": "promise"}
        for k in ("amount", "percent"):
            if k in rx.groupindex:
                claim[k] = float(m.group(k))
        return check_reply([claim], policy, order, today)["ok"]
    return False


def instructions_v63(policy: Policy, order: dict | None, today: date, done: list[dict]) -> str:
    base = instructions_v62(policy, order, today, done)
    offers = []
    if order:
        for t, claim in (("refund", {"type": "refund", "amount": float(order["amount"])}), ("exchange", {"type": "exchange"}),
                         ("discount", {"type": "discount", "percent": 10.0})):
            if check_reply([{**claim, "status": "promise"}], policy, order, today)["ok"]:
                offers.append(offer_sentence({"type": t, **{k: v for k, v in claim.items() if k != "type"}}, order))
    offers.append(OFFERS["handoff_to_human"])
    return base + "\n".join([
        "", "Consent (checked before anything is done): add an action ONLY when the customer's last message clearly asks "
        "for it (\"please refund it\", \"I want an exchange\"), or when they said yes to your offer of it in your previous "
        "reply. If they are only asking (\"what are my options?\", \"can I return this?\", \"is it too late?\"), explain, "
        "end with the matching offer sentence below, and add NO action:",
        *[f"    {o}" for o in offers]])


def evaluate_v63(raw: str, policy: Policy, order: dict | None, today: date, done: list[dict], convo: list[dict],
                 consent_fn=None) -> dict:
    """`consent_fn` (default: this module's `consent`, unchanged) lets v6.5 plug in its consent rule."""
    consent_fn = consent_fn or consent
    ev = evaluate_v62(raw, policy, order, today, done)
    if ev.get("parse_error"):
        return ev
    unapproved = [s for s in ev["unapproved"] if not offer_allowed(s, policy, order, today)]
    issues = [i for i in ev["issues"] if not any(f'"{s}"' in i for s in ev["unapproved"] if s not in unapproved)]
    no_consent = [a for a in ev["valid_actions"] if not consent_fn(a, convo)[0]]
    issues += [consent_issue(a) for a in no_consent]
    return {**ev, "ok": not issues, "issues": issues, "unapproved": unapproved, "no_consent": no_consent}
