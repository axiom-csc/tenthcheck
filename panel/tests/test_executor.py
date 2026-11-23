"""Executor modes, idempotency, policy re-check, hand-off, proxy wording and the approval page (no network)."""
import hashlib
import hmac
import json
import logging
import sys
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenthlook.panel.execute.approval_page import serve  # noqa: E402
from tenthlook.panel.execute.executor import Executor, FakeBackend, Limits, action_key  # noqa: E402
from tenthlook.panel.execute.handoff import LogNotifier, WebhookNotifier  # noqa: E402
from tenthlook.panel.execute.store import ActionStore  # noqa: E402
from tenthlook.panel.guard import proxy as P  # noqa: E402
from tenthlook.panel.guard.policy import load_policy  # noqa: E402
from tenthlook.panel.guard.v62 import PENDING  # noqa: E402

POLICY = load_policy(ROOT / "guard_data/policy_fictional_store.toml")
HOOK_SECRET = "whsec-never-log-me"


class Orders:
    """Fake Shopify-shaped order lookup."""
    order_id_pattern = __import__("tenthlook.panel.shopify.orders", fromlist=["x"]).SHOPIFY_ORDER_ID

    def __init__(self, **over):
        self.today = date(2026, 11, 17)
        self.o = {"order_id": "#1001", "purchase_date": "2026-11-06", "amount": 54.0, "amount_paid": 54.0, "on_sale": False,
                  "has_receipt": True, "item": "canvas tote", "test": True, **over}
        self.fresh_calls = 0

    def get_order(self, order_id, fresh=False):
        self.fresh_calls += fresh
        return dict(self.o) if order_id.lstrip("#") == "1001" else None


class Hook:
    def __init__(self, status=200):
        self.sent, self.status = [], status

    def __call__(self, method, url, headers, body):
        self.sent.append({"headers": headers, "body": json.loads(body)})
        return self.status, b"ok"


def make(mode="approval", refund_max=50.0, backend=None, orders=None, notifier=None, db=":memory:", allow_live=False):
    hook = Hook()
    ex = Executor(ActionStore(db), backend or FakeBackend(), orders or Orders(), POLICY,
                  Limits(auto_refund_max=refund_max, auto_discount_max_percent=10), notifier or WebhookNotifier(
                      "https://hooks.example/panel", HOOK_SECRET, transport=hook), mode=mode, allow_live_orders=allow_live)
    return ex, hook


REFUND = {"type": "refund", "amount": 40.0}
REQUESTS = {"refund": "Please refund order #1001.", "discount": "Could I get 10% off my next order? Order #1001.",
            "exchange": "Please set up an exchange for order #1001, I have the receipt.",
            "handoff_to_human": "I want to talk to a person about order #1001."}


def ask(action):
    """A transcript in which the customer clearly asked for this action (v6.3 consent)."""
    return [{"role": "user", "content": REQUESTS[action["type"]]}]
ORDER = Orders().o


# ---------------------------------------------------------------------------------------------- modes
def test_dry_run_records_and_calls_nothing():
    ex, hook = make("dry_run")
    r = ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND))
    assert r.status == "dry_run" and "would execute" in r.decision and ex.backend.calls == [] and hook.sent == []
    r2 = ex.submit("c1", ORDER, {"type": "refund", "amount": 54.0}, transcript=ask({"type": "refund", "amount": 54.0}))
    assert "would queue" in r2.decision


def test_approval_mode_only_approved_actions_execute():
    ex, _ = make("approval")
    r = ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND))
    assert r.status == "queued" and ex.backend.calls == []
    assert [p.key for p in ex.pending()] == [r.key]
    assert ex.approve(r.key).status == "executed" and len(ex.backend.calls) == 1
    assert ex.approve(r.key).status == "executed" and len(ex.backend.calls) == 1  # approving again does nothing
    r2 = ex.submit("c2", ORDER, REFUND, transcript=ask(REFUND))
    assert ex.reject(r2.key).status == "rejected" and ex.approve(r2.key).status == "rejected"
    assert len(ex.backend.calls) == 1


def test_auto_mode_executes_within_limits_and_queues_above():
    ex, _ = make("auto", refund_max=50)
    assert ex.submit("c1", ORDER, {"type": "refund", "amount": 40}, transcript=ask({"type": "refund", "amount": 40})).status == "executed"
    big = ex.submit("c2", ORDER, {"type": "refund", "amount": 54}, transcript=ask({"type": "refund", "amount": 54}))
    assert big.status == "queued" and "> auto limit $50.00" in big.decision
    assert ex.submit("c3", ORDER, {"type": "discount", "percent": 10}, transcript=ask({"type": "discount", "percent": 10})).status == "executed"
    assert ex.submit("c4", ORDER, {"type": "exchange"}, transcript=ask({"type": "exchange"})).status == "queued"  # exchanges are manual by default
    assert len(ex.backend.calls) == 2


