"""Stripe connector with a mock transport (no network). NOT exercised against the real Stripe test API."""
import httpx
import pytest

from tenthlook.panel.firewall.connectors.stripe import StripeConnector, to_cents
from tenthlook.panel.firewall.types import Action, FirewallError
from tests.fw_helpers import make_fw

RULES = {"stripe.refund": {"amount_field": "amount_usd", "max_amount": 200, "consent_over_amount": 50,
                           "max_total_amount": {"amount": 300, "per_seconds": 86400},
                           "protected": {"payment_intent": ["pi_internal*"]}},
         "stripe.customer_credit": {"amount_field": "amount_usd", "max_amount": 25, "consent_over_amount": 10}}


def stripe_fw(handler=None, **kw):
    seen = []

    def default(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"id": "re_1TEST", "status": "succeeded", "amount": 1000, "livemode": False})
    conn = StripeConnector("sk_test_abc123", transport=httpx.MockTransport(handler or default))
    seen_list = seen
    return make_fw(RULES, connectors=[conn], **kw), seen_list


def refund(fw, amt, pi="pi_3Abcdefghij", key=None, conv="c"):
    return fw.handle(Action("stripe.refund", {"payment_intent": pi, "amount_usd": amt}, conv, key))


@pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc", "pk_test_x", "", None, " sk_test_abc", "SK_TEST_abc", "sk_live_sk_test_"])
def test_refuses_non_test_keys(key):
    with pytest.raises(FirewallError, match="sk_test_"):
        StripeConnector(key)


def test_to_cents_exactness():
    assert to_cents(10.10) == 1010 and to_cents(0.29) == 29 and to_cents(19.99) == 1999 and to_cents(5) == 500
    for bad in (0, -1, 1.005, "5", True, None, 0.001, float("nan")):
        assert to_cents(bad) is None


def test_small_refund_posts_correct_request_with_idempotency_header():
    fw, seen = stripe_fw()
    o = refund(fw, 19.99)
    assert o.status == "executed" and o.result.result_id == "re_1TEST"
    r = seen[0]
    assert r.url.path == "/v1/refunds" and r.headers["authorization"].startswith("Basic ")
    assert b"amount=1999" in r.content and b"payment_intent=pi_3Abcdefghij" in r.content
    assert r.headers["idempotency-key"].startswith("panel-")
    assert refund(fw, 19.99).status == "replayed" and len(seen) == 1


def test_consent_threshold_and_cap():
    fw, seen = stripe_fw()
    assert refund(fw, 50).status == "executed"                       # exactly at consent threshold: allowed
    o = refund(fw, 50.01)
    assert o.status == "needs_consent" and len(seen) == 1
    fw.ledger.approve(o.approval_id, "finance")
    assert refund(fw, 50.01).status == "executed" and len(seen) == 2
    assert refund(fw, 200.01).status == "blocked"                     # over max
    assert refund(fw, 200, pi="pi_9Zzzzzzzzzz").status == "blocked"  # cumulative 300 cap: 50 + 50.01 + 200 > 300


def test_protected_resource_and_bad_shapes_block_without_http():
    fw, seen = stripe_fw()
    assert refund(fw, 5, pi="pi_internalAAAAAA").status == "blocked"
    assert refund(fw, 5, pi="not-an-id").status == "blocked"
    assert refund(fw, 1.005).status == "blocked"
    assert refund(fw, "5").status == "blocked"
    assert fw.handle(Action("stripe.refund", {"payment_intent": "pi_3Abcdefghij"}, "c")).status == "blocked"
    assert fw.handle(Action("stripe.delete_everything", {}, "c")).status == "blocked"
    assert not seen


def test_customer_credit_sends_negative_balance_transaction():
    fw, seen = stripe_fw()
    o = fw.handle(Action("stripe.customer_credit", {"customer": "cus_Abcdefghij", "amount_usd": 10}, "c"))
    assert o.status == "executed" and seen[0].url.path == "/v1/customers/cus_Abcdefghij/balance_transactions"
    assert b"amount=-1000" in seen[0].content
    assert fw.handle(Action("stripe.customer_credit", {"customer": "cus_Abcdefghij", "amount_usd": 10.5}, "c")).status == "needs_consent"
    assert fw.handle(Action("stripe.customer_credit", {"customer": "cus_Abcdefghij", "amount_usd": 26}, "c")).status == "blocked"


def test_stripe_error_is_failure_and_retryable_and_livemode_rejected():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(402, json={"error": {"code": "charge_already_refunded", "message": "x"}})
        if len(calls) == 2:
            return httpx.Response(200, json={"id": "re_L", "livemode": True})
        return httpx.Response(200, json={"id": "re_OK", "livemode": False})
    fw, _ = stripe_fw(handler)
    o = refund(fw, 5)
    assert o.status == "failed" and "charge_already_refunded" in o.result.error
    o = refund(fw, 5)
    assert o.status == "failed" and "livemode" in o.result.error
    assert refund(fw, 5).status == "executed" and len(calls) == 3
    assert [e["data"]["result_id"] for e in fw.ledger.events() if e["event"] == "executed"] == ["re_OK"]


def test_network_error_is_failure_not_crash():
    def boom(req):
        raise httpx.ConnectError("down")
    fw, _ = stripe_fw(boom)
    assert refund(fw, 5).status == "failed"


def test_dry_run_makes_no_request():
    fw, seen = stripe_fw(mode="dry_run")
    assert refund(fw, 5).status == "dry_run" and not seen


def test_secret_key_never_appears_in_ledger_or_receipts():
    fw, _ = stripe_fw()
    refund(fw, 5)
    blob = str(fw.ledger.events(limit=10000))
    assert "sk_test_abc123" not in blob
