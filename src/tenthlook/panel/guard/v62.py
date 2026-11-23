"""Panel Guard v6.2: the frozen v6 checker plus v6.1's hand-off rule plus proxy-written confirmations (proxy only).

Why: in the gpt-4o-mini run (j12) a $54 refund was issued on turn 1. On turns 2 and 3 the bot's restatements of it
("Your refund ... has been processed") were blocked by v6's commitment cues, and the regeneration it sent was a bare
"I'm sorry, but I can't do that under our store policy.", which reads as taking the refund back.

What v6.2 changes (all in the proxy; src/tenthlook/panel/guard/v6.py stays frozen as the reported version):
1. The proxy writes confirmations. For each approved action the proxy appends the exact confirmation sentence with
   the real values (v6's TEMPLATES). Any confirmation sentence the model wrote itself is removed first, so it is
   never doubled. The model is told to add the action and not to word the confirmation.
2. Actions already executed in the conversation are known. The proxy is stateless, so it finds them in the
   assistant messages the business sends back: the exact confirmation sentences the proxy wrote earlier. For each
   one, the model is told it is done, and a status sentence ("Your refund of $54.00 for order A1051 has already been
   issued ...") is approved for exact use. A repeated action of a type already executed is not executed again; its
   status sentence is used instead (a second $54 refund is never approved by repetition).
3. Nothing contradicts an executed action. If a reply after an executed action contains a refusal and doesn't
   already mention that action, the proxy puts the action's status sentence first. The fallback reply also opens
   with the status of every executed action.
4. v6.1's phantom hand-off rule, with an executed hand-off counted as present.

Limits: detection relies on the business sending back the proxy's own replies unchanged. If it rewrites or drops
them, earlier actions are not seen (and a repeat action would be checked by v6 alone). Unmeasured on held-out data.
"""
from __future__ import annotations

import re
from datetime import date

from tenthlook.panel.guard.policy import Policy
from tenthlook.panel.guard.v5 import REFUSALS, _norm, sentences
from tenthlook.panel.guard.v6 import TEMPLATES, evaluate, instructions, refusal_sentence_ok, render
from tenthlook.panel.guard.v61 import HANDOFF, is_offer, phantom_handoff, phantom_issue

V62_VERSION = "guard-v6.2-proxy"
STATUS = {
    "refund": "Your refund of ${amount:.2f} for order {order} has already been issued and will reach your original "
              "payment method in 3 to 5 business days.",
    "discount": "Your {percent:g}% discount has already been added to your next order.",
    "exchange": "Your exchange for order {order} is already set up, and the prepaid return label will come by email.",
    "store_credit": "Your ${amount:.2f} in store credit has already been added to your account.",
    "shipping_change": "The shipping on order {order} has already been upgraded.",
    "exception": "The exception for order {order} has already been made.",
    "handoff_to_human": "Your request is already with a member of our team, who will contact you by email.",
}
CONFIRM_LINE = "- For each action, include its sentence exactly as given above."
NEW_CONFIRM_LINE = ("- Don't write the confirmation for an action yourself: after your message, the exact confirmation "
                    "sentence for each approved action is added for you. Just add the action.")


def _sentence_rx(tmpl: str) -> re.Pattern:
    """Regex for one sentence of a TEMPLATES entry, capturing amount / percent / order."""
    s = re.escape(tmpl)
    s = s.replace(re.escape("{amount:.2f}"), r"(?P<amount>\d+(?:\.\d+)?)")
    s = s.replace(re.escape("{percent:g}"), r"(?P<percent>\d+(?:\.\d+)?)")
    s = s.replace(re.escape("{order}"), r"(?P<order>#?[A-Za-z0-9-]+)")
    return re.compile(s, re.I)


SENT_RX = {t: [_sentence_rx(x) for x in sentences(TEMPLATES[t])] for t in TEMPLATES}


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def executed_before(convo: list[dict]) -> list[dict]:
    """Actions executed earlier in the conversation: one per type, found from the proxy's own confirmation sentences."""
    found: dict[str, dict] = {}
    for m in convo:
        if m.get("role") != "assistant":
            continue
        text = _text(m.get("content"))
        for t, rxs in SENT_RX.items():
            mt = rxs[0].search(text)
            if mt and t not in found:
                a = {"type": t}
                for k in ("amount", "percent"):
                    if k in rxs[0].groupindex:
                        a[k] = float(mt.group(k))
                found[t] = a
    return list(found.values())


def status(a: dict, order: dict | None) -> str:
    oid = (order or {}).get("order_id", "?")
    return STATUS[a["type"]].format(amount=float(a.get("amount") or 0), percent=float(a.get("percent") or 0), order=oid)


def instructions_v62(policy: Policy, order: dict | None, today: date, done: list[dict]) -> str:
    base = instructions(policy, order, today)
    assert CONFIRM_LINE in base, "v6 instructions changed"
    base = base.replace(CONFIRM_LINE, NEW_CONFIRM_LINE)
    if not done:
        return base
    lines = ["", "Already done in this conversation (do NOT add these actions again):"]
    for a in done:
        lines.append(f"- {a['type']}: done. To mention it, use exactly: {status(a, order)}")
    return base + "\n".join(lines)


def _mentions(s: str, t: str, order: dict | None, done: list[dict]) -> bool:
    """s is a sentence of type t's confirmation template (any values) or the status sentence of an executed t."""
    return any(rx.fullmatch(s.strip()) for rx in SENT_RX[t]) or any(
        a["type"] == t and _norm(s) == _norm(status(a, order)) for a in done)


