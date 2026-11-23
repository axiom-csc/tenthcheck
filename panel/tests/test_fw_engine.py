"""Firewall engine: verdicts, consent (out-of-band only), idempotency, fail-closed, receipts, append-only ledger."""
import sqlite3
import threading
import time

import pytest

from tenthlook.panel.firewall.ledger import Ledger, approval_id
from tenthlook.panel.firewall.receipts import generate_key, load_public_key, verify_receipt, Signer
from tenthlook.panel.firewall.types import FirewallError, Action
from tests.fw_helpers import IN_GIT_CHECKOUT, NOT_IN_GIT_REASON, FakeConnector, act, make_fw, pubkey_path


def test_allow_executes_once_and_replays():
    fw = make_fw()
    c = fw.connectors[0]
    o1 = fw.handle(act(amount=10))
    assert o1.status == "executed" and o1.result.result_id == "id-1" and len(c.calls) == 1
    o2 = fw.handle(act(amount=10))                      # identical call again
    assert o2.status == "replayed" and o2.result.result_id == "id-1" and len(c.calls) == 1
    assert fw.handle(act(amount=11)).status == "executed" and len(c.calls) == 2   # different args = different action


def test_block_over_cap_and_unknown_tool_and_no_execution():
    fw = make_fw()
    c = fw.connectors[0]
    o = fw.handle(act(amount=100.01))
    assert o.status == "blocked" and "exceeds" in o.verdict.reasons[0] and not c.calls
    o = fw.handle(act("fake.nuke", amount=1))
    assert o.status == "blocked" and "unknown action" in o.verdict.reasons[0]
    assert fw.handle(act("other.tool")).status == "blocked"
    assert not c.calls


def test_needs_consent_held_until_out_of_band_approval():
    fw = make_fw()
    c = fw.connectors[0]
    o = fw.handle(act(amount=75))
    assert o.status == "needs_consent" and o.approval_id and not c.calls
    assert fw.handle(act(amount=75)).status == "needs_consent"          # still held; no duplicate pending rows
    assert len([e for e in fw.ledger.events() if e["event"] == "pending"]) == 1
    ok, msg = fw.ledger.approve(o.approval_id, "alice")
    assert ok
    o2 = fw.handle(act(amount=75))
    assert o2.status == "executed" and len(c.calls) == 1
    assert "approved_by=alice" in o2.receipt["reasons"]
    ap = [e for e in fw.ledger.events() if e["event"] == "approved"][0]
    assert ap["data"]["who"] == "alice" and ap["ts"] > 0              # who/when recorded in the ledger


def test_agent_cannot_grant_its_own_consent():
    fw = make_fw()
    c = fw.connectors[0]
    sneaky = {"amount": 75, "consent": True, "approved": True, "_meta": {"consent": True, "approved_by": "me"}, "user_approved": "yes"}
    o = fw.handle(act(**sneaky))
    assert o.status == "needs_consent" and not c.calls
    # approving a DIFFERENT action does not release this one
    other = fw.handle(act(amount=76))
    fw.ledger.approve(other.approval_id, "bob")
    assert fw.handle(act(**sneaky)).status == "needs_consent" and not c.calls


def test_denied_stays_blocked_and_approval_is_single_use_per_key():
    fw = make_fw()
    o = fw.handle(act(amount=75))
    assert fw.ledger.approve(o.approval_id, "alice", deny=True)[0]
    o2 = fw.handle(act(amount=75))
    assert o2.status == "blocked" and "denied" in o2.verdict.reasons[0]
    assert fw.ledger.approve(o.approval_id, "alice")[0] is False        # already decided
    assert fw.ledger.approve("nonexistent", "alice")[0] is False
    assert fw.ledger.approve(o.approval_id, "")[0] is False             # who is required


def test_explicit_idempotency_key_and_mismatch():
    fw = make_fw()
    c = fw.connectors[0]
    assert fw.handle(act(key="K1", amount=5)).status == "executed"
    assert fw.handle(act(key="K1", amount=5, conv="other-conversation")).status == "replayed"   # explicit key spans conversations
    o = fw.handle(act(key="K1", amount=6))
    assert o.status == "blocked" and "different action" in o.verdict.reasons[0] and len(c.calls) == 1


