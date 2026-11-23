import json
import sqlite3
import subprocess
import sys

import pytest

from tenthlook.core import crypto, receipt, store
from tenthlook.core.cli import main

H = "a" * 64
P = "b" * 64


def mk(key, **kw):
    base = dict(subject_type="git_diff", artifact_sha256=H, policy_sha256=P, checker_name="t", checker_version="1",
                verdict="ALLOWED", reasons=["ok"])
    base.update(kw)
    return receipt.create(signing_key=key, **base)


@pytest.fixture
def key():
    return crypto.generate_keypair()[0]


def test_sign_verify(key):
    r = mk(key)
    assert receipt.verify(r).ok
    assert receipt.verify(r, crypto.public_from_private(key)).ok


@pytest.mark.parametrize("field,val", [("verdict", "BLOCKED"), ("artifact_sha256", "c" * 64), ("policy_sha256", "c" * 64),
                                       ("reasons", ["x"]), ("timestamp_utc", "2020-01-01T00:00:00Z"),
                                       ("checker", {"name": "evil", "version": "1"}), ("prev_receipt_sha256", "d" * 64),
                                       ("receipt_id", "other"), ("trace_sha256", "e" * 64)])
def test_tampered_field_fails(key, field, val):
    r = mk(key)
    r[field] = val
    assert not receipt.verify(r).ok


def test_unknown_or_missing_field_rejected(key):
    r = mk(key)
    assert not receipt.verify({**r, "extra": 1}).ok
    del r["reasons"]
    assert not receipt.verify(r).ok


def test_bad_enum_and_hash_rejected(key):
    with pytest.raises(receipt.ReceiptError):
        mk(key, verdict="MAYBE")
    with pytest.raises(receipt.ReceiptError):
        mk(key, subject_type="email")
    with pytest.raises(receipt.ReceiptError):
        mk(key, artifact_sha256="xyz")


def test_wrong_key_fails(key):
    other_pub = crypto.generate_keypair()[1]
    r = mk(key)
    assert not receipt.verify(r, other_pub).ok              # pinned key mismatch
    r2 = dict(r, pubkey=other_pub)                           # swap in someone else's pubkey, keep signature
    assert not receipt.verify(r2).ok


def test_refuses_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv(crypto.KEY_ENV, raising=False)
    monkeypatch.delenv(crypto.KEY_FILE_ENV, raising=False)
    with pytest.raises(crypto.KeyMissing):
        crypto.load_signing_key()
    with pytest.raises(crypto.KeyMissing):
        crypto.load_signing_key(tmp_path / "nope.key")
    with pytest.raises(crypto.KeyMissing):
        mk("")


def test_key_file_rules(tmp_path, monkeypatch):
    monkeypatch.delenv(crypto.KEY_ENV, raising=False)
    kf = tmp_path / "k" / "signing.key"
    pub = crypto.write_new_key_file(kf)
    assert oct(kf.stat().st_mode & 0o777) == "0o600"
    assert crypto.public_from_private(crypto.load_signing_key(kf)) == pub
    with pytest.raises(FileExistsError):
        crypto.write_new_key_file(kf)                       # never overwrite
    kf.chmod(0o644)
    with pytest.raises(crypto.KeyRefused):
        crypto.load_signing_key(kf)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(crypto.KeyRefused):
        crypto.write_new_key_file(repo / "sub" / "signing.key")


def test_canonicalization_stable_across_dict_ordering():
    a = {"b": 1, "a": {"z": [1, 2, {"y": None, "x": True}], "k": "é"}}
    b = {"a": {"k": "é", "z": [1, 2, {"x": True, "y": None}]}, "b": 1}
    assert crypto.canonical_json(a) == crypto.canonical_json(b) == b'{"a":{"k":"\xc3\xa9","z":[1,2,{"x":true,"y":null}]},"b":1}'
    assert crypto.hash_json(a) == crypto.hash_json(b)
    # JCS orders keys by UTF-16 code unit: U+FF5E (BMP) sorts after U+1F600 (surrogate pair D83D..)
    assert crypto.canonical_json({"～": 1, "\U0001f600": 2}).decode() == '{"\U0001f600":2,"～":1}'


@pytest.mark.parametrize("bad", [1.5, float("nan"), 2**60, {1: 2}, {"a": {1, 2}}, "\ud800"])
def test_canonical_rejects_what_it_cannot_promise(bad):
    with pytest.raises((TypeError, ValueError)):
        crypto.canonical_json(bad)


