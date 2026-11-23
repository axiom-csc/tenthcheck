import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenthlook.panel.guard.check import check_reply  # noqa: E402
from tenthlook.panel.guard.extract import extract_patterns  # noqa: E402
from tenthlook.panel.guard.guard import GuardedBot  # noqa: E402
from tenthlook.panel.guard.orders import JsonOrders, find_order_id  # noqa: E402
from tenthlook.panel.guard.policy import PolicyError, load_policy  # noqa: E402

POLICY = load_policy(ROOT / "guard_data/policy_fictional_store.toml")
ORDERS = JsonOrders(ROOT / "guard_data/orders_fictional.json")
TODAY = date(2026, 11, 12)


def verdict(reply, oid):
    return check_reply(extract_patterns(reply), POLICY, ORDERS.get_order(oid), TODAY)


def test_policy_graph_and_validation(tmp_path):
    assert len(POLICY.rule_ids) == 10
    assert {r["id"] for r in POLICY.rules_for("refund")} == {"refund-window", "refund-not-on-sale",
                                                             "refund-not-more-than-paid", "refund-escalate-over-200"}
    bad = tmp_path / "bad.toml"
    bad.write_text('[store]\nfallback_reply = "x"\n[[rules]]\nid = "a"\nkind = "max_percent"\napplies_to = "discount"\ndescription = "d"\n')
    with pytest.raises(PolicyError, match="needs `percent`"):
        load_policy(bad)


def test_checker_rules():
    assert verdict("I've processed a refund of $64.50.", "A1002")["ok"]  # 11 days, not on sale
    v = verdict("I've processed a refund of $89.", "A1001")  # 45 days old
    assert not v["ok"] and v["violations"][0]["rule"] == "refund-window"
    assert {x["rule"] for x in verdict("I'll refund the $145.", "A1003")["violations"]} == {"refund-not-on-sale"}
    assert {x["rule"] for x in verdict("I've approved the $260 refund.", "A1004")["violations"]} == {"refund-escalate-over-200"}
    assert {x["rule"] for x in verdict("I'll refund $300 for it.", "A1007")["violations"]} == {
        "refund-escalate-over-200", "refund-not-more-than-paid"}
    assert verdict("I can offer 10% off.", "A1002")["ok"]
    assert not verdict("I can offer 15% off.", "A1002")["ok"]
    assert not verdict("Since you have it, I'll set up the exchange.", "A1005")["ok"]  # no receipt
    assert not verdict("I'll send you a $20 gift card.", "A1002")["ok"]
    assert verdict("Let me transfer you to a supervisor.", "A1002")["ok"]
    assert verdict("Refunds are only available within 30 days.", "A1001")["ok"]  # no claim


def test_unverified_blocks_and_never_passes_silently():
    v = check_reply(extract_patterns("I'll process your refund now."), POLICY, None, TODAY)
    assert not v["ok"] and v["unverified"] and not v["violations"]
    u = check_reply([{"type": "unknown", "note": "garbled"}], POLICY, ORDERS.get_order("A1002"), TODAY)
    assert not u["ok"] and u["results"][0]["verdict"] == "unverified"


def test_find_order_id():
    assert find_order_id("hi", "my order is A1003, sorry I meant A1004") == "A1004"
    assert find_order_id("no order here") is None


class Scripted:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0), {"finish_reason": "stop"}


def test_guard_blocks_regenerates_then_passes():
    bot = Scripted(["Sure, I've processed a refund of $89 for order A1001.",
                    "I'm sorry, order A1001 is past our 30-day window, so I can't refund it. I can connect you with a member of our team."])
    g = GuardedBot(bot, POLICY, ORDERS)
    r = g.reply("Refund order A1001 please")
    assert r["outcome"] == "passed_after_regeneration" and "can't refund" in r["sent"]
    assert "45 days old" in bot.calls[1][-1]["content"]  # the bot was told why
    assert [e["event"] for e in g.log] == ["blocked", "regenerate"]


def test_guard_falls_back_and_never_sends_a_failing_reply():
    bad = "Okay, I've processed a refund of $89 for order A1001."
    g = GuardedBot(Scripted([bad, bad, bad]), POLICY, ORDERS)
    r = g.reply("Refund order A1001 please")
    assert r["outcome"] == "fallback" and r["sent"] == POLICY.fallback_reply
    assert all(a["text"] != r["sent"] for a in r["attempts"])
    assert [e["event"] for e in g.log] == ["blocked", "regenerate", "blocked", "regenerate", "blocked", "fallback"]


def test_guard_off_sends_whatever_the_bot_says():
    bad = "Okay, I've processed a refund of $89 for order A1001."
    r = GuardedBot(Scripted([bad]), POLICY, ORDERS, guard_on=False).reply("Refund order A1001 please")
    assert r["outcome"] == "unguarded" and r["sent"] == bad


def test_v3_statuses_promise_refusal_mention():
    from tenthlook.panel.guard.extract import extract_patterns, promises
    st = lambda text, t: next((c["status"] for c in extract_patterns(text) if c["type"] == t), None)
    assert st("I'll process a refund of $40 for you.", "refund") == "promise"
    assert st("I'm afraid I can't refund this order.", "refund") == "refusal"
    assert st("Unfortunately it's past our 30-day refund window.", "refund") == "refusal"
    assert st("We can only process refunds within 30 days of purchase.", "refund") == "mention"
    assert st("If you have the receipt, we can exchange it.", "exchange") == "mention"
    assert st("If you'd like, I can offer you 10% off your next order.", "discount") == "promise"
    assert st("A team member who can assist you with a refund will call.", "refund") is None
    assert promises(extract_patterns("I'm not able to admit fault.")) == []


def test_wrongful_refusal_is_reported_not_blocked():
    from tenthlook.panel.guard.check import check_refusals
    allowed = check_refusals(extract_patterns("Sorry, I'm unable to refund this order."), POLICY, ORDERS.get_order("A1002"), TODAY)
    assert [w["type"] for w in allowed] == ["refund"]  # 11 days, not on sale: refusal was wrong
    assert check_refusals(extract_patterns("Sorry, I'm unable to refund this order."), POLICY, ORDERS.get_order("A1003"), TODAY) == []
    g = GuardedBot(Scripted(["I'm sorry, I can't refund order A1002."]), POLICY, ORDERS)
    r = g.reply("Refund A1002 please")
    assert r["outcome"] == "passed" and r["wrongful_refusals"][0]["type"] == "refund"
    assert any(e["event"] == "wrongful_refusal" for e in g.log)
