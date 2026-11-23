"""v6.1 phantom hand-off rule (proxy only; v6.py stays frozen)."""
import hashlib
import re
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tenthlook.panel.guard import proxy as P  # noqa: E402
from tenthlook.panel.guard.orders import JsonOrders  # noqa: E402
from tenthlook.panel.guard.policy import load_policy  # noqa: E402
from tenthlook.panel.guard.v6 import REFUSALS, TEMPLATES  # noqa: E402
from tenthlook.panel.guard.v61 import phantom_handoff  # noqa: E402
from test_proxy import KEY, MSGS, post, run  # noqa: E402,F401  (run is the proxy + fake-upstream fixture)

POLICY = load_policy(ROOT / "guard_data/policy_fictional_store.toml")
ORDERS = ROOT / "guard_data/orders_fictional.json"
HANDOFF = TEMPLATES["handoff_to_human"]
PHANTOM = json.dumps({"actions": [], "message": f"{REFUSALS[0]} {HANDOFF}"})
REAL = json.dumps({"actions": [{"type": "handoff_to_human"}], "message": f"{REFUSALS[0]} {HANDOFF}"})


def _undo_rename(s):
    s = re.sub(r'tenthlook.panel\.', 'panel.', s)
    s = re.sub(r'src/tenthlook/panel/', 'panel/', s)
    return re.sub(r'usepanel (?=(?:init|proxy|serve|keygen|approve|deny|pending|ui|verify-receipt|verify-chain|export-receipts|check-reply)\b)', 'panel ', s)


def test_v6_is_still_frozen():
    want = (ROOT / "guard_data/fresh4/FROZEN_V6.sha256").read_text().split()[0]
    assert hashlib.sha256(_undo_rename((ROOT.parent / "src/tenthlook/panel/guard/v6.py").read_text(encoding="utf-8")).encode()).hexdigest() == want


@pytest.mark.parametrize("s", [
    HANDOFF,
    "I've passed your request on to a member of our team.",
    "A member of our team will contact you by email.",
    "Someone will contact you within 24 hours.",
    "Someone from our support team will be in touch shortly.",
    "I've escalated this to my supervisor.",
    "Your case has been escalated.",
    "I've forwarded your details to our returns team.",
    "Our team will review it and get back to you.",
    "Let me transfer you to a manager.",
    "I'll connect you with a specialist now.",
    "They'll reach out to you tomorrow.",
    "A specialist is now reviewing your case.",
])
def test_handoff_claim_without_action_is_flagged(s):
    assert phantom_handoff(s, []) == [s]


@pytest.mark.parametrize("s", [
    "I can connect you with a member of our team who can review your request.",  # v6 approved offer
    "Would you like me to connect you with a member of our team?",                # v6 approved offer
    POLICY.fallback_reply,
    "I can't transfer you to a manager.",
    "I'm not able to escalate this.",
    "I've set up an exchange for order A1001, and you'll get a prepaid return label by email.",
    "Thanks for your patience! Is there anything else about the boots I can help with?",
])
def test_offers_refusals_and_other_sentences_are_not_flagged(s):
    assert phantom_handoff(s, []) == []


def test_handoff_claim_with_action_is_fine():
    assert phantom_handoff(HANDOFF, [{"type": "handoff_to_human"}]) == []


def test_proxy_blocks_phantom_handoff_and_regenerates(run):  # noqa: F811
    url, seen, log = run([PHANTOM, REAL])
    r = post(url, {"model": "gpt-x", "messages": MSGS})
    g = r["panel_guard"]
    assert g["guard"] == "guard-v6.5-proxy"
    assert g["outcome"] == "passed_after_regeneration" and g["regenerations"] == 1
    assert g["actions"] == [{"type": "handoff_to_human"}]
    fb = seen[1]["body"]["messages"][-1]["content"]
    assert fb.startswith("Your reply was blocked") and "handoff_to_human" in fb
    rec = json.loads(log.read_text().splitlines()[0])
    assert "hand" in rec["blocked"][0]["issues"][0]


def test_proxy_falls_back_if_phantom_handoff_persists(run):  # noqa: F811
    url, seen, _ = run([PHANTOM])
    r = post(url, {"model": "gpt-x", "messages": MSGS})
    assert r["panel_guard"]["outcome"] == "fallback" and len(seen) == 3
    assert r["choices"][0]["message"]["content"] == POLICY.fallback_reply


def test_proxy_passes_real_handoff_first_try(run):  # noqa: F811
    url, seen, log = run([REAL])
    r = post(url, {"model": "gpt-x", "messages": MSGS})
    assert r["panel_guard"]["outcome"] == "passed" and len(seen) == 1 and not log.exists()


def test_guarded_differs_from_frozen_v6_only_by_the_new_rule():
    """v6 alone would send the phantom reply; v6.1 regenerates it."""
    from tenthlook.panel.guard.v6 import GuardedBotV6

    class S:
        def __init__(self, script):
            self.script, self.n = script, 0

        def complete(self, messages):
            self.n += 1
            return self.script[min(self.n - 1, len(self.script) - 1)], {"usage": None}

    msg = "I want a refund for order A1001."
    v6 = GuardedBotV6(S([PHANTOM, REAL]), POLICY, JsonOrders(ORDERS)).reply(msg)
    v61 = P.guarded([{"role": "user", "content": msg}], S([PHANTOM, REAL]).complete, POLICY, JsonOrders(ORDERS))
    assert v6["outcome"] == "passed" and v6["executed_actions"] == []
    assert v61["outcome"] == "passed_after_regeneration" and v61["actions"] == [{"type": "handoff_to_human"}]


@pytest.mark.parametrize("s", [  # offers leave the choice to the customer: they must pass
    "I can pass your request to a team member, who will contact you by email.",
    "If you'd like, I can pass this to a team member to review, and they'll contact you by email.",
    "Would you like me to pass this to our team so someone will contact you?",
    "I can escalate your report to our team for review and have them contact you by email.",
    "We can offer up to 10% off your next order instead, or pass your request to a team member who will contact you by email.",
    "Shall I ask a member of our team to contact you?",
    "Sure, I can connect you with a manager, who will be in touch.",
])
def test_offers_are_not_phantom_handoffs(s):
    assert phantom_handoff(s, []) == []


@pytest.mark.parametrize("s", [  # completed or definite claims with no action must still block
    "I've passed your request to a member of our team, who will contact you by email.",
    "I can confirm I've passed your request to our team, who will contact you.",
    "I'll pass your request to our team, who will contact you by email.",
    "Your request has been passed to our team for review.",
    "A member of our team will review your request and contact you by email.",
    "I can let you know it has already been escalated to a manager.",
])
def test_completed_handoff_claims_still_block(s):
    assert phantom_handoff(s, []) == [s]