def test_receipt_signature_independent_of_key_order(key):
    r = mk(key)
    reordered = dict(reversed(list(r.items())))
    assert receipt.verify(reordered).ok


# ------------------------------------------------------------ chain

def build(tmp_path, key, n=4):
    db = tmp_path / "r.db"
    with store.Store(db) as s:
        for i in range(n):
            s.create_and_append(signing_key=key, subject_type="mcp_tool_call", artifact_sha256=H, policy_sha256=P,
                                checker_name="t", checker_version="1", verdict="BLOCKED", reasons=[f"r{i}"])
        return db, s.head()


def raw(db, sql, *args):
    c = sqlite3.connect(db)
    c.executescript("DROP TRIGGER IF EXISTS receipts_no_update; DROP TRIGGER IF EXISTS receipts_no_delete;")  # attacker with file access
    c.execute(sql, args)
    c.commit()
    c.close()


def test_intact_chain(tmp_path, key):
    db, head = build(tmp_path, key)
    r = store.verify_chain(db, crypto.public_from_private(key), expect_head=head, expect_count=4)
    assert r.ok and r.count == 4 and r.head == head


def test_sql_cannot_update_or_delete(tmp_path, key):
    db, _ = build(tmp_path, key)
    c = sqlite3.connect(db)
    for sql in ("DELETE FROM receipts WHERE seq=2", "UPDATE receipts SET body='{}' WHERE seq=2"):
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(sql)


def test_deleted_middle_receipt_breaks_chain(tmp_path, key):
    db, _ = build(tmp_path, key)
    raw(db, "DELETE FROM receipts WHERE seq=2")
    r = store.verify_chain(db)
    assert not r.ok and any("#2" in e or "#3" in e for e in r.errors)


def test_deleted_first_receipt_breaks_chain(tmp_path, key):
    db, _ = build(tmp_path, key)
    raw(db, "DELETE FROM receipts WHERE seq=1")
    assert not store.verify_chain(db).ok


def test_deleted_tail_needs_external_head(tmp_path, key):
    db, head = build(tmp_path, key)
    raw(db, "DELETE FROM receipts WHERE seq=4")
    assert store.verify_chain(db).ok                          # documented limitation: nothing follows the tail
    assert not store.verify_chain(db, expect_head=head).ok    # ...caught with an externally kept head
    assert not store.verify_chain(db, expect_count=4).ok


def test_edited_receipt_breaks_chain(tmp_path, key):
    db, _ = build(tmp_path, key)
    c = sqlite3.connect(db)
    body = c.execute("SELECT body FROM receipts WHERE seq=2").fetchone()[0]
    c.close()
    forged = json.loads(body)
    forged["verdict"] = "ALLOWED"
    raw(db, "UPDATE receipts SET body=? WHERE seq=2", json.dumps(forged))
    assert not store.verify_chain(db).ok


def test_edited_and_resigned_with_other_key_caught_by_pin_and_chain(tmp_path, key):
    db, _ = build(tmp_path, key)
    evil = crypto.generate_keypair()[0]
    c = sqlite3.connect(db)
    old = json.loads(c.execute("SELECT body FROM receipts WHERE seq=2").fetchone()[0])
    c.close()
    forged = receipt.create(signing_key=evil, subject_type=old["subject_type"], artifact_sha256=H, policy_sha256=P,
                            checker_name="t", checker_version="1", verdict="ALLOWED", reasons=["forged"],
                            prev_receipt_sha256=old["prev_receipt_sha256"], receipt_id=old["receipt_id"], timestamp_utc=old["timestamp_utc"])
    raw(db, "UPDATE receipts SET body=?, receipt_sha256=? WHERE seq=2", crypto.canonical_json(forged).decode(), receipt.receipt_hash(forged))
    assert not store.verify_chain(db).ok                                              # receipt #3 no longer links to it
    assert not store.verify_chain(db, crypto.public_from_private(key)).ok            # and the pinned key rejects it


