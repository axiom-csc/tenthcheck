"""Panel Guard v7: consent by explicit confirmation (new version; v6.x stays frozen as measured).

v6.x decided consent by reading the customer's words with phrase rules (is "Count me in" a yes? is "Great." a yes? is
"Sure, I'll keep that in mind." a yes?). Every held-out set found another phrase the rules got wrong. v7 changes the
question: instead of guessing whether a message was consent, Panel ASKS, in words it generates from the action itself,
and accepts only a short, plain yes to that exact question.

Rules
  1. Before any action runs, Panel sends "To confirm: refund $24.00 on order #1042? Reply yes to proceed." (text built
     from the action's type, amount/percent and order id by `confirmation_prompt`; the model does not write it) and
     stores a pending record bound to that exact action (`fingerprint`: type, amount, percent, order id).
  2. Consent is a short affirmative reply (`is_affirmative`: a closed list such as yes / yes please / confirm / go ahead /
     do it / yep / count me in; no question mark, no hedge, no extra words) to THAT message: the bot's immediately
     previous message must contain the stored prompt, in the same conversation.
  3. Anything else: re-ask once (same question, one more time), then hand off to a person. An explicit no cancels
     (no re-ask: asking again after "no" would be wrong; this is a deliberate refinement of "anything else").
  4. The action that runs is the STORED one, never whatever the model proposes later. If the model proposes a different
     action (changed amount, other type) before the yes, the old pending record is superseded and the customer is asked
     again about the new one. The executor refuses an action whose fingerprint is not the confirmed one.
  5. Policy checks still apply twice: before the prompt is sent and again when the customer says yes.

What v7 does not do: it does not read intent from free text (so a customer who writes "Okay, the large then. Please go
ahead." in answer to the prompt is re-asked, not obeyed), and it adds turns (a prompt and a yes for every action).
No model is involved in the consent decision.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tenthlook.panel.guard.orders import ORDER_ID, find_order_id
from tenthlook.panel.guard.policy import Policy
from tenthlook.panel.guard.v5 import sentences
from tenthlook.panel.guard.v62 import compose, confirmation, executed_before, fallback_v62
from tenthlook.panel.guard.v63 import TYPE_WORDS, last_turn
from tenthlook.panel.guard.v64 import _allowed, evaluate_v64, instructions_v64

V7_VERSION = "guard-v7-confirm"
MAX_ASKS = 2          # the first prompt + one re-ask, then hand off

# ---------------------------------------------------------------------------------------------- the confirmation text
_PROMPTS = {
    "refund": "To confirm: refund ${amount:.2f} on order {order}? Reply yes to proceed.",
    "exchange": "To confirm: set up an exchange for order {order}? Reply yes to proceed.",
    "discount": "To confirm: add a {percent:g}% discount to your next order? Reply yes to proceed.",
    "handoff_to_human": "To confirm: connect you with a member of our team? Reply yes to proceed.",
    "store_credit": "To confirm: add ${amount:.2f} in store credit to your account? Reply yes to proceed.",
    "shipping_change": "To confirm: upgrade the shipping on order {order}? Reply yes to proceed.",
    "exception": "To confirm: make an exception to our policy for order {order}? Reply yes to proceed.",
}
REASK_LEAD = "I need a clear yes before I go ahead, and nothing has been changed yet."
DECLINED = "Okay, I won't do that. Is there anything else I can help with?"
CHANGED = ("Something changed between your confirmation and the action, so I haven't done anything. "
           "A member of our team will need to look at it.")
HANDOFF_TEXT = ("I wasn't able to get a clear yes, so I haven't changed anything. I'm passing this to a member of our "
                "team, who will pick it up from here.")


def confirmation_prompt(action: dict, order: dict | None) -> str:
    t = action.get("type")
    if t not in _PROMPTS:
        raise ValueError(f"no confirmation text for action type {t!r}")
    return _PROMPTS[t].format(amount=float(action.get("amount") or 0), percent=float(action.get("percent") or 0),
                              order=(order or {}).get("order_id", "?"))


def reask_prompt(action: dict, order: dict | None) -> str:
    return f"{REASK_LEAD} {confirmation_prompt(action, order)}"


def fingerprint(action: dict, order_id: str) -> str:
    amt = f"{float(action['amount']):.2f}" if action.get("amount") is not None else ""
    pct = f"{float(action['percent']):g}" if action.get("percent") is not None else ""
    return hashlib.sha256(f"{action.get('type', '')}|{amt}|{pct}|{order_id}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------------------------- yes / not-yes
_STRONG = ["yes", "yeah", "yep", "yup", "y", "sure", "absolutely", "definitely", "certainly", "confirm", "confirmed",
           "i confirm", "go ahead", "go for it", "do it", "proceed", "please proceed", "please do", "count me in",
           "i'm in", "let's do it", "lets do it", "sounds good", "that works"]
_FILLER = ["please", "thanks", "thank you", "ok", "okay", "alright"]   # fillers never make a yes by themselves
_LONE_OK = {"ok", "okay", "alright", "all right", "fine"}
_PHRASES = sorted([(p.split(), True) for p in _STRONG] + [(p.split(), False) for p in _FILLER], key=lambda x: -len(x[0]))
_DECLINE = re.compile(r"^\s*(?:no|nope|nah)\b(?!\s+(?:problem|worries))|\bno thanks\b|\bnever mind\b|\bcancel\b|\bforget it\b|"
                      r"\bdon'?t\b|\bdo not\b|\bstop\b|\bchanged my mind\b|\bnot interested\b|\bleave it\b", re.I)


def _norm(msg: str) -> list[str]:
    s = msg.lower().replace("’", "'")
    s = re.sub(r"[.,;:!…\"“”()]+", " ", s)
    return s.split()


def is_affirmative(msg: str) -> bool:
    """A short, plain yes: every word is from a closed list, at least one is a real yes word, and there is no question or hedge."""
    if not msg or "?" in msg:
        return False
    words = _norm(msg)
    if not words or len(words) > 6:
        return False
    joined = " ".join(words)
    if joined in _LONE_OK or joined in {f"{o} please" for o in _LONE_OK}:
        return True
    i, strong = 0, False
    while i < len(words):
        for ph, is_strong in _PHRASES:
            if words[i:i + len(ph)] == ph:
                i += len(ph)
                strong = strong or is_strong
                break
        else:
            return False
    return strong


def is_decline(msg: str) -> bool:
    return bool(_DECLINE.search(msg or ""))


# ---------------------------------------------------------------------------------------------- the pending record
@dataclass
class Pending:
    conversation_id: str
    fingerprint: str
    action: dict
    order_id: str
    prompt: str
    asked: int
    status: str


class ConfirmationStore:
    """One pending confirmation per conversation, bound to one exact action. SQLite (":memory:" or a file)."""

    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("""CREATE TABLE IF NOT EXISTS confirmations (
            conversation_id TEXT, fingerprint TEXT, action TEXT, order_id TEXT, prompt TEXT, asked INTEGER, status TEXT,
            created REAL, updated REAL, PRIMARY KEY (conversation_id, fingerprint))""")

    def _row(self, r) -> Pending:
        return Pending(r[0], r[1], json.loads(r[2]), r[3], r[4], r[5], r[6])

    def open(self, conv: str, action: dict, order_id: str, prompt: str, status: str = "pending") -> Pending:
        fp, now = fingerprint(action, order_id), time.time()
        with self._lock:
            self._db.execute("UPDATE confirmations SET status='superseded', updated=? WHERE conversation_id=? AND status='pending'", (now, conv))
            self._db.execute("INSERT OR REPLACE INTO confirmations VALUES (?,?,?,?,?,?,?,?,?)",
                             (conv, fp, json.dumps(action), order_id, prompt, 1, status, now, now))
        return self.pending(conv) if status == "pending" else self.get(conv, fp)

    def get(self, conv: str, fp: str) -> Pending | None:
        r = self._db.execute("SELECT conversation_id, fingerprint, action, order_id, prompt, asked, status FROM confirmations "
                             "WHERE conversation_id=? AND fingerprint=?", (conv, fp)).fetchone()
        return self._row(r) if r else None

    def pending(self, conv: str) -> Pending | None:
        r = self._db.execute("SELECT conversation_id, fingerprint, action, order_id, prompt, asked, status FROM confirmations "
                             "WHERE conversation_id=? AND status='pending' ORDER BY updated DESC LIMIT 1", (conv,)).fetchone()
        return self._row(r) if r else None

    def status_of(self, conv: str, fp: str) -> str | None:
        r = self._db.execute("SELECT status FROM confirmations WHERE conversation_id=? AND fingerprint=?", (conv, fp)).fetchone()
        return r[0] if r else None

    def _set(self, conv: str, fp: str, status: str, bump: bool = False) -> None:
        with self._lock:
            self._db.execute("UPDATE confirmations SET status=?, asked=asked+?, updated=? WHERE conversation_id=? AND fingerprint=?",
                             (status, 1 if bump else 0, time.time(), conv, fp))

    def confirm(self, conv: str) -> Pending | None:
        p = self.pending(conv)
        if p:
            self._set(conv, p.fingerprint, "confirmed")
        return p

    def bump(self, conv: str) -> None:
        p = self.pending(conv)
        if p:
            self._set(conv, p.fingerprint, "pending", bump=True)

    def close(self, conv: str, status: str) -> None:
        p = self.pending(conv)
        if p:
            self._set(conv, p.fingerprint, status)

    def mark(self, conv: str, fp: str, status: str) -> None:
        self._set(conv, fp, status)


def consent_v7(store: ConfirmationStore):
    """The executor's consent gate under v7: an action runs only if THIS conversation confirmed exactly this action."""
    def fn(action: dict, transcript, conversation_id: str | None = None, order_id: str = "") -> tuple[bool, str]:
        if not conversation_id:
            return False, "v7: no conversation id, so no confirmation to check"
        st = store.status_of(conversation_id, fingerprint(action, order_id or ""))
        if st in ("confirmed", "executed"):
            return True, "the customer confirmed exactly this action"
        return False, f"v7: this exact action was not confirmed (status: {st or 'never asked'})"
    return fn