def test_default_key_is_per_conversation_tool_args():
    fw = make_fw()
    c = fw.connectors[0]
    fw.handle(act(conv="a", amount=5)); fw.handle(act(conv="b", amount=5)); fw.handle(act(conv="a", amount=5))
    assert len(c.calls) == 2


def test_failed_call_is_not_recorded_executed_and_can_retry():
    c = FakeConnector(fail_times=1)
    fw = make_fw(connectors=[c])
    o = fw.handle(act(amount=5))
    assert o.status == "failed" and not [e for e in fw.ledger.events() if e["event"] == "executed"]
    o = fw.handle(act(amount=5))
    assert o.status == "executed" and len(c.calls) == 2
    assert fw.handle(act(amount=5)).status == "replayed" and len(c.calls) == 2


def test_connector_exception_is_a_failure_not_a_crash():
    class Boom(FakeConnector):
        def execute(self, action, mode):
            raise RuntimeError("kaboom")
    fw = make_fw(connectors=[Boom()])
    assert fw.handle(act(amount=5)).status == "failed"


def test_validation_exception_fails_closed():
    class Bad(FakeConnector):
        def validate(self, *a, **k):
            raise ValueError("parser exploded")
    fw = make_fw(connectors=[Bad()])
    o = fw.handle(act(amount=5))
    assert o.status == "blocked" and "blocked for safety" in o.verdict.reasons[0]


@pytest.mark.parametrize("args", [{"amount": float("nan")}, {"amount": "5"}, {"amount": None}, {}, {"amount": {"x": 1}}])
def test_bad_args_block(args):
    fw = make_fw()
    assert fw.handle(Action("fake.pay", args, "c")).status == "blocked"
    assert not fw.connectors[0].calls


def test_malformed_calls_block():
    fw = make_fw()
    assert fw.handle(Action("", {}, "c")).status == "blocked"
    assert fw.handle(Action("fake.pay", "not a dict", "c")).status == "blocked"   # type: ignore
    assert fw.handle(Action("fake.pay", {"amount": object()}, "c")).status == "blocked"
    assert not fw.connectors[0].calls


def test_modes():
    fw = make_fw(mode="dry_run")
    o = fw.handle(act(amount=5))
    assert o.status == "dry_run" and o.result.simulated and not fw.connectors[0].calls
    assert not [e for e in fw.ledger.events() if e["event"] == "executed"]
    fw = make_fw(mode="approval")
    assert fw.handle(act(amount=5)).status == "needs_consent"           # approval mode: writes always need a human
    assert fw.handle(act("fake.read")).status == "executed"             # reads don't
    assert fw.handle(act(amount=500)).status == "blocked"               # blocks still block


def test_rate_limit_uses_ledger_and_clock():
    t = [1000.0]
    fw = make_fw({"fake.pay": {"rate_limit": {"max": 2, "per_seconds": 60}}}, clock=lambda: t[0])
    assert fw.handle(act(amount=1)).status == "executed"
    assert fw.handle(act(amount=2)).status == "executed"
    assert fw.handle(act(amount=3)).status == "blocked"
    t[0] += 61
    assert fw.handle(act(amount=3)).status == "executed"


def test_cumulative_cap_uses_executed_amounts():
    fw = make_fw({"fake.pay": {"max_total_amount": {"amount": 100, "per_seconds": 3600}}})
    assert fw.handle(act(amount=60)).status == "executed"
    assert fw.handle(act(amount=40)).status == "executed"
    assert fw.handle(act(amount=1)).status == "blocked"


