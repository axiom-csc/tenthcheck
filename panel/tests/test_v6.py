"""v6 sentence filter and actions: made-up sentences only (nothing from fresh3/fresh4 replies)."""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenthlook.panel.guard.policy import load_policy  # noqa: E402
from tenthlook.panel.guard.v6 import evaluate, fact_sentence_ok, instructions, policy_numbers, refusal_sentence_ok  # noqa: E402

P = load_policy(ROOT / "guard_data/policy_fictional_store.toml")
TODAY = date(2026, 11, 12)
O = {"order_id": "B2001", "purchase_date": "2026-11-02", "amount": 88.0, "on_sale": False, "has_receipt": True}
SALE = {"order_id": "B2002", "purchase_date": "2026-11-02", "amount": 300.0, "on_sale": True, "has_receipt": False}


def ev(actions, message, order=O):
    return evaluate(json.dumps({"actions": actions, "message": message}), P, order, TODAY)


def test_reworded_refusals_pass():
    for s in ["I can't offer a voucher for this.", "I'm not able to waive the shipping fee.",
              "Sorry, but we don't give compensation for late parcels.", "That isn't possible under our policy."]:
        assert refusal_sentence_ok(s), s


def test_refusal_lookalikes_fail():
    for s in ["I can't refund more than $50.", "I can't do a refund, but I'll add store credit.",
              "I don't see why you can't have a refund.", "There's no reason I can't refund you.",
              "I can't refuse a refund here.", "It isn't too late for a refund.", "You'll get a refund."]:
        assert not refusal_sentence_ok(s), s


def test_fact_sentences():
    pn = policy_numbers(P)
    assert fact_sentence_ok("You paid $88 for order B2001 on November 2.", O, TODAY, pn)
    assert fact_sentence_ok("Exchanges are available within 60 days, and you bought it 10 days ago.", O, TODAY, pn)
    assert not fact_sentence_ok("You paid $90 for order B2001.", O, TODAY, pn)
    assert not fact_sentence_ok("You bought it on 3 November, so it's within the window.", O, TODAY, pn)
    assert not fact_sentence_ok("You paid $88, so I'll refund $88.", O, TODAY, pn)
    assert not fact_sentence_ok("Your refund is on its way.", O, TODAY, pn)  # no number: not a fact restatement


COMMIT_LOOKALIKES = [
    "You're eligible for the $200 refund.",                     # policy number + eligibility
    "Order B2001 qualifies for a refund of $88.",
    "Your order B2001 qualify for the 30 day refund window.",
    "I can refund the $88 for order B2001.",
    "We can do the $88 refund on order B2001.",
    "You'll receive $88 back within 10 days.",
    "You'll get $88 back for order B2001.",
    "You get $88 back on order B2001.",
    "You're getting the $88 back.",
    "I've updated order B2001 for the $88 refund.",
    "I've noted the $88 refund on order B2001.",
    "I've applied 10% to order B2001.",
    "Order B2001 is within 30 days, would you like to proceed with the $88 refund?",
    "Would you like to go ahead with the $88 for order B2001?",
    "The $88 for order B2001 is on its way.",
]


def test_commitment_cues_fail_fact_and_refusal_rules():
    pn = policy_numbers(P)
    for s in COMMIT_LOOKALIKES:
        assert not fact_sentence_ok(s, O, TODAY, pn), s
        assert not refusal_sentence_ok(s), s
        assert not ev([], s)["ok"], s


def test_handoff_action_and_unknown_action():
    r = ev([{"type": "handoff_to_human"}], "I've passed your request to a member of our team, who will contact you by email.")
    assert r["ok"], r["issues"]
    r = ev([{"type": "connect_agent"}], "Hello.")
    assert not r["ok"] and r["invalid_actions"]


def test_actions_still_checked_exactly():
    assert not ev([{"type": "refund", "amount": 300}], "", SALE)["ok"]
    assert not ev([{"type": "discount", "percent": 15}], "")["ok"]
    assert not ev([{"type": "refund"}], "")["ok"]  # refund without an amount
    assert ev([{"type": "refund", "amount": 88}], "I've issued a refund of $88.00 for order B2001. It will reach your original payment method in 3 to 5 business days.")["ok"]


def test_promise_text_without_action_blocked():
    assert not ev([], "I'll get that refund sorted for you today.")["ok"]
    assert not ev([], "We can give you free shipping next time.")["ok"]


def test_narrow_topic_keywords():
    assert ev([], "Feel free to ask if you need anything else. Our tees come in five colours.")["ok"]


def test_instructions_are_order_specific():
    t = instructions(P, SALE, TODAY)
    assert "refund: NOT allowed" in t and "exchange: NOT allowed" in t and "handoff_to_human" in t
    t = instructions(P, O, TODAY)
    assert "$88.00 for order B2001" in t and "exchange: ALLOWED" in t


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
    print("ok")