def test_dry_run_row_is_replaced_by_a_real_submit_later():
    store = ActionStore(":memory:")
    dry = Executor(store, FakeBackend(), Orders(), POLICY, Limits(auto_refund_max=50), LogNotifier(), mode="dry_run")
    dry.submit("c1", ORDER, REFUND, transcript=ask(REFUND))
    live = Executor(store, FakeBackend(), Orders(), POLICY, Limits(auto_refund_max=50), LogNotifier(), mode="auto")
    assert live.submit("c1", ORDER, REFUND, transcript=ask(REFUND)).status == "executed" and len(live.backend.calls) == 1


def test_limits_from_policy_file(tmp_path):
    f = tmp_path / "p.toml"
    f.write_text((ROOT / "guard_data/policy_fictional_store.toml").read_text()
                 + '\n[execution]\nmode = "auto"\nauto_refund_max = 50\nauto_discount_max_percent = 10\n')
    lim = Limits.from_policy_file(f)
    assert (lim.mode, lim.auto_refund_max, lim.auto_discount_max_percent, lim.auto_exchange) == ("auto", 50, 10, False)
    load_policy(f)  # the guard's loader still accepts the file


# ---------------------------------------------------------------------------------------------- idempotency
def test_key_is_conversation_order_action_amount():
    k = action_key("c1", "#1001", {"type": "refund", "amount": 40})
    assert k == action_key("c1", "#1001", {"type": "refund", "amount": 40.0})
    assert len({k, action_key("c2", "#1001", {"type": "refund", "amount": 40}),
                action_key("c1", "#1002", {"type": "refund", "amount": 40}),
                action_key("c1", "#1001", {"type": "refund", "amount": 41}),
                action_key("c1", "#1001", {"type": "discount", "percent": 40})}) == 5


def test_same_key_never_executes_twice_even_in_auto_mode():
    ex, _ = make("auto")
    for _ in range(5):
        assert ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND)).status == "executed"
    assert len(ex.backend.calls) == 1


def test_concurrent_submits_execute_once(tmp_path):
    ex, _ = make("auto", db=tmp_path / "a.sqlite")
    out = []
    threads = [threading.Thread(target=lambda: out.append(ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND)).status)) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ex.backend.calls) == 1 and set(out) <= {"executed", "executing", "auto"}


def test_ledger_survives_a_restart(tmp_path):
    db = tmp_path / "ledger.sqlite"
    ex1, _ = make("auto", db=db)
    ex1.submit("c1", ORDER, REFUND, transcript=ask(REFUND))
    ex2, _ = make("auto", db=db)  # new process, same ledger
    assert ex2.submit("c1", ORDER, REFUND, transcript=ask(REFUND)).status == "executed" and ex2.backend.calls == []


def test_failed_action_is_not_retried_and_the_team_is_told():
    ex, hook = make("auto", backend=FakeBackend(fail=True))
    r = ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND))
    assert r.status == "failed" and "backend down" in r.error
    assert ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND)).status == "failed" and len(ex.backend.calls) == 1
    assert hook.sent and "refund failed" in hook.sent[-1]["body"]["reason"]


# ---------------------------------------------------------------------------------------------- re-check / safety
def test_refund_is_rechecked_against_the_fresh_order():
    orders = Orders(amount=30.0)  # another refund happened since the guard checked: only $30 left
    ex, _ = make("auto", orders=orders)
    r = ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND))
    assert r.status == "blocked" and "policy re-check failed" in r.error and ex.backend.calls == []
    assert orders.fresh_calls == 1


def test_live_orders_are_refused_unless_allowed():
    ex, _ = make("auto", orders=Orders(test=False))
    assert ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND)).status == "blocked" and ex.backend.calls == []
    ex2, _ = make("auto", orders=Orders(test=False), allow_live=True)
    assert ex2.submit("c1", ORDER, REFUND, transcript=ask(REFUND)).status == "executed"