def _confirmation_like(s: str, order: dict | None, done: list[dict]) -> bool:
    return any(_mentions(s, t, order, done) for t in TEMPLATES)


def evaluate_v62(raw: str, policy: Policy, order: dict | None, today: date, done: list[dict]) -> dict:
    ev = evaluate(raw, policy, order, today)
    if ev.get("parse_error"):
        return ev
    done_types = {a["type"] for a in done}
    ok_extra = {_norm(x) for a in done for x in [status(a, order), *sentences(render(a, order) or "")]}
    unapproved = [s for s in ev["unapproved"] if _norm(s) not in ok_extra]
    new = [a for a in ev["valid_actions"] if a.get("type") not in done_types]
    repeated = [a for a in ev["valid_actions"] if a.get("type") in done_types]
    issues = [i for i in ev["issues"] if i.startswith("Action ")]
    issues += [f'This sentence is about a policy topic but is not an approved sentence, a plain refusal, or a '
               f'restatement of the order details: "{s}"' for s in unapproved]
    phantom = phantom_handoff(ev["message"], new + done)
    issues += [phantom_issue(s) for s in phantom]
    return {**ev, "ok": not issues, "issues": issues, "unapproved": unapproved, "valid_actions": new,
            "repeated_actions": repeated, "phantom_handoff": phantom}


_HANDOFF_WORDS = re.compile(r"\b(?:member of (?:our|the) (?:\w+ )?team|team members?|our (?:\w+ )?team|the team|"
                            r"a (?:real )?person|a human|supervisor|manager|specialist|representative)\b", re.I)


def _states_handoff(s: str) -> bool:
    """A body sentence that already speaks about handing the request to a person (claim or offer)."""
    return bool(HANDOFF.search(s) or (is_offer(s) and _HANDOFF_WORDS.search(s)))


# When an executor is connected (src/tenthlook/panel/execute), the confirmation depends on what actually happened to the action.
# Only an executed action gets the "I've issued ..." sentence; anything else says what really happened.
PENDING = {
    "refund": "I've sent a refund of ${amount:.2f} for order {order} to our team for approval. You'll get an email once it's processed.",
    "discount": "I've asked our team to approve a {percent:g}% discount on your next order. You'll get an email once it's approved.",
    "exchange": "I've sent your exchange request for order {order} to our team. You'll get an email with the next steps.",
    "handoff_to_human": "I've asked a member of our team to review your request. You'll get an email once they pick it up.",
}
NOT_DONE = "I wasn't able to complete that myself just now, so a member of our team will need to look at it."
NOT_DONE_NO_TEAM = "I wasn't able to complete that myself just now, and I can't promise when it will be done."


def confirmation(action: dict, order: dict | None, record=None) -> str:
    """The sentence for one approved action. record is the executor's Record (None: no executor, v6.2 behaviour)."""
    if record is None or record.status == "executed":
        s = render(action, order) or ""
        code = (getattr(record, "result", None) or {}).get("code") if record is not None else None
        return s + (f" Your code is {code}." if code and action.get("type") == "discount" else "")
    oid = (order or {}).get("order_id", "?")
    if record.status in ("queued", "dry_run", "approved", "executing", "auto"):
        return PENDING.get(action["type"], PENDING["handoff_to_human"]).format(
            amount=float(action.get("amount") or 0), percent=float(action.get("percent") or 0), order=oid)
    if record.status == "needs_consent":
        from tenthlook.panel.guard.v63 import offer_sentence
        return offer_sentence(action, order)
    if record.status == "rejected":
        return "I'm not able to offer that."
    # failed / blocked: the executor tried to tell the team; say only what is true
    return NOT_DONE if record.type != "handoff_to_human" else NOT_DONE_NO_TEAM


def compose(message: str, new: list[dict], repeated: list[dict], done: list[dict], order: dict | None,
            tail: list[str] | None = None) -> tuple[str, bool]:
    """The text the customer gets: the model's sentences minus confirmations, then the proxy's own confirmations.
    Returns (text, acknowledged), where acknowledged means status sentences were put first because of a refusal."""
    sents = sentences(message)
    body = [s for s in sents if not _confirmation_like(s, order, done)]
    mentioned = {a["type"] for a in done if any(_mentions(s, a["type"], order, done) for s in sents)}
    refuses = any(refusal_sentence_ok(s) or _norm(s) in {_norm(r) for r in REFUSALS[:4]} for s in body)
    head = []
    ack = False
    for a in done:
        if a["type"] == "handoff_to_human" and any(_states_handoff(s) for s in body):
            continue  # the model's own sentence already talks about the hand-off: no second, redundant one
        if a["type"] in mentioned or a["type"] in {r["type"] for r in repeated}:
            head.append(status(a, order))  # model restated it: proxy states it in its own words
        elif refuses:
            head.append(status(a, order))
            ack = True
    if tail is None:
        tail = [render(a, order) for a in new if render(a, order)]
    return " ".join([*head, *body, *tail]).strip(), ack


def fallback_v62(policy: Policy, done: list[dict], order: dict | None) -> str:
    if not done:
        return policy.fallback_reply
    head = " ".join(status(a, order) for a in done)
    if any(a["type"] == "handoff_to_human" for a in done):
        return head + " " + sentences(policy.fallback_reply)[0]
    return head + " " + policy.fallback_reply
