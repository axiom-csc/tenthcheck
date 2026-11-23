"""Panel Guard v6.5: consent recognition, both ways (proxy and executor; v6.3/v6.4 stay frozen as measured).

fresh7 found a miss: after "Would you like me to set up an exchange for order A1104?" the customer said "Let's go with
the exchange." and v6.3's consent rule (kept in v6.4) didn't count it, so an allowed exchange never happened. Checking
the rule while fixing that found the opposite, unsafe error: v6.3 also counted "Okay, thanks for explaining. I'll think
about it and get back to you.", "Okay, thanks.", "Sure, maybe later." and "Yes, but let me check with my partner first."
as a yes to an offer, because its yes pattern only looks at the first word. It never caused a wrong action on fresh6 or
fresh7 (the model never proposed one on those turns), but the checker would have allowed it.

v6.5's rule, for an action type T and the customer's latest message:
  1. **Hedged or deferred: never consent.** "maybe", "might", "probably", "not sure", "let me check / think", "check
     with", "think about", "get back to you", "decide later", "later", "for now", "next time", "not yet", "what about",
     "how about", "if I", "only if", "hold off", "wait", "on second thought", or a question that isn't a request
     ("Is the exchange free?", "Would the exchange be better?"). A polite request ("Could you set up the exchange?")
     is a request, not a hedge.
  2. **A clear request for T** (v6.3's asks_for, plus "let's go with", "I'll go with", "go ahead with", "I'll take"):
     consent, with or without an offer.
  3. **A yes to an offer of T in the bot's previous reply**: a sentence that starts with a strong yes ("yes", "yeah",
     "sure", "absolutely", "definitely", "perfect", "that works", "sounds good", "go ahead", "go for it", "do it",
     "please do", "let's do / go with / go ahead", "I'll take / go with", "that would be great") or is a bare
     "okay" / "alright" on its own. "Okay, thanks." and "Okay, thanks for explaining." are acknowledgements, not a yes.
  4. Otherwise, and on any "no" / "don't" / "never mind" / "cancel": not consent.

Unmeasured on held-out data; measure on fresh8. The phrase lists in tests/test_v65.py are the specification.
"""
from __future__ import annotations

import re
from datetime import date

from tenthlook.panel.guard.policy import Policy
from tenthlook.panel.guard.v5 import sentences
from tenthlook.panel.guard.v63 import NEG_REQUEST, NO, TYPE_WORDS, asks_for, last_turn, offered
from tenthlook.panel.guard.v64 import evaluate_v64, explanations, instructions_v64

V65_VERSION = "guard-v6.5-proxy"

HEDGE = re.compile(
    r"\b(?:maybe|might|perhaps|probably|possibly|not sure|unsure|undecided|haven'?t decided|let me (?:check|think|see|"
    r"ask|talk)|check with|talk to my|ask my|think (?:about|it over)|thinking about|get back to you|decide later|"
    r"later|for now|next time|not yet|what about|how about|if i\b|only if|hold off|wait|on second thought|"
    r"i'?ll let you know|need to check)\b", re.I)
POLITE_REQUEST = re.compile(r"^\s*(?:please\s+)?(?:can|could|would|will) you\b", re.I)
GO_WITH = re.compile(r"\b(?:let'?s go (?:with|ahead with)|i'?ll go (?:with|ahead with)|go ahead with|i'?ll take|"
                     r"i'?d like to go with|i choose|i'?ll do)\b", re.I)
STRONG_YES = re.compile(
    r"^\s*(?:oh\s+)?(?:yes|yeah|yep|yup|sure|absolutely|definitely|perfect|great|that works|that'?s fine|"
    r"sounds (?:good|great|perfect)|go ahead|go for it|do it|please (?:do|go ahead)|let'?s (?:do|go)|"
    r"i'?ll (?:take|go with)|that would be (?:great|good|perfect|lovely|helpful)|i'?d like that|"
    r"(?:ok(?:ay)?|alright|all right)[,!.\s]+(?:let'?s|go|please|do|set|yes|sure|sounds|that works|i'?ll)|"
    r"no (?:problem|worries)[,!.\s]+(?:go|please|do|set|that works|sounds|let'?s|yes|sure))\b", re.I)
# "no" about the action, not any "no"/"don't" ("They don't fit, so let's go with the exchange", "No problem, go ahead")
REFUSE = re.compile(
    r"^\s*no\b(?!\s+(?:problem|worries))|\bno thanks\b|\bnot the\b|\bdon'?t (?:\w+\s+)?(?:go ahead|do (?:it|that|this)|"
    r"bother|set|want (?:it|that|to)|need (?:it|that))\b", re.I)
NOT_A_NO = re.compile(r"\bno (?:problem|worries)\b", re.I)
BARE_OK = re.compile(r"^\s*(?:ok(?:ay)?|alright|all right|fine)(?:\s+then)?\s*[.!]*\s*$", re.I)


def _questions(msg: str) -> list[str]:
    return [s for s in sentences(msg) if s.rstrip().endswith("?")]


def hedged(msg: str, action_type: str = "") -> bool:
    """A hedge, deferral, or a question that isn't a request: never consent. A question that v6.3's request rule reads
    as a request for this action ("Can I talk to a person?", "Could I get 10% off?") or that starts "can/could/would
    you ..." is a request, not a hedge."""
    if HEDGE.search(msg):
        return True
    return any(not (POLITE_REQUEST.search(q) or (action_type and asks_for(q, action_type))) for q in _questions(msg))


def says_yes(msg: str) -> bool:
    if BARE_OK.match(msg):
        return True
    return any(STRONG_YES.search(s) for s in sentences(msg))


def consent_v65(action: dict, convo: list[dict]) -> tuple[bool, str]:
    t = action.get("type", "")
    msg, prev = last_turn(convo)
    if not msg.strip():
        return False, "no customer message"
    if NEG_REQUEST.search(msg) or REFUSE.search(msg):
        return False, "the customer said no"
    if hedged(msg, t):
        return False, "the customer hedged, deferred or asked a question"
    words = TYPE_WORDS.get(t)
    if asks_for(msg, t) or (GO_WITH.search(msg) and words is not None and words.search(msg)):
        return True, "the customer asked for it"
    if says_yes(msg) and not NO.search(NOT_A_NO.sub("", msg)) and offered(prev, t):
        return True, "the customer said yes to the offer"
    return False, "the customer only asked a question or didn't confirm"


def evaluate_v65(raw: str, policy: Policy, order: dict | None, today: date, done: list[dict], convo: list[dict]) -> dict:
    return evaluate_v64(raw, policy, order, today, done, convo, consent_fn=consent_v65)


instructions_v65 = instructions_v64

__all__ = ["V65_VERSION", "consent_v65", "evaluate_v65", "instructions_v65", "hedged", "says_yes", "explanations"]
