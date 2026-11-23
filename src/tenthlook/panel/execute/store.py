"""The action ledger: one row per idempotency key, in SQLite (stdlib). It is what guarantees never-twice.

Statuses: dry_run -> (superseded by a real submit) | queued -> approved | rejected | auto ->
executing -> executed | failed | blocked.
A key is executed only by the call that atomically moves it into "executing" (UPDATE ... WHERE status IN
('approved', 'auto')), so two threads or processes can't both run it. A row in "executing" after a crash is NOT
retried automatically: its outcome is unknown and a person must check Shopify (the Shopify idempotency key makes a
manual retry within 24 hours safe).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Record:
    key: str
    conversation_id: str
    order_id: str
    type: str
    amount: float | None
    percent: float | None
    status: str
    mode: str
    created: float
    updated: float
    decision: str = ""          # why it was queued / auto-executed
    result: dict = field(default_factory=dict)
    error: str = ""
    decided_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class ActionStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL" if self.path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self._db.execute("""CREATE TABLE IF NOT EXISTS actions (
            key TEXT PRIMARY KEY, conversation_id TEXT, order_id TEXT, type TEXT, amount REAL, percent REAL,
            status TEXT, mode TEXT, created REAL, updated REAL, decision TEXT, result TEXT, error TEXT, decided_by TEXT)""")
        self._db.execute("CREATE TABLE IF NOT EXISTS transcripts (conversation_id TEXT PRIMARY KEY, messages TEXT, updated REAL)")

    def save_transcript(self, conversation_id: str, messages: list[dict]) -> None:
        keep = [{"role": m.get("role"), "content": m.get("content")} for m in messages if m.get("role") in ("user", "assistant")]
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO transcripts VALUES (?,?,?)", (conversation_id, json.dumps(keep), time.time()))

    def transcript(self, conversation_id: str) -> list[dict]:
        with self._lock:
            r = self._db.execute("SELECT messages FROM transcripts WHERE conversation_id=?", (conversation_id,)).fetchone()
        return json.loads(r[0]) if r else []

    def _row(self, r) -> Record | None:
        if r is None:
            return None
        d = dict(zip(("key", "conversation_id", "order_id", "type", "amount", "percent", "status", "mode", "created",
                      "updated", "decision", "result", "error", "decided_by"), r))
        d["result"] = json.loads(d["result"] or "{}")
        return Record(**d)

    def get(self, key: str) -> Record | None:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM actions WHERE key=?", (key,)).fetchone())

    def insert(self, rec: Record, replace_status: str | None = None) -> bool:
        """Insert a new row; or, if replace_status is given, overwrite a row only while it has that status
        (used to turn a dry_run row into a real one). Returns False if another writer got there first."""
        vals = (rec.key, rec.conversation_id, rec.order_id, rec.type, rec.amount, rec.percent, rec.status, rec.mode,
                rec.created, rec.updated, rec.decision, json.dumps(rec.result), rec.error, rec.decided_by)
        with self._lock:
            if replace_status is None:
                cur = self._db.execute("INSERT OR IGNORE INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
            else:
                cur = self._db.execute("""UPDATE actions SET status=?, mode=?, updated=?, decision=?, result=?, error=?
                                          WHERE key=? AND status=?""",
                                       (rec.status, rec.mode, rec.updated, rec.decision, json.dumps(rec.result),
                                        rec.error, rec.key, replace_status))
            return cur.rowcount == 1

    def transition(self, key: str, from_statuses: tuple[str, ...], to: str, **fields) -> bool:
        """Atomically move key from one of from_statuses to `to`. True only for the caller that made the move."""
        sets, vals = ["status=?", "updated=?"], [to, time.time()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(v) if k == "result" else v)
        q = f"UPDATE actions SET {', '.join(sets)} WHERE key=? AND status IN ({','.join('?' * len(from_statuses))})"
        with self._lock:
            return self._db.execute(q, (*vals, key, *from_statuses)).rowcount == 1

    def list(self, statuses: tuple[str, ...] | None = None, conversation_id: str | None = None, limit: int = 200) -> list[Record]:
        q, args = "SELECT * FROM actions", []
        conds = []
        if statuses:
            conds.append(f"status IN ({','.join('?' * len(statuses))})")
            args += list(statuses)
        if conversation_id:
            conds.append("conversation_id=?")
            args.append(conversation_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created DESC LIMIT ?"
        with self._lock:
            return [self._row(r) for r in self._db.execute(q, (*args, limit)).fetchall()]
