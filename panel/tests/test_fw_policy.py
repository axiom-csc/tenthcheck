"""Rule engine: every rule type + edge cases (amount exactly at limit, missing fields, unknown action)."""
import pytest

from tenthlook.panel.firewall.policy import LedgerState, evaluate, parse_policy
from tenthlook.panel.firewall.types import ALLOW, BLOCK, NEEDS_CONSENT, Action, FirewallError

R = {"max_amount": 100, "consent_over_amount": 50}


def ev(rule, args=None, facts=None, state=None, now=1000.0, tool="t"):
    return evaluate(Action(tool, args if args is not None else {}, "c"), rule, facts or {}, state or LedgerState(), now)


def test_unknown_action_blocks():
    v = ev(None, {"amount": 1})
    assert v.decision == BLOCK and "unknown action" in v.reasons[0]


def test_not_allowed_blocks():
    assert ev({"allowed": False}).decision == BLOCK


def test_amount_boundaries():
    assert ev(R, {"amount": 50}).decision == ALLOW           # exactly at the consent threshold: no consent needed
    assert ev(R, {"amount": 50.01}).decision == NEEDS_CONSENT
    assert ev(R, {"amount": 100}).decision == NEEDS_CONSENT  # exactly at the max: allowed, but over consent threshold
    assert ev({"max_amount": 100}, {"amount": 100}).decision == ALLOW   # exactly at max is allowed
    assert ev({"max_amount": 100}, {"amount": 100.01}).decision == BLOCK


@pytest.mark.parametrize("bad", ["10", None, True, -1, float("nan"), float("inf"), [1], {"a": 1}])
def test_bad_amount_types_block(bad):
    assert ev(R, {"amount": bad}).decision == BLOCK


def test_missing_amount_and_required_fields():
    assert ev(R, {}).decision == BLOCK
    assert ev({"required_fields": ["order_id"]}, {"x": 1}).decision == BLOCK
    assert ev({"required_fields": ["order_id"]}, {"order_id": None}).decision == BLOCK
    assert ev({"required_fields": ["order_id"]}, {"order_id": "1"}).decision == ALLOW


def test_custom_amount_field():
    r = {"amount_field": "amount_usd", "max_amount": 10}
    assert ev(r, {"amount_usd": 10}).decision == ALLOW and ev(r, {"amount_usd": 11}).decision == BLOCK
    assert ev(r, {"amount": 1}).decision == BLOCK


def test_rows():
    r = {"max_rows": 10, "consent_over_rows": 5}
    assert ev(r, facts={"rows": 5}).decision == ALLOW
    assert ev(r, facts={"rows": 6}).decision == NEEDS_CONSENT
    assert ev(r, facts={"rows": 10}).decision == NEEDS_CONSENT
    assert ev(r, facts={"rows": 11}).decision == BLOCK
    assert ev(r, facts={}).decision == BLOCK            # unknown count fails closed


def test_protected_globs_case_insensitive():
    r = {"protected": {"customer": ["cus_vip*", "CUS_ADMIN"]}}
    assert ev(r, {"customer": "CUS_VIP_1"}).decision == BLOCK
    assert ev(r, {"customer": "cus_admin"}).decision == BLOCK
    assert ev(r, {"customer": "cus_ok"}).decision == ALLOW
    assert ev(r, {}).decision == ALLOW


def test_consent_if_and_block_if_operators():
    r = {"consent_if": [{"field": "reason", "op": "eq", "value": "fraud"}, {"field": "n", "op": "gt", "value": 3}],
         "block_if": [{"field": "dest", "op": "matches", "value": "^evil"}]}
    assert ev(r, {"reason": "fraud", "n": 1}).decision == NEEDS_CONSENT
    assert ev(r, {"reason": "x", "n": 4}).decision == NEEDS_CONSENT
    assert ev(r, {"reason": "x", "n": 1}).decision == ALLOW
    assert ev(r, {"reason": "x", "n": 1, "dest": "evil.com"}).decision == BLOCK
    assert ev(r, {"reason": "x", "n": "4"}).decision == NEEDS_CONSENT   # unevaluable condition -> human decides


def test_rate_limit_window():
    r = {"rate_limit": {"max": 2, "per_seconds": 60}}
    st = LedgerState([(950.0, None), (990.0, None)])
    assert ev(r, state=st, now=1000).decision == BLOCK
    assert ev(r, state=st, now=1020).decision == ALLOW        # 950 has left the 60s window
    assert ev(r, state=LedgerState([(990.0, None)]), now=1000).decision == ALLOW


def test_cumulative_cap():
    r = {"max_total_amount": {"amount": 100, "per_seconds": 3600}}
    st = LedgerState([(900.0, 60.0), (950.0, 30.0)])
    assert ev(r, {"amount": 10}, state=st).decision == ALLOW    # exactly 100
    assert ev(r, {"amount": 10.5}, state=st).decision == BLOCK


def test_blocks_dominate_consent_and_reasons_are_readable():
    v = ev({"max_amount": 10, "consent_if": [{"field": "a", "op": "eq", "value": 1}]}, {"amount": 11, "a": 1})
    assert v.decision == BLOCK and all(isinstance(x, str) and x for x in v.reasons)


def test_pure_function_same_inputs_same_verdict():
    a = ev(R, {"amount": 75})
    assert all(ev(R, {"amount": 75}).to_dict() == a.to_dict() for _ in range(20))


def test_parse_policy_rejects_typos_and_bad_values():
    with pytest.raises(FirewallError):
        parse_policy({"actions": {"x": {"max_ammount": 5}}})          # typo must not silently drop a cap
    with pytest.raises(FirewallError):
        parse_policy({"actions": {"x": {"max_amount": "5"}}})
    with pytest.raises(FirewallError):
        parse_policy({"actions": {"x": {"max_amount": -1}}})
    with pytest.raises(FirewallError):
        parse_policy({"mode": "yolo", "actions": {"x": {}}})
    with pytest.raises(FirewallError):
        parse_policy({"actions": {}})
    with pytest.raises(FirewallError):
        parse_policy({"actions": {"x": {"rate_limit": {"max": 1}}}})
    with pytest.raises(FirewallError):
        parse_policy({"surprise": 1, "actions": {"x": {}}})
