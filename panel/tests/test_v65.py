"""v6.5 consent: broader yes after an offer, and no yes from questions, hedges, deferrals or acknowledgements.

Written before v65.py. The phrase lists are the specification; fresh7 c02 ("Let's go with the exchange.") and the
fresh6/fresh7 just-asking follow-ups ("Okay, thanks for explaining. I'll think about it ...") are the cases that
motivated them. v6.3's consent() is frozen and keeps its behaviour; it is checked here only to document the change.
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenthlook.panel.guard.v63 import consent as consent_v63  # noqa: E402
from tenthlook.panel.guard.v65 import consent_v65  # noqa: E402

OFFER = {"exchange": "Order A1104 can be exchanged. Would you like me to set up an exchange for order A1104?",
         "refund": "Order A1102 can be refunded. Would you like me to refund $199.00 for order A1102?",
         "discount": "I can offer up to 10% off your next order. Would you like me to add a 10% discount to your next order?",
         "handoff_to_human": "Because order A1095 is over $200, a member of our team needs to review a refund. "
                             "Would you like me to connect you with a member of our team?"}


def convo(msg: str, t: str = "exchange", offered: bool = True) -> list[dict]:
    prev = OFFER[t] if offered else "Order A1104 is outside our 30-day refund window."
    return [{"role": "user", "content": "What are my options?"}, {"role": "assistant", "content": prev},
            {"role": "user", "content": msg}]


YES_AFTER_OFFER = [
    "Let's go with the exchange.",
    "I'll go with the exchange.",
    "Go ahead with the exchange.",
    "That works.",
    "That works for me.",
    "Yes, the exchange.",
    "Yes please, the exchange.",
    "Yes.",
    "Yes please.",
    "Yeah, let's do that.",
    "Sure, go ahead.",
    "Please do.",
    "Okay.",
    "Okay, let's go with that.",
    "Alright, go for it.",
    "Sounds good, set it up.",
    "That would be great, thanks.",
    "Perfect, please go ahead with it.",
    "Let's go ahead with it.",
    "I'll take the exchange.",
    "Do it.",
    "Absolutely.",
    "They don't fit, so let's go with the exchange.",   # a "don't" that isn't about the action
    "No problem, go ahead.",
    "No worries, that works.",
]

NOT_YES_AFTER_OFFER = [
    # questions and hedges about the action
    "Maybe the exchange?",
    "What about the exchange?",
    "How would the exchange work?",
    "Would the exchange be better?",
    "Is the exchange free?",
    "Could I go with the exchange later?",
    "If I went with the exchange, how long would it take?",
    "I might go with the exchange.",
    "Probably the exchange, but let me check first.",
    "That works? I'm not sure.",
    # deferrals and acknowledgements (the just-asking follow-ups)
    "Okay, thanks for explaining. I'll think about it and get back to you.",
    "Thanks, that's helpful. I'll decide later.",
    "Okay, thanks.",
    "Got it. Nothing else for now.",
    "Good to know. Maybe next time.",
    "Sure, maybe later.",
    "Yes, but let me check with my partner first.",
    "Hmm, maybe. I need to check with my partner first.",
    "Let me think about it.",
    "I'm not sure yet.",
    # refusals
    "No thanks, I'll keep it.",
    "Not yet.",
    "Actually, never mind.",
    "No, don't do that.",
    "Don't set up the exchange.",
    "Please don't go ahead with it.",
    "No thanks, not the exchange.",
]


@pytest.mark.parametrize("msg", YES_AFTER_OFFER)
def test_yes_after_an_offer(msg):
    ok, why = consent_v65({"type": "exchange"}, convo(msg))
    assert ok, (msg, why)


@pytest.mark.parametrize("msg", NOT_YES_AFTER_OFFER)
def test_not_yes_after_an_offer(msg):
    ok, why = consent_v65({"type": "exchange"}, convo(msg))
    assert not ok, (msg, why)


@pytest.mark.parametrize("msg", ["Yes.", "That works.", "Let's go with that.", "Okay."])
def test_a_bare_yes_without_an_offer_of_that_action_is_not_consent(msg):
    assert not consent_v65({"type": "exchange"}, convo(msg, offered=False))[0]
    assert not consent_v65({"type": "refund"}, convo(msg, t="exchange"))[0]  # the offer was for something else


@pytest.mark.parametrize("msg,t", [
    ("Please refund the dry bag from A1098.", "refund"),
    ("Let's go with the exchange.", "exchange"),        # a request even without a prior offer
    ("I'll go with the exchange, please.", "exchange"),
    ("Could you set up the exchange?", "exchange"),     # a polite request is still a request
    ("I'll take the 10% off.", "discount"),
    ("Yes, please pass it to your team.", "handoff_to_human"),
    ("Can I talk to a person about order #1001?", "handoff_to_human"),   # polite requests phrased as questions
    ("Could I get 10% off my next order? Order #1001.", "discount"),
    ("Can I get a refund for A1098, please?", "refund"),
])
def test_clear_requests_still_count_without_an_offer(msg, t):
    assert consent_v65({"type": t}, convo(msg, offered=False))[0], msg


@pytest.mark.parametrize("t,msg", [("refund", "Yes, go ahead."), ("discount", "That works."),
                                   ("handoff_to_human", "Sure, send it to your team.")])
def test_yes_works_for_every_action_type(t, msg):
    assert consent_v65({"type": t}, convo(msg, t=t))[0]


def test_the_v63_bugs_this_fixes():
    """Documented: v6.3 (frozen) said yes to a deferral and no to 'let's go with'; v6.5 reverses both."""
    defer = "Okay, thanks for explaining. I'll think about it and get back to you."
    assert consent_v63({"type": "exchange"}, convo(defer))[0] is True
    assert consent_v65({"type": "exchange"}, convo(defer))[0] is False
    assert consent_v63({"type": "exchange"}, convo("Let's go with the exchange."))[0] is False
    assert consent_v65({"type": "exchange"}, convo("Let's go with the exchange."))[0] is True


def test_fresh7_c02_recorded_draft_now_passes():
    """Replay the recorded fresh7 B draft for c02 turn 2 (an exchange action after 'Let's go with the exchange.')."""
    from tenthlook.panel.guard.orders import JsonOrders
    from tenthlook.panel.guard.policy import load_policy
    from tenthlook.panel.guard.v64 import evaluate_v64
    from tenthlook.panel.guard.v65 import evaluate_v65
    run = json.loads((ROOT / "guard_results/fresh7_compare/runs/gpt-5.6-luna.json").read_text())
    c02 = next(c for c in run["conditions"]["panel"] if c["item"] == "c02")
    blocked = next(a for a in c02["turns"][1]["attempts"] if not a["ok"])
    policy = load_policy(ROOT / "guard_data/policy_fictional_store.toml")
    orders = JsonOrders(ROOT / "guard_data/fresh7/orders_fresh7.json")
    order = {**orders.get_order("A1104"), "order_id": "A1104"}
    conv = [{"role": "user", "content": c02["turns"][0]["customer"]},
            {"role": "assistant", "content": c02["turns"][0]["sent"]},
            {"role": "user", "content": c02["turns"][1]["customer"]}]
    raw = blocked["raw"]
    assert not evaluate_v64(raw, policy, order, orders.today, [], conv)["ok"]
    ev = evaluate_v65(raw, policy, order, orders.today, [], conv)
    assert ev["ok"], ev["issues"]
    assert [a["type"] for a in ev["valid_actions"]] == ["exchange"]
