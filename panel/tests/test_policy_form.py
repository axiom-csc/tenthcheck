"""Policy setup form: validation, generated files load and enforce what the form says, server endpoints."""
import json
import sys
import tempfile
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tenthlook.panel.guard import server  # noqa: E402
from tenthlook.panel.guard.check import check_reply  # noqa: E402
from tenthlook.panel.guard.policy import load_policy  # noqa: E402
from tenthlook.panel.guard.policy_form import DEFAULTS, build  # noqa: E402

TODAY = date(2026, 11, 12)
ORDER = {"order_id": "B1", "purchase_date": "2026-11-01", "amount": 300.0, "on_sale": False, "has_receipt": True}


def load(toml):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(toml)
    return load_policy(f.name)


def ok(claims, policy, order=ORDER):
    return check_reply([dict(c, status="promise") for c in claims], policy, order, TODAY)["ok"]


def test_defaults_need_a_store_name_then_build_a_valid_policy():
    r = build({})
    assert not r["ok"] and r["errors"] == {"store_name": "Required."}
    r = build({"store_name": 'Harbor "Bike" Co.'})
    assert r["ok"] and r["rules"] == 9
    p = load(r["toml"])
    assert p.name == 'Harbor "Bike" Co.' and len(p.rule_ids) == 9
    assert "Refunds are only available within 30 days of purchase." in r["summary"]
    assert any("no escalation threshold" in s for s in r["summary"])


def test_generated_rules_enforce_the_form_answers():
    r = build({"store_name": "S", "refund_window_days": 14, "refund_max_amount": "150", "escalate_over": "100",
               "discount_max_percent": 15, "exchanges_allowed": False, "no_compensation": False})
    p = load(r["toml"])
    assert ok([{"type": "refund", "amount": 90}], p)
    assert not ok([{"type": "refund", "amount": 120}], p)  # over escalation threshold
    assert not ok([{"type": "refund", "amount": 90}], p, {**ORDER, "purchase_date": "2026-10-20"})  # 23 days > 14
    assert ok([{"type": "discount", "percent": 15}], p) and not ok([{"type": "discount", "percent": 16}], p)
    assert not ok([{"type": "exchange"}], p)
    assert ok([{"type": "compensation"}], p)
    assert not ok([{"type": "refund", "amount": 50}], p, {**ORDER, "on_sale": True})


def test_validation_messages():
    r = build({"store_name": "S", "refund_window_days": "abc", "refund_max_amount": "-5", "discount_max_percent": 140,
               "exchange_window_days": 0, "fallback_reply": "", "unverified": "maybe"})
    assert set(r["errors"]) == {"refund_window_days", "refund_max_amount", "discount_max_percent", "exchange_window_days",
                                "fallback_reply", "unverified"}
    assert not r["toml"]


def test_warnings_and_never_rules():
    r = build({"store_name": "S", "refund_max_amount": 100, "escalate_over": 200, "discount_max_percent": 0,
               "refunds_allowed": True})
    assert r["ok"] and any("no refund will ever be escalated" in w for w in r["warnings"])
    p = load(r["toml"])
    assert not ok([{"type": "discount", "percent": 1}], p)
    r2 = build({"store_name": "S", "refunds_allowed": False})
    assert not ok([{"type": "refund", "amount": 1}], load(r2["toml"]))


def test_form_page_and_build_endpoint_and_v6_session():
    app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=app.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{app.server_address[1]}"
    try:
        assert "Panel Guard policy setup" in urllib.request.urlopen(url + "/policy").read().decode()
        assert 'id="gver"' in urllib.request.urlopen(url + "/").read().decode()
        req = urllib.request.Request(url + "/api/policy-build", data=json.dumps({**DEFAULTS, "store_name": "S"}).encode(), method="POST")
        d = json.loads(urllib.request.urlopen(req).read())
        assert d["ok"] and d["toml"].startswith("# Policy generated")
        req = urllib.request.Request(url + "/api/session", method="POST", data=json.dumps(
            {"policy": d["toml"], "guard": "v6", "base_url": "http://127.0.0.1:9/v1", "model": "x"}).encode())
        s = json.loads(urllib.request.urlopen(req).read())
        assert s["guard"] == "v6" and s["rules"] == 9 and s["store"] == "S"
    finally:
        app.shutdown()
