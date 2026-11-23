"""Append-only SQLite receipt store with a hash chain.

Each receipt carries the SHA-256 of the previous receipt (`prev_receipt_sha256`), and is signed, so editing,
deleting or reordering any receipt that is followed by another one breaks `verify_chain`.

Limits (see README): deleting the LAST receipts is not detectable from the database alone (nothing follows them). Keep the
head hash and count somewhere else (`head()`; `verify_chain(expect_head=..., expect_count=...)`). SQLite triggers refuse
UPDATE and DELETE through SQL, but anyone with file access can bypass them; the signed chain is what detects that.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto, receipt as rcpt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_sha256 TEXT NOT NULL UNIQUE,
  body TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
"""


class ChainError(RuntimeError):
    pass


@dataclass
class ChainResult:
    ok: bool
    count: int = 0
    head: str = rcpt.GENESIS
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None)  # explicit transactions
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def head(self) -> str:
        row = self.db.execute("SELECT receipt_sha256 FROM receipts ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else rcpt.GENESIS

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

    def append(self, r: dict) -> str:
        """Add an already-signed receipt. It must verify and its prev hash must equal the current head."""
        v = rcpt.verify(r)
        if not v.ok:
            raise ChainError("; ".join(v.errors))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            head = self.head()
            if r["prev_receipt_sha256"] != head:
                raise ChainError(f"prev_receipt_sha256 {r['prev_receipt_sha256'][:12]}.. is not the current head {head[:12]}..")
            h = rcpt.receipt_hash(r)
            self.db.execute("INSERT INTO receipts (receipt_sha256, body) VALUES (?, ?)", (h, crypto.canonical_json(r).decode()))
            self.db.execute("COMMIT")
            return h
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create_and_append(self, *, signing_key: str, **kw) -> dict:
        """Build a receipt chained to the current head, sign it and store it, atomically with respect to other writers."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            r = rcpt.create(prev_receipt_sha256=self.head(), signing_key=signing_key, **kw)
            self.db.execute("INSERT INTO receipts (receipt_sha256, body) VALUES (?, ?)",
                            (rcpt.receipt_hash(r), crypto.canonical_json(r).decode()))
            self.db.execute("COMMIT")
            return r
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def all(self) -> list[dict]:
        return [json.loads(b) for (b,) in self.db.execute("SELECT body FROM receipts ORDER BY seq")]


def verify_receipts(receipts: list, pinned_pubkey: str | None = None, expect_head: str | None = None,
                    expect_count: int | None = None, seqs: list[int] | None = None, stored_hashes: list[str] | None = None) -> ChainResult:
    """Offline chain check over receipts in order (from a core store, a JSON export, or any append-only ledger).

    Checks each receipt's schema and signature, that it names the previous receipt's hash, and (when `seqs` /
    `stored_hashes` are given) that sequence numbers are contiguous and stored hashes match."""
    res = ChainResult(True)
    prev, last_seq = rcpt.GENESIS, 0
    for i, r in enumerate(receipts):
        seq = seqs[i] if seqs else i + 1
        tag = f"receipt #{seq}"
        if seq != last_seq + 1:
            res.errors.append(f"{tag}: sequence gap (expected #{last_seq + 1}): a receipt was deleted or inserted")
        last_seq = seq
        if not isinstance(r, dict):
            res.errors.append(f"{tag}: not an object")
            prev = ""
            continue
        v = rcpt.verify(r, pinned_pubkey)
        res.errors += [f"{tag}: {m}" for m in v.errors]
        h = rcpt.receipt_hash(r) if not rcpt.validate_shape(r) else ""
        if not v.errors:
            if stored_hashes and stored_hashes[i] != h:
                res.errors.append(f"{tag}: stored hash does not match the receipt")
            if r["prev_receipt_sha256"] != prev:
                res.errors.append(f"{tag}: previous-receipt hash does not match the receipt before it: edited, deleted or reordered")
        prev = h or (stored_hashes[i] if stored_hashes else "")
    res.count, res.head = len(receipts), prev if receipts else rcpt.GENESIS
    if expect_head is not None and expect_head != res.head:
        res.errors.append(f"head is {res.head[:12]}.., expected {expect_head[:12]}..: receipts were removed or added since it was recorded")
    if expect_count is not None and expect_count != len(receipts):
        res.errors.append(f"{len(receipts)} receipts, expected {expect_count}")
    if expect_head is None and expect_count is None:
        res.notes.append("no expected head/count given: removal of the newest receipts cannot be detected")
    if pinned_pubkey is None:
        res.notes.append("no pinned public key: this shows internal consistency, not who signed")
    res.ok = not res.errors
    return res


def verify_chain(db_path: str | Path, pinned_pubkey: str | None = None, expect_head: str | None = None,
                 expect_count: int | None = None) -> ChainResult:
    """Offline. `db_path` is a core receipt database, or a JSON file holding a list of receipts in order (for example
    `panel export-receipts`)."""
    p = Path(db_path)
    if not p.is_file():
        return ChainResult(False, errors=[f"{p} does not exist"])
    head = p.read_bytes()[:16]
    if not head.startswith(b"SQLite format 3"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return ChainResult(False, errors=[f"{p} is neither a receipt database nor a JSON list of receipts"])
        if not isinstance(data, list):
            return ChainResult(False, errors=["JSON chain must be a list of receipts, oldest first"])
        return verify_receipts(data, pinned_pubkey, expect_head, expect_count)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT seq, receipt_sha256, body FROM receipts ORDER BY seq").fetchall()
    except sqlite3.Error as e:
        return ChainResult(False, errors=[f"not a receipt database: {e}"])
    finally:
        con.close()
    bodies, bad = [], []
    for seq, _, body in rows:
        try:
            r = json.loads(body)
            if crypto.canonical_json(r).decode() != body:
                bad.append(f"receipt #{seq}: body is not in canonical form")
            bodies.append(r)
        except (ValueError, TypeError):
            bodies.append(None)
            bad.append(f"receipt #{seq}: body is not JSON")
    res = verify_receipts(bodies, pinned_pubkey, expect_head, expect_count, [r[0] for r in rows], [r[1] for r in rows])
    res.errors += bad
    res.ok = not res.errors
    return res