# ---------------------------------------------------------------------------------------------- the guarded turn
def instructions_v7(policy: Policy, order: dict | None, today: date, done: list[dict]) -> str:
    return instructions_v64(policy, order, today, done) + (
        "\nPanel asks the customer to confirm before ANY action runs. When the customer clearly asks for an action, or "
        "agrees to one, add the action and keep the message short. Do not write a confirmation question yourself and do "
        "not say it is done: Panel sends the confirmation question and runs the action only after the customer's yes.")


def guard_system_v7(policy: Policy, order: dict | None, today: date, done: list[dict] | None = None) -> str:
    o = ""
    if order:
        o = (f"\nThe customer's order {order['order_id']}: {order.get('item', 'item')}, bought {order['purchase_date']}, "
             f"paid ${order['amount']:g}, on sale: {'yes' if order.get('on_sale') else 'no'}, "
             f"receipt: {'yes' if order.get('has_receipt') else 'no'}.")
    return f"Today's date is {today.isoformat()}.{o}\n\n" + instructions_v7(policy, order, today, done or [])


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _defer(action: dict, convo: list[dict]) -> tuple[bool, str]:
    return True, "consent is decided later by the v7 confirmation"


def _claim_ok(policy: Policy, order: dict | None, today: date, action: dict) -> bool:
    if action.get("type") == "handoff_to_human":
        return True
    if order is None:
        return False
    claim = {"type": {"refund": "refund", "exchange": "exchange", "discount": "discount"}.get(action["type"], action["type"]),
             **({"amount": float(action["amount"])} if action.get("amount") is not None else {}),
             **({"percent": float(action["percent"])} if action.get("percent") is not None else {})}
    return _allowed(policy, order, today, claim)