def test_concurrent_identical_calls_execute_once():
    c = FakeConnector()
    slow = c.execute
    c.execute = lambda a, m: (time.sleep(0.15), slow(a, m))[1]
    fw = make_fw(connectors=[c])
    outs = []
    ts = [threading.Thread(target=lambda: outs.append(fw.handle(act(amount=5)).status)) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(c.calls) == 1 and outs.count("executed") == 1


def test_consent_wait_releases_when_approved_in_time():
    fw = make_fw(consent_wait=5)
    def approve_soon():
        for _ in range(100):
            time.sleep(0.05)
            p = fw.ledger.pending()
            if p:
                fw.ledger.approve(p[0]["approval_id"], "carol"); return
    threading.Thread(target=approve_soon).start()
    assert fw.handle(act(amount=75)).status == "executed"


# ----------------------------------------------------------------------------- ledger
def test_ledger_is_append_only():
    led = Ledger(":memory:")
    led.append("k", "c", "t", "requested", {})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        led._db.execute("UPDATE events SET event='executed'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        led._db.execute("DELETE FROM events")


def test_ledger_unique_index_allows_one_execution_per_key():
    led = Ledger(":memory:")
    led.append("k", "c", "t", "executed", {})
    with pytest.raises(sqlite3.IntegrityError):
        led.append("k", "c", "t", "executed", {})


def test_ledger_persists_across_connections(tmp_path):
    p = tmp_path / "l.sqlite"
    fw = make_fw(); fw.ledger = Ledger(p)
    fw.handle(act(amount=5))
    led2 = Ledger(p)
    assert led2.claim(act(amount=5).key(), "c1", "fake.pay")[0] == "executed"


# ----------------------------------------------------------------------------- receipts
def test_receipt_verifies_and_detects_tampering(tmp_path):
    pol_file = tmp_path / "p.yaml"; pol_file.write_text("x: 1")
    fw = make_fw()
    r = fw.handle(act(amount=10)).receipt
    pub = load_public_key(pubkey_path())
    assert verify_receipt(r, pub) == []
    assert r["verdict"] == "ALLOWED" and "result_id=id-1" in r["reasons"] and "policy_version=t1" in r["reasons"]
    assert any(x.startswith("idempotency_key=") for x in r["reasons"])
    import copy
    for field, val in (("verdict", "BLOCKED"), ("reasons", ["result_id=x"]), ("timestamp_utc", "2020-01-01T00:00:00Z"), ("artifact_sha256", "0" * 64)):
        bad = copy.deepcopy(r); bad[field] = val
        assert "INVALID" in verify_receipt(bad, pub)[0]
    detail = fw.ledger.receipt_details()[0]
    assert verify_receipt(r, pub, detail=detail) == []
    detail["action"]["args"]["amount"] = 9999          # the stored detail no longer matches what was signed
    assert verify_receipt(r, pub, detail=detail)
    assert verify_receipt({"nonsense": 1}, pub) == ["malformed receipt"]
    assert any("policy file hash" in x for x in verify_receipt(r, pub, policy_file=pol_file))


def test_blocked_actions_get_receipts_and_other_key_fails():
    fw = make_fw()
    o = fw.handle(act(amount=1000))
    assert o.status == "blocked" and o.receipt and "outcome=blocked" in o.receipt["reasons"]
    other = generate_key(__import__("tempfile").mkdtemp() + "/o.pem")
    assert verify_receipt(o.receipt, load_public_key(str(other) + ".pub"))


@pytest.mark.skipif(not IN_GIT_CHECKOUT, reason=NOT_IN_GIT_REASON)
def test_keygen_refuses_a_path_inside_the_repo():
    from pathlib import Path
    repo_path = Path(__file__).resolve().parents[1] / "k.pem"
    with pytest.raises(FirewallError, match="git work tree"):
        generate_key(repo_path)
    assert not repo_path.exists()


def test_start_refuses_missing_or_loose_key(tmp_path):
    with pytest.raises(FirewallError, match="usepanel keygen"):
        Signer.load(tmp_path / "missing.pem")
    k = generate_key(tmp_path / "k.pem")
    k.chmod(0o644)
    with pytest.raises(FirewallError, match="readable by others"):
        Signer.load(k)
    with pytest.raises(FirewallError):
        make_fw.__globals__["Firewall"](make_fw().policy, Ledger(), None, [])
