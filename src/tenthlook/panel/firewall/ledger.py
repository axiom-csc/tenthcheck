"""The shared action ledger: append-only SQLite, used by every connector.

One table, `events`. Rows are only ever INSERTed; triggers make UPDATE and DELETE fail. The state of an action is
derived from its events. Idempotency is enforced two ways: `claim()` is an atomic check-then-insert inside
BEGIN IMMEDIATE, and a partial UNIQUE index allows at most one `executed` event per idempotency key, so even a bug
elsewhere cannot record two executions.

Events per key: requested, pending (needs consent; carries the action), approved / denied (who, when; written ONLY by
the out-of-band channels: `usepanel approve`, the local UI/HTTP endpoint), claimed (about to call the system of
record), executed (after success only), failed (after an error; the key may then be retried), blocked, dry_run,
receipt (the signed receipt).
A key left in `claimed` with no outcome (crash mid-call) is NOT retried automatically: its outcome is unknown.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from tenthlook.panel.firewall.types import canonical, sha256_hex

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, idem_key TEXT NOT NULL, conversation_id TEXT NOT NULL,
  tool TEXT NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_key ON events(idem_key, seq);
CREATE INDEX IF NOT EXISTS events_conv ON events(conversation_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS one_execution ON events(idem_key) WHERE event = 'executed';
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""


def approval_id(idem_key: str) -> str:
    return sha256_hex(idem_key)[:10]


class Ledger:
    def __init__(self, path: str | Path = ":memory:", clock=time.time):
        self.path, self.clock = str(path), clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=15)
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=15000")
        self._db.executescript(SCHEMA)

    # ------------------------------------------------------------------ writes
    def append(self, idem_key: str, conversation_id: str, tool: str, event: str, data: dict | None = None) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO events(ts, idem_key, conversation_id, tool, event, data) VALUES (?,?,?,?,?,?)",
                                   (self.clock(), idem_key, conversation_id, tool, event, canonical(data or {})))
            return cur.lastrowid

    def append_receipt(self, idem_key: str, conversation_id: str, tool: str, build) -> dict:
        """Write a chained receipt. `build(prev_receipt_sha256)` returns (receipt, detail). The chain head is read and the new
        receipt inserted inside one BEGIN IMMEDIATE, so concurrent writers (other processes on the same ledger file) cannot
        fork the chain. The event stores {"receipt", "detail"}."""
        from tenthlook.core import receipt as core
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("""SELECT data FROM events WHERE event='receipt' AND json_extract(data,'$.receipt') IS NOT NULL
                                          ORDER BY seq DESC LIMIT 1""").fetchone()
                prev = core.receipt_hash(json.loads(row[0])["receipt"]) if row else core.GENESIS
                receipt, detail = build(prev)
                self._db.execute("INSERT INTO events(ts, idem_key, conversation_id, tool, event, data) VALUES (?,?,?,?,?,?)",
                                 (self.clock(), idem_key, conversation_id, tool, "receipt", canonical({"receipt": receipt, "detail": detail})))
                self._db.execute("COMMIT")
                return receipt
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def claim(self, idem_key: str, conversation_id: str, tool: str) -> tuple[str, dict | None]:
        """Atomically decide whether the caller may execute. Returns (outcome, prior):
        'claimed' (go ahead), 'executed' (prior = the executed event data; do NOT run), 'in_flight' (someone else is
        mid-call, or crashed mid-call: do not run)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                last = self._db.execute("""SELECT event, data FROM events WHERE idem_key=? AND event IN
                                           ('claimed','executed','failed') ORDER BY seq DESC LIMIT 1""", (idem_key,)).fetchone()
                if last and last[0] == "executed":
                    out = ("executed", json.loads(last[1]))
                elif last and last[0] == "claimed":
                    out = ("in_flight", None)
                else:
                    self._db.execute("INSERT INTO events(ts, idem_key, conversation_id, tool, event, data) VALUES (?,?,?,?,?,?)",
                                     (self.clock(), idem_key, conversation_id, tool, "claimed", "{}"))
                    out = ("claimed", None)
                self._db.execute("COMMIT")
                return out
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def approve(self, aid: str, who: str, deny: bool = False, reason: str = "") -> tuple[bool, str]:
        """Out-of-band consent. Only a `pending` key can be approved/denied, once."""
        who = (who or "").strip()
        if not who:
            return False, "who is required"
        with self._lock:
            row = self._db.execute("""SELECT idem_key, conversation_id, tool, data FROM events WHERE event='pending'
                                      AND json_extract(data,'$.approval_id')=? ORDER BY seq DESC LIMIT 1""", (aid,)).fetchone()
            if not row:
                return False, f"no pending action with id {aid}"
            key, conv, tool, data = row
            st = self.consent_state(key)
            if st != "pending":
                return False, f"action {aid} is already {st}"
            self.append(key, conv, tool, "denied" if deny else "approved",
                        {"approval_id": aid, "who": who, "reason": reason, "action_sha": json.loads(data)["action_sha"]})
            return True, "denied" if deny else "approved"

    # ------------------------------------------------------------------ reads
    def _rows(self, q: str, args=()) -> list[tuple]:
        with self._lock:
            return self._db.execute(q, args).fetchall()

    def events(self, idem_key: str | None = None, conversation_id: str | None = None, since_seq: int = 0,
               limit: int = 1000) -> list[dict]:
        q, a = "SELECT seq, ts, idem_key, conversation_id, tool, event, data FROM events WHERE seq > ?", [since_seq]
        if idem_key:
            q, a = q + " AND idem_key=?", a + [idem_key]
        if conversation_id:
            q, a = q + " AND conversation_id=?", a + [conversation_id]
        rows = self._rows(q + " ORDER BY seq LIMIT ?", (*a, limit))
        return [{"seq": r[0], "ts": r[1], "idem_key": r[2], "conversation_id": r[3], "tool": r[4], "event": r[5],
                 "data": json.loads(r[6])} for r in rows]

    def consent_state(self, idem_key: str) -> str | None:
        """pending | approved | denied | None (never needed consent)."""
        r = self._rows("""SELECT event FROM events WHERE idem_key=? AND event IN ('pending','approved','denied')
                          ORDER BY seq DESC LIMIT 1""", (idem_key,))
        return r[0][0] if r else None

    def approved_for(self, idem_key: str, action_sha: str) -> dict | None:
        """The approval event, if the latest consent event for this key is an approval of THIS exact action."""
        r = self._rows("""SELECT event, data FROM events WHERE idem_key=? AND event IN ('pending','approved','denied')
                          ORDER BY seq DESC LIMIT 1""", (idem_key,))
        if r and r[0][0] == "approved":
            d = json.loads(r[0][1])
            if d.get("action_sha") == action_sha:
                return d
        return None

    def prior_execution(self, idem_key: str) -> dict | None:
        r = self._rows("SELECT data FROM events WHERE idem_key=? AND event='executed'", (idem_key,))
        return json.loads(r[0][0]) if r else None

    def action_sha_for_key(self, idem_key: str) -> str | None:
        r = self._rows("""SELECT json_extract(data,'$.action_sha') FROM events WHERE idem_key=? AND event IN
                          ('pending','claimed','executed','requested') AND json_extract(data,'$.action_sha') IS NOT NULL
                          ORDER BY seq LIMIT 1""", (idem_key,))
        return r[0][0] if r else None

    def pending(self) -> list[dict]:
        out = []
        for r in self._rows("SELECT idem_key FROM events WHERE event='pending' GROUP BY idem_key"):
            if self.consent_state(r[0]) == "pending" and not self.prior_execution(r[0]):
                ev = [e for e in self.events(idem_key=r[0]) if e["event"] == "pending"][-1]
                out.append({"approval_id": ev["data"]["approval_id"], "tool": ev["tool"], "ts": ev["ts"],
                            "conversation_id": ev["conversation_id"], "args": ev["data"].get("args"),
                            "reasons": ev["data"].get("reasons", []), "action_sha": ev["data"]["action_sha"]})
        return sorted(out, key=lambda p: p["ts"])

    def executed_for(self, tool: str, since_ts: float) -> list[tuple[float, float | None]]:
        rows = self._rows("""SELECT ts, json_extract(data,'$.amount') FROM events WHERE event='executed' AND tool=? AND ts>?
                             ORDER BY seq""", (tool, since_ts))
        return [(r[0], r[1]) for r in rows]

    def executed_in_conversation(self, conversation_id: str) -> list[dict]:
        return [e for e in self.events(conversation_id=conversation_id, limit=100000) if e["event"] == "executed"]

    def receipts(self) -> list[dict]:
        """Signed tenthlook-core receipts, oldest first (the chain). Pre-chain receipts from earlier versions are not included."""
        return [e["data"]["receipt"] for e in self.events(limit=10**6) if e["event"] == "receipt" and "receipt" in e["data"]]

    def receipt_details(self) -> list[dict]:
        """The detail record behind each receipt's trace_sha256, in the same order as receipts()."""
        return [e["data"]["detail"] for e in self.events(limit=10**6) if e["event"] == "receipt" and "receipt" in e["data"]]