def _result(sent, outcome, actions, oid, attempts, done, repeated, regen, usage, added_s, v7: dict, ack=False) -> dict:
    return {"sent": sent, "outcome": outcome, "actions": actions, "order_id": oid, "attempts": attempts, "already_done": done,
            "repeated_actions": repeated, "acknowledged_before_refusal": ack, "regenerations": regen, "usage": usage,
            "added_s": added_s, "v7": v7}


def guarded_v7(messages: list[dict], call, policy: Policy, orders, store: ConfirmationStore, executor=None,
               conversation_id: str | None = None, max_retries: int = 2) -> dict:
    """One guarded turn under v7. `call(messages) -> (raw_text, meta)` is one upstream completion (used only when the
    customer's message is not a plain yes to a pending confirmation)."""
    from tenthlook.panel.guard.proxy import conversation_fallback_id, feedback
    convo = [m for m in messages if m.get("role") != "system"]
    oid = find_order_id(*(_text(m.get("content")) for m in convo if m.get("role") in ("user", "assistant")),
                        pattern=getattr(orders, "order_id_pattern", ORDER_ID))
    order = orders.get_order(oid) if oid else None
    order_id = (order or {}).get("order_id", "")
    cid = conversation_id or conversation_fallback_id(convo, oid)
    done = executed_before(convo)
    msg, prev = last_turn(convo)
    pend = store.pending(cid)
    stale = False
    if pend is not None and pend.prompt not in prev:          # consent counts only as a reply to THAT message
        store.close(cid, "stale")
        pend, stale = None, True
    if pend is not None:
        action = pend.action
        if is_affirmative(msg):
            if fingerprint(action, order_id) != pend.fingerprint or pend.order_id != order_id:
                store.close(cid, "changed")
                return _result(CHANGED, "v7_changed", [], oid, [], done, [], 0, {}, 0.0, {"event": "changed"})
            if not _claim_ok(policy, order, orders.today, action):
                store.close(cid, "blocked")
                return _result(CHANGED, "v7_blocked", [], oid, [], done, [], 0, {}, 0.0, {"event": "blocked_at_yes"})
            store.confirm(cid)
            record = executor.submit(cid, order, action, transcript=convo) if executor is not None else None
            store.mark(cid, pend.fingerprint, "executed")
            out = {**action, **({"status": record.status, "key": record.key} if record is not None else {"v7": "confirmed"})}
            return _result(confirmation(action, order, record), "v7_executed", [out], oid, [], done, [], 0, {}, 0.0,
                           {"event": "executed", "asked": pend.asked})
        if is_decline(msg):
            store.close(cid, "declined")
            return _result(DECLINED, "v7_declined", [], oid, [], done, [], 0, {}, 0.0, {"event": "declined"})
    sys_msg = {"role": "system", "content": "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system").strip()
               + ("\n\n" if any(m.get("role") == "system" for m in messages) else "") + guard_system_v7(policy, order, orders.today, done)}
    attempts, extra, usage = [], [], {}
    for i in range(1 + max_retries):
        t0 = time.perf_counter()
        raw, meta = call([sys_msg, *convo, *extra])
        gen_s = time.perf_counter() - t0
        ev = evaluate_v64(raw, policy, order, orders.today, done, convo, consent_fn=_defer)
        for k, v in (meta.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] = usage.get(k, 0) + v
        attempts.append({"attempt": i, "ok": ev["ok"], "issues": ev["issues"], "raw": raw, "text": ev["message"],
                         "actions": ev["actions"], "gen_s": round(gen_s, 3), "model": meta.get("model")})
        if not ev["ok"]:
            extra = [{"role": "assistant", "content": raw}, {"role": "user", "content": feedback(ev["issues"])}]
            continue
        new = [a for a in ev["valid_actions"] if a.get("type") in _PROMPTS]
        repeated = ev.get("repeated_actions", [])
        regen = len(attempts) - 1
        if new:
            a = new[0]                                         # one confirmation at a time
            if pend is not None and fingerprint(a, order_id) != pend.fingerprint:
                pass                                           # changed parameters: superseded below, customer asked again
            elif pend is not None:
                a = pend.action                                # same action again: keep the existing question (re-ask below)
            if pend is None or fingerprint(a, order_id) != pend.fingerprint:
                store.open(cid, a, order_id, confirmation_prompt(a, order))
                return _result(confirmation_prompt(a, order), "v7_confirm", [], oid, attempts, done, repeated, regen, usage,
                               round(sum(x["gen_s"] for x in attempts[1:]), 3), {"event": "confirm", "superseded": pend is not None})
        if pend is not None:                                   # not a yes, no new action: re-ask once, then hand off
            if pend.asked < MAX_ASKS:
                store.bump(cid)
                return _result(reask_prompt(pend.action, order), "v7_reask", [], oid, attempts, done, repeated, regen, usage,
                               round(sum(x["gen_s"] for x in attempts[1:]), 3), {"event": "reask", "asked": pend.asked + 1})
            store.close(cid, "handed_off")
            ho = {"type": "handoff_to_human", "v7_reason": "unconfirmed"}
            if executor is not None:
                store.open(cid, ho, order_id, "(hand-off after no clear yes)", status="confirmed")
                rec = executor.submit(cid, order, ho, transcript=convo)
                ho = {**ho, "status": rec.status, "key": rec.key}
            return _result(HANDOFF_TEXT, "v7_handoff", [ho], oid, attempts, done, repeated, regen, usage,
                           round(sum(x["gen_s"] for x in attempts[1:]), 3), {"event": "handoff"})
        sent, ack = compose(ev["message"], [], repeated, done, order)
        return _result(sent, "passed" if regen == 0 else "passed_after_regeneration", [], oid, attempts, done, repeated, regen,
                       usage, round(sum(x["gen_s"] for x in attempts[1:]), 3), {"event": "none", "stale_pending": stale}, ack)
    return _result(fallback_v62(policy, done, order), "fallback", [], oid, attempts, done, [], len(attempts) - 1, usage,
                   round(sum(x["gen_s"] for x in attempts[1:]), 3), {"event": "fallback"})


__all__ = ["V7_VERSION", "ConfirmationStore", "confirmation_prompt", "reask_prompt", "fingerprint", "is_affirmative", "is_decline",
           "consent_v7", "guarded_v7", "instructions_v7", "guard_system_v7"]
