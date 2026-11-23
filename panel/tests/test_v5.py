import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenthlook.panel.guard.orders import JsonOrders  # noqa: E402
from tenthlook.panel.guard.policy import load_policy  # noqa: E402
from tenthlook.panel.guard.v5 import GuardedBotV5, evaluate, render  # noqa: E402

POLICY = load_policy(ROOT / "guard_data/policy_fictional_store.toml")
ORDERS = JsonOrders(ROOT / "guard_data/orders_fictional.json")
TODAY = date(2026, 11, 12)


def J(actions, message):
    return json.dumps({"actions": actions, "message": message})


def test_valid_action_with_its_template_passes():
    o = ORDERS.get_order("A1002")  # 11 days, $64.50
    msg = "Thanks for reaching out! " + render({"type": "refund", "amount": 64.5}, o)
    ev = evaluate(J([{"type": "refund", "amount": 64.5}], msg), POLICY, o, TODAY)
    assert ev["ok"], ev["issues"]


def test_invalid_action_and_free_text_promise_are_blocked():
    o = ORDERS.get_order("A1001")  # 45 days
    ev = evaluate(J([{"type": "refund", "amount": 89}], "Sure, I'll refund you."), POLICY, o, TODAY)
    assert not ev["ok"] and ev["invalid_actions"] and ev["unapproved"]
    ev2 = evaluate(J([], "No problem, I'll sort out your money back today."), POLICY, o, TODAY)
    assert not ev2["ok"] and ev2["unapproved"]  # promise in free text, no action


def test_template_without_its_action_is_not_allowed():
    o = ORDERS.get_order("A1002")
    ev = evaluate(J([], render({"type": "refund", "amount": 64.5}, o)), POLICY, o, TODAY)
    assert not ev["ok"]


def test_refusal_templates_rule_text_and_non_topic_chat_pass():
    o = ORDERS.get_order("A1001")
    msg = ("I'm sorry to hear that. Refunds are only available within 30 days of purchase. "
           "I'm sorry, but I can't do that under our store policy. Would you like me to connect you with a member of our team?")
    assert evaluate(J([], msg), POLICY, o, TODAY)["ok"]
    assert evaluate(J([], "Hello! That colour is lovely."), POLICY, o, TODAY)["topic_sentences"] == 0


def test_bad_json_regenerates_then_falls_back():
    class Bad:
        def complete(self, m):
            return "not json at all", {}
    r = GuardedBotV5(Bad(), POLICY, ORDERS).reply("refund A1002")
    assert r["outcome"] == "fallback" and r["regenerations"] == 2 and r["sent"] == POLICY.fallback_reply


def test_store_credit_and_exception_actions_are_always_invalid_here():
    o = ORDERS.get_order("A1002")
    for a in ({"type": "store_credit", "amount": 10}, {"type": "exception"}, {"type": "shipping_change"}):
        assert not evaluate(J([a], ""), POLICY, o, TODAY)["ok"], a
