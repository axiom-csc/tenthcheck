"""Shopify client, token, order lookup and the two write mutations, against a mocked Shopify (no network)."""
import json
import logging
import os
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenthlook.panel.shopify import API_VERSION  # noqa: E402
from tenthlook.panel.shopify.actions import UUID_RE, create_discount_code, create_refund, refund_transaction, shopify_key  # noqa: E402
from tenthlook.panel.shopify.client import ShopifyClient, ShopifyError, TokenProvider  # noqa: E402
from tenthlook.panel.shopify.config import Config, ConfigError  # noqa: E402
from tenthlook.panel.shopify.orders import SHOPIFY_ORDER_ID, ShopifyOrders  # noqa: E402
from tenthlook.panel.guard.orders import find_order_id  # noqa: E402

SECRET = "shpss_TEST-CLIENT-SECRET-never-log-me"
TOKEN = "shpat_TEST-ACCESS-TOKEN-never-log-me"
ENV = {"SHOPIFY_STORE": "panel-dev.myshopify.com", "SHOPIFY_CLIENT_ID": "cid-123", "SHOPIFY_CLIENT_SECRET": SECRET}


def order_node(name="#1001", total="54.00", refunded="0.00", compare_at=None, unit="54.00", test=True, processed="2026-11-06T10:00:00Z"):
    return {"id": "gid://shopify/Order/1", "name": name, "test": test, "processedAt": processed, "createdAt": processed,
            "cancelledAt": None, "displayFinancialStatus": "PAID",
            "totalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "USD"}},
            "totalRefundedSet": {"shopMoney": {"amount": refunded, "currencyCode": "USD"}},
            "lineItems": {"nodes": [{"name": "Canvas tote", "quantity": 1, "originalUnitPriceSet": {"shopMoney": {"amount": unit}},
                                     "variant": {"compareAtPrice": compare_at}}]},
            "transactions": [{"id": "gid://shopify/OrderTransaction/9", "kind": "SALE", "status": "SUCCESS",
                              "gateway": "bogus", "test": True, "amountSet": {"shopMoney": {"amount": total}},
                              "maximumRefundableV2": {"amount": str(float(total) - float(refunded)), "currencyCode": "USD"}}]}


