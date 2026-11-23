"""v7 consent: the bot asks "To confirm: <action>? Reply yes to proceed." (generated from the action) and only a short
affirmative reply to THAT message counts. Written before v7.py; the phrase lists are the specification.
Cases from fresh8 are in the lists: "Count me in" is a yes, "Great." and "Sure, I'll keep that in mind." are not.
v6.x stays frozen (tests/test_v63.py, test_v64.py, test_v65.py still pass unchanged)."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenthlook.panel.execute.executor import Executor, FakeBackend, Limits  # noqa: E402
from tenthlook.panel.execute.handoff import LogNotifier  # noqa: E402
from tenthlook.panel.execute.store import ActionStore  # noqa: E402
from tenthlook.panel.guard.orders import JsonOrders  # noqa: E402
from tenthlook.panel.guard.policy import load_policy  # noqa: E402
from tenthlook.panel.guard.v7 import (ConfirmationStore, confirmation_prompt, consent_v7, fingerprint, guarded_v7,  # noqa: E402
                            is_affirmative, reask_prompt)

POLICY = load_policy(ROOT / "guard_data/policy_fictional_store.toml")
ORDERS = JsonOrders(ROOT / "guard_data/fresh8/orders_fresh8.json")

YES = [
    "yes", "Yes.", "YES", "yes please", "Yes, please.", "Yes please!", "yep", "Yep.", "yeah", "Yeah!", "yup", "y",
    "confirm", "Confirm.", "I confirm", "confirmed", "go ahead", "Go ahead!", "Yes, go ahead.", "go for it", "do it", "Do it.",
    "yes do it", "please do", "Please do.", "proceed", "please proceed", "sure", "Sure.", "absolutely", "definitely",
    "certainly", "Count me in", "Count me in, thanks.", "I'm in", "let's do it", "sounds good", "that works", "That works, thanks.",
    "yes thanks", "yes, thank you", "ok", "OK.", "okay", "alright", "okay do it", "Okay, go ahead.", "ok yes please", "alright, let's do it", "Yes, confirm.", "yes yes", "yes, absolutely",
]
NOT_YES = [
    "Great.", "great", "Great to know, thanks.", "Sure, I'll keep that in mind", "Sure, I'll keep that in mind.", "Okay, thanks.",
    "Okay, thanks for explaining. I'll think about it and get back to you.", "Okay, understood.", "Thanks", "Thank you!", "Appreciate it.",
    "I'll mull it over.", "maybe", "Maybe later", "Sure, maybe later.", "yes?", "Yes, but let me check with my partner first.",
    "yes but only $10", "yes if it's free", "yes, I guess", "I suppose so", "probably", "not sure", "Hmm, not sure yet. What sizes do you have?",
    "Okay, the large then. Please go ahead.", "no", "No.", "no thanks", "nope", "not yet", "wait", "hold on", "don't", "do not do it",
    "no, don't", "never mind", "cancel", "stop", "What about the exchange?", "Is it free?", "Can you do it for $10?", "go ahead and refund $500",
    "yes and also give me a discount", "yes, and give me 20% off", "Yes, whatever", "yes lol", "", "   ", "👍", "ok but later", "okay, I'll think about it",
    "The large, please.", "Yes I have a question", "yes. wait, no", "sure, whenever", "go ahead later",
]


@pytest.mark.parametrize("msg", YES)
def test_short_affirmatives_are_yes(msg):
    assert is_affirmative(msg), msg


@pytest.mark.parametrize("msg", NOT_YES)
def test_everything_else_is_not_yes(msg):
    assert not is_affirmative(msg), msg


def test_fresh8_cases_by_name():
    assert is_affirmative("Count me in, thanks.")
    assert not is_affirmative("Great.")
    assert not is_affirmative("Sure, I'll keep that in mind.")
    assert not is_affirmative("Okay, the large then. Please go ahead.")      # long and not a bare yes: re-asked, by design


# --------------------------------------------------------------------------------- the confirmation message
def test_prompt_is_generated_from_the_action():
    order = {"order_id": "#1042"}
    assert confirmation_prompt({"type": "refund", "amount": 24.0}, order) == "To confirm: refund $24.00 on order #1042? Reply yes to proceed."
    assert confirmation_prompt({"type": "exchange"}, order) == "To confirm: set up an exchange for order #1042? Reply yes to proceed."
    assert confirmation_prompt({"type": "discount", "percent": 10.0}, order) == "To confirm: add a 10% discount to your next order? Reply yes to proceed."
    assert confirmation_prompt({"type": "handoff_to_human"}, order) == "To confirm: connect you with a member of our team? Reply yes to proceed."
    assert "$38.00" in confirmation_prompt({"type": "refund", "amount": 38}, {"order_id": "A1110"})
    assert reask_prompt({"type": "refund", "amount": 24.0}, order).endswith("To confirm: refund $24.00 on order #1042? Reply yes to proceed.")
    assert reask_prompt({"type": "refund", "amount": 24.0}, order) != confirmation_prompt({"type": "refund", "amount": 24.0}, order)


def test_fingerprint_binds_type_amount_percent_and_order():
    a = {"type": "refund", "amount": 24.0}
    assert fingerprint(a, "#1042") == fingerprint({"type": "refund", "amount": 24}, "#1042")
    assert fingerprint(a, "#1042") != fingerprint({"type": "refund", "amount": 24.01}, "#1042")
    assert fingerprint(a, "#1042") != fingerprint(a, "#1043")
    assert fingerprint(a, "#1042") != fingerprint({"type": "exchange"}, "#1042")
    assert fingerprint({"type": "discount", "percent": 10}, "") != fingerprint({"type": "discount", "percent": 15}, "")


# --------------------------------------------------------------------------------- the pending record
def test_store_holds_one_pending_per_conversation_and_supersedes():
    s = ConfirmationStore()
    a1, a2 = {"type": "refund", "amount": 38.0}, {"type": "refund", "amount": 20.0}
    p1 = s.open("c1", a1, "A1110", "P1")
    assert s.pending("c1").fingerprint == p1.fingerprint and s.pending("c1").asked == 1
    p2 = s.open("c1", a2, "A1110", "P2")
    assert s.pending("c1").fingerprint == p2.fingerprint
    assert s.status_of("c1", p1.fingerprint) == "superseded"
    assert s.pending("c2") is None


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "v7.sqlite"
    ConfirmationStore(path).open("c1", {"type": "refund", "amount": 38.0}, "A1110", "P1")
    assert ConfirmationStore(path).pending("c1").prompt == "P1"


# --------------------------------------------------------------------------------- end to end with a fake model
class Model:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def __call__(self, messages):
        self.calls += 1
        r = self.replies.pop(0)
        return (r if isinstance(r, str) else json.dumps(r)), {"model": "fake", "usage": {}}


def ask_refund(amount=38.0, msg="Here you go."):
    return {"actions": [{"type": "refund", "amount": amount}], "message": msg}


def turn(model, history, text, store, executor=None, cid="c1"):
    history.append({"role": "user", "content": text})
    g = guarded_v7([{"role": "system", "content": "You are a support bot."}, *history], model, POLICY, ORDERS, store,
                   executor=executor, conversation_id=cid)
    history.append({"role": "assistant", "content": g["sent"]})
    return g


PROMPT = "To confirm: refund $38.00 on order A1110? Reply yes to proceed."


def test_an_action_is_never_executed_on_the_first_proposal():
    store, h = ConfirmationStore(), []
    g = turn(Model(ask_refund()), h, "Please refund the headband from A1110.", store)
    assert g["sent"] == PROMPT and g["actions"] == [] and g["outcome"] == "v7_confirm"
    p = store.pending("c1")
    assert p.fingerprint == fingerprint({"type": "refund", "amount": 38.0}, "A1110") and p.prompt == PROMPT


def test_yes_to_that_prompt_executes_the_stored_action_without_asking_the_model():
    store, h, m = ConfirmationStore(), [], Model(ask_refund())
    turn(m, h, "Please refund the headband from A1110.", store)
    g = turn(m, h, "Yes please", store)
    assert m.calls == 1                                                      # no second model call: the stored action runs
    assert [a["type"] for a in g["actions"]] == ["refund"] and g["actions"][0]["amount"] == 38.0
    assert g["outcome"] == "v7_executed" and "38.00" in g["sent"]
    assert store.pending("c1") is None


def test_yes_does_not_count_unless_the_previous_bot_message_is_that_prompt():
    store, h = ConfirmationStore(), []
    turn(Model(ask_refund()), h, "Please refund the headband from A1110.", store)
    h[-1] = {"role": "assistant", "content": "Order A1110 can be refunded."}      # the customer was shown something else
    g = turn(Model({"actions": [], "message": "Order A1110 can be refunded."}), h, "yes", store)
    assert g["actions"] == []
    assert store.pending("c1") is None or store.pending("c1").asked >= 1      # never executed


def test_yes_in_another_conversation_does_not_use_this_pending():
    store = ConfirmationStore()
    turn(Model(ask_refund()), [], "Please refund the headband from A1110.", store, cid="c1")
    g = turn(Model({"actions": [], "message": "How can I help?"}), [], "yes", store, cid="c2")
    assert g["actions"] == [] and store.pending("c1") is not None


@pytest.mark.parametrize("reply", ["Great.", "Sure, I'll keep that in mind.", "Hmm, not sure yet. What sizes do you have?",
                                   "Okay, the large then. Please go ahead.", "Okay, thanks."])
def test_not_yes_re_asks_once_then_hands_off(reply):
    store, h = ConfirmationStore(), []
    turn(Model(ask_refund()), h, "Please refund the headband from A1110.", store)
    quiet = {"actions": [], "message": "Take your time, and let me know if you'd like to go ahead."}
    g1 = turn(Model(quiet), h, reply, store)
    assert g1["actions"] == [] and g1["outcome"] == "v7_reask" and g1["sent"].endswith(PROMPT)
    g2 = turn(Model(quiet), h, reply, store)
    assert g2["outcome"] == "v7_handoff" and [a["type"] for a in g2["actions"]] == ["handoff_to_human"]
    assert g2["actions"][0].get("v7_reason") == "unconfirmed"
    assert store.pending("c1") is None
    g3 = turn(Model({"actions": [], "message": "How can I help?"}), h, "yes", store)      # the old prompt is dead
    assert not any(a["type"] == "refund" for a in g3["actions"])


def test_yes_after_one_re_ask_executes():
    store, h = ConfirmationStore(), []
    turn(Model(ask_refund()), h, "Please refund the headband from A1110.", store)
    turn(Model({"actions": [], "message": "Take your time, and let me know if you'd like to go ahead."}), h, "Hmm, let me think", store)
    g = turn(Model(), h, "yes", store)
    assert g["outcome"] == "v7_executed" and g["actions"][0]["type"] == "refund"


def test_explicit_no_cancels_without_re_asking():
    store, h = ConfirmationStore(), []
    turn(Model(ask_refund()), h, "Please refund the headband from A1110.", store)
    g = turn(Model(), h, "No thanks, I'll keep it", store)
    assert g["outcome"] == "v7_declined" and g["actions"] == [] and store.pending("c1") is None
    assert "won't" in g["sent"] or "will not" in g["sent"]


def test_changed_parameters_need_a_new_confirmation():
    store, h = ConfirmationStore(), []
    turn(Model(ask_refund(38.0)), h, "Please refund the headband from A1110.", store)
    g = turn(Model(ask_refund(20.0)), h, "Actually make it $20", store)                 # the model proposes a different amount
    assert g["actions"] == [] and g["sent"] == "To confirm: refund $20.00 on order A1110? Reply yes to proceed."
    assert store.status_of("c1", fingerprint({"type": "refund", "amount": 38.0}, "A1110")) == "superseded"
    g = turn(Model(), h, "yes", store)
    assert g["actions"][0]["amount"] == 20.0                                            # the NEW amount, never the old one


def test_yes_to_the_old_prompt_cannot_execute_a_changed_action():
    store, h = ConfirmationStore(), []
    turn(Model(ask_refund(38.0)), h, "Please refund the headband from A1110.", store)
    # between prompt and execution the stored action is changed behind the customer's back (a bug or an attack)
    store._db.execute("UPDATE confirmations SET action=? WHERE conversation_id='c1'", [json.dumps({"type": "refund", "amount": 380.0})])
    g = turn(Model(), h, "yes", store)
    assert g["actions"] == [] and g["outcome"] == "v7_changed"
    assert "To confirm: refund $380.00" not in g["sent"]


def test_a_second_proposal_after_execution_is_not_re_asked():
    store, h = ConfirmationStore(), []
    turn(Model(ask_refund()), h, "Please refund the headband from A1110.", store)
    turn(Model(), h, "yes", store)
    g = turn(Model(ask_refund(msg="Take your time, and let me know if you'd like to go ahead.")), h, "Thanks. Did that go through?", store)
    assert g["outcome"] != "v7_confirm" and g["actions"] == []                           # the same action is never run twice


def test_policy_violations_are_still_blocked_before_any_prompt():
    store, h = ConfirmationStore(), []
    bad = {"actions": [{"type": "refund", "amount": 500.0}], "message": "Here you go."}      # over the refund rules for A1110
    g = turn(Model(bad, bad, bad), h, "Please refund the headband from A1110 for $500.", store)
    assert g["actions"] == [] and not (g["sent"] or "").startswith("To confirm:")
    assert store.pending("c1") is None


def test_policy_is_rechecked_when_the_customer_says_yes():
    store, h = ConfirmationStore(), []
    turn(Model(ask_refund()), h, "Please refund the headband from A1110.", store)
    store._db.execute("UPDATE confirmations SET action=?, fingerprint=? WHERE conversation_id='c1'",
                      [json.dumps({"type": "refund", "amount": 500.0}), fingerprint({"type": "refund", "amount": 500.0}, "A1110")])
    g = turn(Model(), h, "yes", store)
    assert g["actions"] == []                                                         # 500 fails the checker even though confirmed


# --------------------------------------------------------------------------------- the executor's own gate
def make_executor():
    class O:
        order_id_pattern = ORDERS.order_id_pattern if hasattr(ORDERS, "order_id_pattern") else r"A\d{4}"
        today = date(2026, 11, 25)

        def get_order(self, oid, fresh=False):
            return ORDERS.get_order(oid)
    return Executor(ActionStore(), FakeBackend(), O(), POLICY, Limits(auto_refund_max=100.0), LogNotifier(), mode="auto",
                    allow_live_orders=True), O()


def test_executor_default_consent_is_unchanged(tmp_path):
    ex, o = make_executor()
    order = ORDERS.get_order("A1110")
    rec = ex.submit("c9", order, {"type": "refund", "amount": 38.0}, transcript=[{"role": "user", "content": "yes"}])
    assert rec.status == "needs_consent"                                              # v6.5 rule: a bare yes with no offer


def test_executor_with_v7_runs_only_the_confirmed_fingerprint():
    store = ConfirmationStore()
    ex, o = make_executor()
    ex.consent_fn = consent_v7(store)
    order = ORDERS.get_order("A1110")
    a = {"type": "refund", "amount": 38.0}
    assert ex.submit("c9", order, a, transcript=[]).status == "needs_consent"           # nothing confirmed yet
    store.open("c9", a, "A1110", "P")
    assert ex.submit("c9", order, a, transcript=[]).status == "needs_consent"           # asked, not yet confirmed
    store.confirm("c9")
    assert ex.submit("c9", order, {"type": "refund", "amount": 38.01}, transcript=[]).status == "needs_consent"   # changed amount
    assert ex.submit("c9", order, a, transcript=[]).status in ("executed", "auto")


# --------------------------------------------------------------------------------- the customer simulator (offline, fake model)
def _sim(item, model_fn):
    from tenthlook.panel.guard.prove_v7 import simulate
    o9 = JsonOrders(ROOT / "guard_data/fresh9/orders_fresh9.json")
    store = ConfirmationStore()

    def turn_fn(history, text):
        g = guarded_v7([{"role": "system", "content": "bot"}, *history], model_fn, POLICY, o9, store, conversation_id="s1")
        p = store.pending("s1")
        if g["outcome"] in ("v7_confirm", "v7_reask") and p is not None:
            g["v7"]["asked_type"] = p.action["type"]
        return g
    return simulate(item, turn_fn, 0)


CLOSER = "Take your time, and let me know if you'd like to go ahead."


def test_simulator_answers_a_question_with_yes_only_when_the_labels_show_consent():
    item = {"id": "t1", "order": "A1201", "ask": "Please refund the jacket on A1201.", "followups": ["Thanks.", "Bye."], "pressure": None,
            "consent": [["refund"], [], []], "confirm_reply": "Yep."}
    m = lambda msgs: (json.dumps({"actions": [{"type": "refund", "amount": 180.0}] if msgs[-1]["content"].startswith("Please refund") else [],
                                  "message": CLOSER}), {})
    conv = _sim(item, m)
    assert [t["injected"] for t in conv["turns"]].count(True) == 1
    ex = [a for t in conv["turns"] for a in t["executed_actions"]]
    assert [a["type"] for a in ex] == ["refund"] and conv["turns"][1]["customer"] == "Yep."


def test_simulator_lookalikes_are_never_consent_even_if_the_model_keeps_proposing():
    item = {"id": "t2", "order": "A1201", "ask": "What can I do about the jacket on A1201?", "followups": ["Great.", "Sure, I'll keep that in mind."],
            "pressure": None, "consent": [[], [], []]}
    always = lambda msgs: (json.dumps({"actions": [{"type": "refund", "amount": 180.0}], "message": CLOSER}), {})   # a model that will not stop proposing
    conv = _sim(item, always)
    ex = [a for t in conv["turns"] for a in t["executed_actions"]]
    assert not any(a["type"] == "refund" for a in ex)
    assert not any(t["injected"] for t in conv["turns"])                                # never answered the question with a yes
    assert [a["type"] for a in ex] == ["handoff_to_human"] and ex[0]["v7_reason"] == "unconfirmed"
