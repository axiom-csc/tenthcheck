"""Receipt contents and independent verification (Phase 5)."""
import hashlib
import json

from tenthlook.panel.firewall.receipts import load_public_key, verify_receipt
from tenthlook.panel.firewall.types import canonical
from tests.fw_helpers import POLICY_SHA, act, make_fw, pubkey_path


def test_receipt_has_every_required_field_and_hashes_recompute_independently():
    fw = make_fw()
    o = fw.handle(act(amount=10, key="idem-1"))
    p = o.receipt
    for f in ("artifact_sha256", "policy_sha256", "verdict", "reasons", "timestamp_utc", "trace_sha256", "checker", "pubkey", "signature",
              "prev_receipt_sha256", "receipt_id", "subject_type"):
        assert f in p, f
    d = fw.ledger.receipt_details()[0]
    for f in ("tool", "action", "idempotency_key", "outcome", "result_id", "policy_version", "timestamp"):
        assert f in d, f
    # recompute the action hash with nothing but hashlib + json, not Panel's helpers
    manual = hashlib.sha256(json.dumps({"tool": "fake.pay", "args": {"amount": 10}}, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode()).hexdigest()
    assert p["artifact_sha256"] == manual and p["policy_sha256"] == POLICY_SHA and d["policy_version"] == "t1"
    assert d["idempotency_key"] == "k:idem-1" and d["result_id"] == "id-1" and p["verdict"] == "ALLOWED"
    assert verify_receipt(o.receipt, load_public_key(pubkey_path()), detail=d) == []


def test_key_order_and_whitespace_do_not_change_the_signed_bytes():
    assert canonical({"b": 1, "a": [1, 2]}) == canonical(json.loads('{ "a" : [1,2], "b":1 }'))


def test_one_receipt_per_outcome_and_all_verify_from_the_ledger():
    fw = make_fw()
    fw.handle(act(amount=10)); fw.handle(act(amount=500)); fw.handle(act("nope"))
    rs = fw.ledger.receipts()
    assert [r["verdict"] for r in rs] == ["ALLOWED", "BLOCKED", "BLOCKED"]
    pub = load_public_key(pubkey_path())
    assert all(verify_receipt(r, pub) == [] for r in rs)


# ----------------------------------------------------------------------------- chaining (tenthlook-core)
import sqlite3

import pytest
from tenthlook.core import store as core_store

from tenthlook.panel.firewall.ledger import Ledger
from tenthlook.panel.firewall.receipts import GENESIS, receipt_hash, verify_chain


def _file_ledger_fw(tmp_path):
    from tests.fw_helpers import FakeConnector, make_fw
    fw = make_fw()
    fw.ledger = Ledger(tmp_path / "l.db")
    return fw


def _run(fw, n=4):
    for i in range(n):
        fw.handle(act(amount=10 + i, key=f"k{i}"))
    return fw


def _attack(path, sql, *args):
    c = sqlite3.connect(path)
    c.executescript("DROP TRIGGER IF EXISTS events_no_update; DROP TRIGGER IF EXISTS events_no_delete;")  # someone with file access
    c.execute(sql, args)
    c.commit(); c.close()


def _receipt_seqs(path):
    c = sqlite3.connect(path)
    return [r[0] for r in c.execute("SELECT seq FROM events WHERE event='receipt' ORDER BY seq")]


def test_receipts_are_chained_per_ledger(tmp_path):
    fw = _run(_file_ledger_fw(tmp_path))
    rs = fw.ledger.receipts()
    pub = load_public_key(pubkey_path())
    assert rs[0]["prev_receipt_sha256"] == GENESIS
    assert [r["prev_receipt_sha256"] for r in rs[1:]] == [receipt_hash(r) for r in rs[:-1]]
    res = verify_chain(rs, pub, expect_head=receipt_hash(rs[-1]), expect_count=4)
    assert res.ok, res.errors
    # a second ledger starts its own chain
    other = _run(_file_ledger_fw(tmp_path / "..") if False else make_fw_with_ledger(tmp_path / "m.db"), 1)
    assert other.ledger.receipts()[0]["prev_receipt_sha256"] == GENESIS


def make_fw_with_ledger(path):
    from tests.fw_helpers import make_fw
    fw = make_fw()
    fw.ledger = Ledger(path)
    return fw


def test_deleted_receipt_breaks_the_chain(tmp_path):
    fw = _run(_file_ledger_fw(tmp_path))
    seqs = _receipt_seqs(tmp_path / "l.db")
    _attack(tmp_path / "l.db", "DELETE FROM events WHERE seq=?", seqs[1])
    res = verify_chain(Ledger(tmp_path / "l.db").receipts(), load_public_key(pubkey_path()))
    assert not res.ok and any("edited, deleted or reordered" in e for e in res.errors)


def test_edited_receipt_breaks_the_chain(tmp_path):
    fw = _run(_file_ledger_fw(tmp_path))
    seqs = _receipt_seqs(tmp_path / "l.db")
    c = sqlite3.connect(tmp_path / "l.db")
    body = c.execute("SELECT data FROM events WHERE seq=?", (seqs[1],)).fetchone()[0]
    c.close()
    forged = json.loads(body)
    forged["receipt"]["verdict"] = "BLOCKED" if forged["receipt"]["verdict"] == "ALLOWED" else "ALLOWED"
    _attack(tmp_path / "l.db", "UPDATE events SET data=? WHERE seq=?", canonical(forged), seqs[1])
    res = verify_chain(Ledger(tmp_path / "l.db").receipts(), load_public_key(pubkey_path()))
    assert not res.ok and any("signature" in e for e in res.errors)


def test_deleting_the_newest_receipt_is_caught_only_with_an_external_head(tmp_path):
    fw = _run(_file_ledger_fw(tmp_path))
    head = receipt_hash(fw.ledger.receipts()[-1])
    _attack(tmp_path / "l.db", "DELETE FROM events WHERE seq=?", _receipt_seqs(tmp_path / "l.db")[-1])
    rs = Ledger(tmp_path / "l.db").receipts()
    assert verify_chain(rs).ok                                  # documented limit: nothing follows the tail
    assert not verify_chain(rs, expect_head=head).ok and not verify_chain(rs, expect_count=4).ok


def test_ledger_still_refuses_sql_edits_and_deletes(tmp_path):
    fw = _run(_file_ledger_fw(tmp_path), 1)
    c = sqlite3.connect(tmp_path / "l.db")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM events WHERE event='receipt'")


def test_cli_verify_chain_and_tenthlook_cli_accept_panels_output(tmp_path, capsys):
    from tenthlook.panel.firewall.cli import main
    from tenthlook.core.cli import main as tl
    fw = _run(_file_ledger_fw(tmp_path))
    pub = pubkey_path()
    led = str(tmp_path / "l.db")
    out = tmp_path / "receipts.json"
    assert main(["export-receipts", "--ledger", led, "--out", str(out)]) == 0
    assert main(["verify-chain", "--ledger", led, "--pubkey", pub]) == 0
    pk = open(pub).read().strip()
    assert tl(["verify-chain", str(out), "--pubkey", pk, "--expect-count", "4"]) == 0
    one = tmp_path / "one.json"; one.write_text(json.dumps(fw.ledger.receipts()[0]))
    assert tl(["verify-receipt", str(one), "--pubkey", pk]) == 0
    assert "OK" in capsys.readouterr().out
    # a tampered export is refused by both
    data = json.loads(out.read_text()); del data[1]; out.write_text(json.dumps(data))
    assert tl(["verify-chain", str(out), "--pubkey", pk]) == 1
    _attack(led, "DELETE FROM events WHERE seq=?", _receipt_seqs(led)[1])
    assert main(["verify-chain", "--ledger", led, "--pubkey", pub]) == 1


def test_concurrent_writers_do_not_fork_the_chain(tmp_path):
    import threading
    fw = _file_ledger_fw(tmp_path)
    fws = [make_fw_with_ledger(tmp_path / "l.db") for _ in range(4)]   # four connections to one ledger file
    def go(f, i):
        for j in range(5):
            f.handle(act(amount=1 + j, key=f"t{i}-{j}"))
    ts = [threading.Thread(target=go, args=(f, i)) for i, f in enumerate(fws)]
    [t.start() for t in ts]; [t.join() for t in ts]
    rs = Ledger(tmp_path / "l.db").receipts()
    assert len(rs) == 20 and verify_chain(rs, load_public_key(pubkey_path()), expect_count=20).ok