class FakeShopify:
    """Records every request; answers the token endpoint and GraphQL by operation."""

    def __init__(self, orders=None, refund_errors=None, throttle_first=0, token_status=200):
        self.requests, self.orders, self.refund_errors = [], orders or {}, refund_errors
        self.throttle_left, self.token_status, self.token_calls = throttle_first, token_status, 0

    def __call__(self, method, url, headers, body):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        if url.endswith("/admin/oauth/access_token"):
            self.token_calls += 1
            if self.token_status != 200:
                return self.token_status, json.dumps({"error": "invalid_client", "client_secret": SECRET}).encode()
            return 200, json.dumps({"access_token": TOKEN, "scope": "write_orders,write_discounts", "expires_in": 86399}).encode()
        q = json.loads(body)
        if self.throttle_left:
            self.throttle_left -= 1
            return 200, json.dumps({"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}).encode()
        if "OrderByName" in q["query"]:
            digits = re.sub(r"\D", "", q["variables"]["q"])
            want_hash = "#" in q["variables"]["q"]
            o = self.orders.get(digits)
            nodes = [o] if o and want_hash == self.orders.get("_hash", True) else []
            return 200, json.dumps({"data": {"orders": {"nodes": nodes}}}).encode()
        if "refundCreate" in q["query"]:
            errs = self.refund_errors or []
            return 200, json.dumps({"data": {"refundCreate": {"refund": None if errs else {
                "id": "gid://shopify/Refund/77", "totalRefundedSet": {"shopMoney": {"amount": q["variables"]["input"]["transactions"][0]["amount"], "currencyCode": "USD"}}},
                "userErrors": errs}}}).encode()
        if "discountCodeBasicCreate" in q["query"]:
            return 200, json.dumps({"data": {"discountCodeBasicCreate": {"codeDiscountNode": {"id": "gid://shopify/DiscountCodeNode/5"},
                                                                         "userErrors": []}}}).encode()
        return 400, b"{}"


def client(fake):
    return ShopifyClient(Config(env=ENV, secrets_file="/nonexistent"), transport=fake, sleep=lambda s: None,
                         tokens=TokenProvider(Config(env=ENV, secrets_file="/nonexistent"), transport=fake))


# ---------------------------------------------------------------------------------------------- config / secrets
def test_secret_file_must_be_private(tmp_path):
    f = tmp_path / "shopify.env"
    f.write_text(f"SHOPIFY_STORE=panel-dev.myshopify.com\nSHOPIFY_CLIENT_SECRET={SECRET}\n")
    os.chmod(f, 0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        Config(env={}, secrets_file=f)
    os.chmod(f, 0o600)
    c = Config(env={}, secrets_file=f)
    assert c.store == "panel-dev.myshopify.com" and c.require("SHOPIFY_CLIENT_SECRET") == SECRET
    assert SECRET not in repr(c) and SECRET not in repr(c.get("SHOPIFY_CLIENT_SECRET")) and "SHOPIFY_CLIENT_SECRET" in c.present()


def test_environment_overrides_file_and_store_must_be_myshopify(tmp_path):
    f = tmp_path / "s.env"
    f.write_text("SHOPIFY_STORE=a.myshopify.com\n")
    os.chmod(f, 0o600)
    assert Config(env={"SHOPIFY_STORE": "b.myshopify.com"}, secrets_file=f).store == "b.myshopify.com"
    with pytest.raises(ConfigError):
        Config(env={"SHOPIFY_STORE": "realshop.com"}, secrets_file=f).store


# ---------------------------------------------------------------------------------------------- token + client
def test_client_credentials_grant_cached_and_refreshed():
    fake, now = FakeShopify(orders={"1001": order_node()}), [1000.0]
    tp = TokenProvider(Config(env=ENV, secrets_file="/x"), transport=fake, clock=lambda: now[0])
    assert tp.token() == TOKEN and tp.token() == TOKEN and fake.token_calls == 1
    form = urllib.parse.parse_qs(fake.requests[0]["body"].decode())
    assert form == {"grant_type": ["client_credentials"], "client_id": ["cid-123"], "client_secret": [SECRET]}
    assert "scope" not in form and tp.scopes == ["write_discounts", "write_orders"]
    now[0] += 86399 - 200  # within 5 minutes of expiry
    tp.token()
    assert fake.token_calls == 2


def test_graphql_request_shape_and_throttle_retry():
    fake = FakeShopify(orders={"1001": order_node()}, throttle_first=1)
    c = client(fake)
    c.graphql("query OrderByName($q: String!) { orders(first:1, query:$q) { nodes { id } } }", {"q": "name:#1001"})
    gql = [r for r in fake.requests if "graphql.json" in r["url"]]
    assert gql[0]["url"] == f"https://panel-dev.myshopify.com/admin/api/{API_VERSION}/graphql.json" and API_VERSION == "2026-10"
    assert gql[0]["headers"]["X-Shopify-Access-Token"] == TOKEN and len(gql) == 2  # retried once after THROTTLED


def test_token_failure_message_never_contains_the_secret(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeShopify(token_status=400)
    with pytest.raises(ShopifyError) as e:
        client(fake).graphql("query { shop { name } }")
    assert SECRET not in str(e.value) and SECRET not in caplog.text and TOKEN not in caplog.text


# ---------------------------------------------------------------------------------------------- order lookup
def test_order_lookup_maps_facts():
    fake = FakeShopify(orders={"1001": order_node(total="120.00", refunded="20.00", unit="120.00", compare_at="150.00")})
    o = ShopifyOrders(client(fake)).get_order("1001")
    assert o["order_id"] == "#1001" and o["purchase_date"] == "2026-11-06" and o["item"] == "Canvas tote"
    assert o["amount"] == 100.0 and o["amount_paid"] == 120.0  # what can still be refunded
    assert o["on_sale"] is True and o["has_receipt"] is True and o["test"] is True


def test_order_lookup_tries_name_without_hash_and_caches():
    fake = FakeShopify(orders={"1002": order_node(name="#1002"), "_hash": False})
    orders = ShopifyOrders(client(fake))
    assert orders.get_order("#1002")["order_id"] == "#1002"
    n = len(fake.requests)
    orders.get_order("1002")
    assert len(fake.requests) == n  # cached
    orders.get_order("1002", fresh=True)
    assert len(fake.requests) > n
    assert ShopifyOrders(client(FakeShopify())).get_order("9999") is None


def test_shopify_order_id_pattern():
    p = SHOPIFY_ORDER_ID
    assert find_order_id("Refund order #1001 please", pattern=p) == "1001"
    assert find_order_id("my order 1002 arrived late", pattern=p) == "1002"
    assert find_order_id("order number 1003", pattern=p) == "1003"
    assert find_order_id("I paid $118 thirty days ago, 2026 was rough", pattern=p) is None


# ---------------------------------------------------------------------------------------------- writes
def test_refund_mutation_is_idempotent_and_targets_the_sale_transaction():
    fake = FakeShopify(orders={"1001": order_node()})
    c = client(fake)
    order = ShopifyOrders(c).get_order("1001")
    key = shopify_key("panel-key-abc")
    assert UUID_RE.match(key) and shopify_key("panel-key-abc") == key  # deterministic
    out = create_refund(c, order, 54.0, key, note="Panel refund")
    req = json.loads(fake.requests[-1]["body"])
    assert f'@idempotent(key: "{key}")' in req["query"] and "refundCreate(input: $input)" in req["query"]
    t = req["variables"]["input"]["transactions"][0]
    assert t == {"orderId": "gid://shopify/Order/1", "parentId": "gid://shopify/OrderTransaction/9", "amount": "54.00",
                 "gateway": "bogus", "kind": "REFUND"}
    assert out["refund_id"] == "gid://shopify/Refund/77"


def test_refund_user_errors_and_no_covering_transaction_raise():
    fake = FakeShopify(orders={"1001": order_node()}, refund_errors=[{"field": ["amount"], "message": "too much"}])
    c = client(fake)
    order = ShopifyOrders(c).get_order("1001")
    with pytest.raises(ShopifyError, match="too much"):
        create_refund(c, order, 10.0, shopify_key("k"), note="x")
    with pytest.raises(ShopifyError, match="no single successful payment"):
        create_refund(c, order, 999.0, shopify_key("k2"), note="x")
    with pytest.raises(ValueError):
        create_refund(c, order, 1.0, "not-a-uuid", note="x")


def test_refund_targets_the_capture_of_an_authorize_then_capture_order():
    # What the Bogus gateway produced on the real dev store: AUTHORIZATION, then CAPTURE of $790.12. Shopify sends
    # maximumRefundableV2 as null on both (it is only set on SuggestedRefund transactions).
    auth = {"id": "gid://shopify/OrderTransaction/20", "kind": "AUTHORIZATION", "status": "SUCCESS", "gateway": "bogus",
            "test": True, "amountSet": {"shopMoney": {"amount": "790.12"}}, "parentTransaction": None,
            "maximumRefundableV2": None}
    cap = {"id": "gid://shopify/OrderTransaction/21", "kind": "CAPTURE", "status": "SUCCESS", "gateway": "bogus",
           "test": True, "amountSet": {"shopMoney": {"amount": "790.12"}}, "parentTransaction": {"id": auth["id"]},
           "maximumRefundableV2": None}
    node = order_node(total="790.12", unit="790.12")
    node["transactions"] = [auth, cap]
    fake = FakeShopify(orders={"1001": node})
    c = client(fake)
    order = ShopifyOrders(c).get_order("1001")
    create_refund(c, order, 1.00, shopify_key("cap"), note="Panel refund")
    t = json.loads(fake.requests[-1]["body"])["variables"]["input"]["transactions"][0]
    assert t == {"orderId": "gid://shopify/Order/1", "parentId": cap["id"], "amount": "1.00", "gateway": "bogus",
                 "kind": "REFUND"}

    # Earlier successful refunds against the capture shrink what is left on it; failed ones do not.
    done = {"id": "gid://shopify/OrderTransaction/22", "kind": "REFUND", "status": "SUCCESS", "gateway": "bogus",
            "amountSet": {"shopMoney": {"amount": "790.00"}}, "parentTransaction": {"id": cap["id"]}}
    failed = dict(done, id="gid://shopify/OrderTransaction/23", status="FAILURE", amountSet={"shopMoney": {"amount": "0.12"}})
    order["transactions"] = [auth, cap, done, failed]
    assert refund_transaction(order, 0.12)["id"] == cap["id"]
    with pytest.raises(ShopifyError, match="no single successful payment"):
        refund_transaction(order, 0.13)
    # An authorization alone (never captured) is not refundable.
    order["transactions"] = [auth]
    with pytest.raises(ShopifyError, match="no single successful payment"):
        refund_transaction(order, 1.00)


def test_discount_code_mutation():
    fake = FakeShopify(orders={"1001": order_node()})
    c = client(fake)
    out = create_discount_code(c, {"order_id": "#1001"}, 10, code="PANEL-ABCD1234")
    d = json.loads(fake.requests[-1]["body"])["variables"]["d"]
    assert d["customerGets"] == {"value": {"percentage": 0.1}, "items": {"all": True}}
    assert d["code"] == "PANEL-ABCD1234" and d["usageLimit"] == 1 and d["appliesOncePerCustomer"] is True
    assert "customerSelection" not in d and "context" in d  # customerSelection is deprecated
    assert out == {"discount_id": "gid://shopify/DiscountCodeNode/5", "code": "PANEL-ABCD1234"}


def test_dev_store_demo_script_end_to_end_against_mock(monkeypatch, tmp_path, capsys):
    """The step-by-step dev-store script, with the network replaced by FakeShopify and the typed answers scripted."""
    import importlib.util
    from datetime import date, timedelta
    import tenthlook.panel.shopify.client as C
    today = (date.today() - timedelta(days=1)).isoformat() + "T10:00:00Z"
    fake = FakeShopify(orders={"1001": order_node(processed=today)})
    monkeypatch.setattr(C, "urllib_transport", fake)
    monkeypatch.setattr(C.TokenProvider.__init__, "__defaults__", (fake, C.time.time))
    monkeypatch.setattr(C.ShopifyClient.__init__, "__defaults__", (None, fake, C.API_VERSION, lambda s: None))
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PANEL_SECRETS_FILE", str(tmp_path / "none.env"))
    answers = iter(["panel-dev.myshopify.com", "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts/shopify_dev_refund_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setattr(sys, "argv", ["demo", "--order", "1001", "--amount", "1", "--db", str(tmp_path / "l.sqlite")])
    demo.main()
    out = capsys.readouterr().out
    refunds = [r for r in fake.requests if b"refundCreate" in (r["body"] or b"")]
    assert len(refunds) == 1 and "status=executed" in out and "Shopify was not called again" in out
    assert SECRET not in out and TOKEN not in out
