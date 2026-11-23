"""Panel Guard v6.1: the frozen v6 checker plus one rule, applied in the proxy only.

Phantom hand-off rule. A reply that says a hand-off happened or will happen ("I've passed your request to a member
of our team", "a member of our team will contact you", "someone will reach out", "I've escalated this") must carry
a {"type": "handoff_to_human"} action. If it doesn't, the reply is blocked and regenerated like any other v6 block:
otherwise the customer is told a person is on it and no hand-off is recorded (seen once in the proxy demo, see
GUARD_README "Known issues").

What does NOT trip it:
- offers and questions that leave the choice to the customer ("I can connect you with a member of our team who can review your request.", "Would you like
  me to connect you with a member of our team?", the policy's fallback reply). They promise nothing has happened;
- plain refusals that pass v6's refusal rule ("I can't transfer you to a manager.").

src/tenthlook/panel/guard/v6.py stays frozen as the reported version (sha256 c30d0c80...). GuardedBotV6 and the measured runs do
not use this rule. v6.1 has not been measured on held-out data.
"""
from __future__ import annotations

import re

from tenthlook.panel.guard.v5 import sentences
from tenthlook.panel.guard.v6 import refusal_sentence_ok

V61_VERSION = "guard-v6.1-proxy"
_WHO = (r"(?:a |an |the |one of our |our )?(?:member of (?:our|the) (?:\w+ )?(?:team|staff)|(?:\w+ )?team members?|"
        r"colleagues?|human(?: agent)?|real person|person|agents?|supervisors?|managers?|specialists?|"
        r"representatives?|reps?|someone(?: (?:from|on) (?:our|the) (?:\w+ )?team)?|(?:our|the) (?:\w+ )?team|staff)")
HANDOFF = re.compile("|".join([
    r"\bescalat(?:ed|ing|ion)\b",
    r"\b(?:passed|passing|forwarded|forwarding|referred|referring|transferred|transferring|handed|handing|sent|"
    r"routed|routing|flagged|raised|connected)\b[^.?!]{0,50}?\b(?:to|over to|on to|onto|along to|with)\s+" + _WHO + r"\b",
    r"\b(?:i'?ll|i will|let me|i'?m going to|we'?ll|we will)\s+(?:now\s+|go ahead and\s+)?"
    r"(?:pass|forward|escalate|refer|transfer|connect|put you through|hand (?:this|it|you|your \w+) (?:over|off|on))\b",
    _WHO + r"\s+(?:will|'ll|is going to|are going to|shall|should be)\b(?! not)",
    r"\b(?:will|'ll)\s+(?:be (?:in touch|contacting|reaching out)|contact you|reach out|get back to you|"
    r"follow up with you|call you|email you)\b",
    r"\b(?:is|are) (?:now )?(?:reviewing|looking into|handling) (?:your|this|the)\b",
]), re.I)


# An offer leaves the choice to the customer: "I can pass your request to a team member, who will contact you",
# "Would you like me to ...", "If you'd like, I can ...". It is not a completed hand-off, unless the same sentence
# also says something already happened ("I can confirm I've passed ...").
OFFER_OPEN = re.compile(
    r"^(?:(?:sure|absolutely|of course|yes|okay|ok)[,!.]?\s+)?(?:(?:but|and|or|also)\s+)?"
    r"(?:(?:if you(?:'d| would) like|if you want|if you prefer|if that helps|if you wish)[,]?\s+)?"
    r"(?:(?:i|we)\s+(?:can|could|(?:would|'d) be happy to|(?:am|'m|are|'re) happy to|(?:am|'m|are|'re) able to)\b"
    r"|(?:would|do) you (?:like|want) (?:me|us) to\b|shall (?:i|we)\b|should (?:i|we)\b|want me to\b)", re.I)
DONE_MARK = re.compile(r"\b(?:i'?ve|we'?ve|i have|we have|has been|have been|already|is now|are now|passed|"
                       r"escalated|forwarded|transferred|referred|handed|connected|flagged|raised)\b", re.I)


def is_offer(s: str) -> bool:
    s = s.strip().lstrip("\"'(")
    return bool(OFFER_OPEN.search(s)) and not DONE_MARK.search(s)


def phantom_handoff(message: str, valid_actions: list[dict]) -> list[str]:
    """Sentences that claim a hand-off when no handoff_to_human action was approved. Empty list = fine.
    Offers ("I can pass this to our team", "Would you like me to ...") and plain refusals are not claims."""
    if any(a.get("type") == "handoff_to_human" for a in valid_actions if isinstance(a, dict)):
        return []
    return [s for s in sentences(message) if HANDOFF.search(s) and not refusal_sentence_ok(s) and not is_offer(s)]


def phantom_issue(s: str) -> str:
    return (f'This sentence says the request was handed to a person, but no {{"type": "handoff_to_human"}} action was '
            f'given: "{s}". Add the handoff_to_human action, or don\'t say a hand-off happened.')


def evaluate_v61(ev: dict) -> dict:
    """Apply the phantom hand-off rule to a v6 evaluate() result. Returns a new result; ev is not changed."""
    if not ev["ok"]:
        return ev
    bad = phantom_handoff(ev["message"], ev["valid_actions"])
    if not bad:
        return ev
    return {**ev, "ok": False, "issues": [phantom_issue(s) for s in bad], "phantom_handoff": bad}
