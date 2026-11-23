"""Shopify as connector #1: same deterministic checks and the same backend as the legacy executor."""
from datetime import date
from pathlib import Path

from tenthlook.panel.execute.executor import FakeBackend
from tenthlook.panel.firewall.connectors.shopify import ShopifyConnector
from tenthlook.panel.firewall.types import Action
from tenthlook.panel.guard.policy import load_policy as load_store_policy
from tests.fw_helpers import make_fw

ROOT = Path(__file__).resolve().parents[1]
STORE = load_store_policy(ROOT / "guard_data/policy_fictional_store.toml")


class Orders:
    def __init__(self, **over):
        self.today = date(2026, 11, 17)
        self.o = {"order_id": "#1001", "purchase_date": "2026-11-06", "amount": 54.0, "amount_paid": 54.0, "on_sale": False,
                  "has_receipt": True, "item": "canvas tote", "test": True, **over}

    def get_order(self, order_id, fresh=False):
        return dict(self.o) if order_id.lstrip("#") == "1001" else None


def fw_for(orders=None, backend=None, **kw):
    be = backend or FakeBackend()
    conn = ShopifyConnector(be, orders or Orders(), STORE)
    rules = {"shopify.refund": {"max_amount": 100, "consent_over_amount": 30, "required_fields": ["order_id"]},
             "shopify.discount": {"amount_field": "percent", "max_amount": 20}}
    return make_fw(rules, connectors=[conn], **kw), be


def call(fw, tool="shopify.refund", conv="c", **a):
    return fw.handle(Action(tool, a, conv))


def test_describe_actions_lists_both_actions():
    names = {a.name for a in ShopifyConnector(FakeBackend(), Orders()).describe_actions()}
    assert names == {"shopify.refund", "shopify.discount"}


def test_small_refund_executes_once_through_the_legacy_backend():
    fw, be = fw_for()
    o = call(fw, order_id="#1001", amount=20.0)
    assert o.status == "executed" and len(be.calls) == 1 and o.result.result_id == "fake-1"
    assert call(fw, order_id="#1001", amount=20.0).status == "replayed" and len(be.calls) == 1


def test_big_refund_needs_consent_then_runs_after_approval():
    fw, be = fw_for()
    o = call(fw, order_id="#1001", amount=45.0)
    assert o.status == "needs_consent" and not be.calls
    fw.ledger.approve(o.approval_id, "ops")
    assert call(fw, order_id="#1001", amount=45.0).status == "executed" and len(be.calls) == 1


def test_store_policy_still_enforced_refund_over_paid_amount_and_unknown_order_and_live_order():
    fw, be = fw_for()
    assert call(fw, order_id="#1001", amount=99.0).status == "blocked"          # more than the $54 order
    assert call(fw, order_id="#9999", amount=5.0).status == "blocked"            # order not found
    fw2, be2 = fw_for(Orders(test=False))
    o = call(fw2, order_id="#1001", amount=5.0)
    assert o.status == "blocked" and "test order" in " ".join(o.verdict.reasons)
    assert not be.calls and not be2.calls


def test_old_purchase_is_blocked_by_store_policy_window():
    fw, be = fw_for(Orders(purchase_date="2026-01-01"))
    assert call(fw, order_id="#1001", amount=5.0).status == "blocked" and not be.calls


def test_backend_failure_can_be_retried():
    fw, be = fw_for(backend=FakeBackend(fail=True))
    assert call(fw, order_id="#1001", amount=5.0).status == "failed"
    be.fail = False
    assert call(fw, order_id="#1001", amount=5.0).status == "executed"


def test_dry_run_calls_nothing():
    fw, be = fw_for(mode="dry_run")
    assert call(fw, order_id="#1001", amount=5.0).status == "dry_run" and not be.calls