# ---------------------------------------------------------------------------------------------- hand-off
def test_handoff_webhook_is_signed_and_carries_the_summary():
    ex, hook = make("approval")
    transcript = [{"role": "user", "content": "Can I talk to a person about order #1001?"}]
    r = ex.submit("c9", ORDER, {"type": "handoff_to_human"}, transcript=transcript)
    assert r.status == "executed"  # hand-offs go out at once (auto_handoff) even in approval mode
    sent = hook.sent[-1]
    body = json.dumps(sent["body"]).encode()
    assert sent["headers"]["X-Panel-Signature"] == "sha256=" + hmac.new(HOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    s = sent["body"]
    assert s["conversation_id"] == "c9" and s["order"]["order_id"] == "#1001" and "talk to a person" in s["customer_last_message"]
    assert HOOK_SECRET not in json.dumps(s)


def test_handoff_without_a_channel_fails_honestly():
    ex, _ = make("approval", notifier=LogNotifier())
    assert ex.submit("c1", ORDER, {"type": "handoff_to_human"}, transcript=ask({"type": "handoff_to_human"})).status == "failed"


# ---------------------------------------------------------------------------------------------- proxy wording
def j(actions, message):
    return json.dumps({"actions": actions, "message": message})


class Scripted:
    def __init__(self, script):
        self.script, self.n = script, 0

    def complete(self, messages):
        self.n += 1
        return self.script[min(self.n - 1, len(self.script) - 1)], {"usage": None}


MSGS = [{"role": "user", "content": "Please refund order #1001, the canvas tote."}]


def test_proxy_says_sent_for_approval_when_queued_never_issued():
    ex, _ = make("approval")
    t = P.guarded(MSGS, Scripted([j([{"type": "refund", "amount": 40}], "Sure.")]).complete, POLICY, ex.orders,
                  executor=ex, conversation_id="conv-1")
    assert "I've issued" not in t["sent"] and PENDING["refund"].format(amount=40, order="#1001") in t["sent"]
    assert t["actions"][0]["status"] == "queued"


def test_proxy_says_issued_only_when_executed():
    ex, _ = make("auto")
    t = P.guarded(MSGS, Scripted([j([{"type": "refund", "amount": 40}], "Sure.")]).complete, POLICY, ex.orders,
                  executor=ex, conversation_id="conv-2")
    assert "I've issued a refund of $40.00 for order #1001." in t["sent"] and t["actions"][0]["status"] == "executed"
    ask_discount = [{"role": "user", "content": "Could I get 10% off my next order? Order #1001."}]
    t2 = P.guarded(ask_discount, Scripted([j([{"type": "discount", "percent": 10}], "Sure.")]).complete, POLICY, ex.orders,
                   executor=ex, conversation_id="conv-3")
    assert "I've added a 10% discount to your next order. Your code is PANEL-" in t2["sent"]


def test_proxy_never_claims_a_failed_action():
    ex, _ = make("auto", backend=FakeBackend(fail=True))
    t = P.guarded(MSGS, Scripted([j([{"type": "refund", "amount": 40}], "Sure.")]).complete, POLICY, ex.orders,
                  executor=ex, conversation_id="conv-4")
    assert "I've issued" not in t["sent"] and "wasn't able to complete" in t["sent"]


def test_proxy_repeat_in_same_conversation_is_one_action():
    ex, _ = make("auto")
    for _ in range(3):
        P.guarded(MSGS, Scripted([j([{"type": "refund", "amount": 40}], "Sure.")]).complete, POLICY, ex.orders,
                  executor=ex, conversation_id="conv-5")
    assert len(ex.backend.calls) == 1


# ---------------------------------------------------------------------------------------------- approval page
def _req(url, data=None, host=None):
    r = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if host:
        r.add_header("Host", host)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_approval_page_lists_and_approves_with_csrf_only():
    ex, _ = make("approval")
    rec = ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND))
    srv = serve(ex, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        code, page = _req(base + "/")
        assert code == 200 and "refund $40.00" in page and "Approve" in page
        csrf = page.split('name="csrf" value="')[1].split('"')[0]
        assert _req(base + "/", host="evil.example:80")[0] == 403  # DNS-rebinding guard
        assert _req(base + "/approve", f"key={rec.key}&csrf=wrong".encode())[0] == 403
        assert ex.store.get(rec.key).status == "queued" and ex.backend.calls == []
        _req(base + "/approve", f"key={rec.key}&csrf={csrf}".encode())
        assert ex.store.get(rec.key).status == "executed" and len(ex.backend.calls) == 1
    finally:
        srv.shutdown()


def test_no_secret_reaches_the_logs(caplog):
    caplog.set_level(logging.DEBUG)
    ex, _ = make("auto")
    ex.submit("c1", ORDER, REFUND, transcript=ask(REFUND))
    ex.submit("c1", ORDER, {"type": "handoff_to_human"}, transcript=ask({"type": "handoff_to_human"}))
    assert HOOK_SECRET not in caplog.text