def test_reordered_receipts_break_chain(tmp_path, key):
    db, _ = build(tmp_path, key)
    c = sqlite3.connect(db)
    c.executescript("DROP TRIGGER receipts_no_update;")
    b2, b3 = [c.execute("SELECT receipt_sha256, body FROM receipts WHERE seq=?", (i,)).fetchone() for i in (2, 3)]
    c.execute("UPDATE receipts SET receipt_sha256='tmp', body='tmp' WHERE seq=2")
    c.execute("UPDATE receipts SET receipt_sha256=?, body=? WHERE seq=3", b2)
    c.execute("UPDATE receipts SET receipt_sha256=?, body=? WHERE seq=2", b3)
    c.commit()
    c.close()
    assert not store.verify_chain(db).ok


def test_append_rejects_wrong_prev_and_unsigned(tmp_path, key):
    with store.Store(tmp_path / "x.db") as s:
        s.append(mk(key))
        with pytest.raises(store.ChainError):
            s.append(mk(key))                                  # prev is GENESIS, head has moved
        bad = mk(key, prev_receipt_sha256=s.head())
        bad["verdict"] = "BLOCKED"
        with pytest.raises(store.ChainError):
            s.append(bad)
        assert s.count() == 1


# ------------------------------------------------------------ CLI (offline)

def test_cli_roundtrip(tmp_path, key, capsys, monkeypatch):
    monkeypatch.delenv(crypto.KEY_ENV, raising=False)
    kf = tmp_path / "keys" / "k"
    assert main(["keygen", "--out", str(kf)]) == 0
    pub = capsys.readouterr().out.strip().split()[-1]
    assert main(["keygen", "--out", str(kf)]) == 2           # refuses to overwrite
    seed = crypto.load_signing_key(kf)
    db = tmp_path / "c.db"
    with store.Store(db) as s:
        r = s.create_and_append(signing_key=seed, subject_type="sql_query", artifact_sha256=H, policy_sha256=P,
                                checker_name="t", checker_version="1", verdict="NOT_CHECKABLE")
    f = tmp_path / "r.json"
    f.write_text(json.dumps(r))
    assert main(["verify-receipt", str(f), "--pubkey", pub]) == 0
    assert main(["verify-chain", str(db), "--pubkey", pub, "--expect-count", "1"]) == 0
    assert main(["verify-receipt", str(f), "--pubkey", crypto.generate_keypair()[1]]) == 1
    r["verdict"] = "ALLOWED"
    f.write_text(json.dumps(r))
    assert main(["verify-receipt", str(f)]) == 1


def test_verification_makes_no_network_calls(tmp_path, key):
    db, _ = build(tmp_path, key, 2)
    code = ("import socket,sys\n"
            "def boom(*a,**k): raise SystemExit('network used')\n"
            "socket.socket.connect=boom; socket.getaddrinfo=boom\n"
            "from tenthlook.core.cli import main\n"
            f"sys.exit(main(['verify-chain', {str(db)!r}]))")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


def test_conformance_fixture_still_verifies():
    from pathlib import Path
    fx = json.loads((Path(__file__).parent / "fixtures" / "conformance.json").read_text())
    for v in fx["canonical_vectors"]:
        assert crypto.canonical_json(v["in"]).decode() == v["canonical"]
        assert crypto.hash_json(v["in"]) == v["sha256"]
    assert receipt.verify(fx["receipt"], fx["pubkey"]).ok
    assert receipt.receipt_hash(fx["receipt"]) == fx["receipt_sha256"]


def test_json_export_chain_and_pure_list_verifier(tmp_path, key):
    db, head = build(tmp_path, key, 3)
    with store.Store(db) as s:
        rs = s.all()
    f = tmp_path / "export.json"
    f.write_text(json.dumps(rs))
    pub = crypto.public_from_private(key)
    assert store.verify_chain(f, pub, expect_head=head, expect_count=3).ok
    assert store.verify_receipts(rs, pub).ok
    f.write_text(json.dumps([rs[0], rs[2]]))
    assert not store.verify_chain(f, pub).ok
    f.write_text(json.dumps([rs[1], rs[0], rs[2]]))
    assert not store.verify_chain(f, pub).ok
    f.write_text("not json")
    assert not store.verify_chain(f).ok


def test_key_force_and_pub_file(tmp_path):
    kf = tmp_path / "k"
    pub = crypto.write_new_key_file(kf, pub_file=True)
    assert (tmp_path / "k.pub").read_text().strip() == pub
    pub2 = crypto.write_new_key_file(kf, force=True)
    assert pub2 != pub and oct(kf.stat().st_mode & 0o777) == "0o600"
