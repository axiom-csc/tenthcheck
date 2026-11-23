"""Carries out approved actions, once each, in one of three modes.

- dry_run (default): records what would happen (and whether auto mode would have executed or queued it); calls
  nothing. A later real submit of the same key replaces the dry-run row.
- approval: every action is queued; a person approves or rejects it on the local approval page
  (src/tenthlook/panel/execute/approval_page.py); only approved actions execute.
- auto: actions within the per-action limits in the policy's [execution] table execute at once; anything above a
  limit, or a type without a limit, is queued for approval.

Consent (v6.3): an action runs only if the transcript shows the customer asked for it in their latest message or
said yes to the bot's offer of it in the previous reply (src/tenthlook/panel/guard/v65.consent_v65 since v6.5: hedges,
deferrals and bare acknowledgements are not a yes). Otherwise it is recorded as
"needs_consent" and nothing runs; a later consented submit of the same action replaces that row. No transcript means
no evidence of consent, so nothing runs.

Idempotency: an action's key is sha256(conversation, order, type, amount/percent). A key is recorded once and
executed at most once (src/tenthlook/panel/execute/store.py), whatever the mode or how often the bot repeats the action. Shopify
refunds also carry a key derived from it (Shopify's own 24-hour idempotency).

Before anything runs, the executor re-reads the order (fresh, not cached) and re-checks the action against the
policy with the same deterministic checker the guard uses, so a refund that became invalid since the guard checked
it (another refund in the meantime, an order that changed) is blocked, not executed. By default it refuses to touch
an order that is not a Shopify test order (set PANEL_ALLOW_LIVE_ORDERS=1 to change that, deliberately).

Backends: refunds and discount codes go to Shopify (src/tenthlook/panel/shopify/actions.py). Exchanges have no Shopify call here:
an approved exchange is sent to the team through the hand-off channel to process by hand. A hand-off is sent through
the hand-off channel (webhook or email); if none is configured it fails, and the bot says so instead of claiming it.
"""
from __future__ import annotations

import hashlib
import logging
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from tenthlook.panel.execute.handoff import summarize
from tenthlook.panel.execute.store import ActionStore, Record
from tenthlook.panel.guard.check import check_reply
from tenthlook.panel.guard.policy import Policy
from tenthlook.panel.guard.v65 import consent_v65 as consent

log = logging.getLogger("tenthlook.panel.execute")
MODES = ("dry_run", "approval", "auto")
CLAIM = {"refund": "refund", "discount": "discount", "exchange": "exchange", "handoff_to_human": "escalate",
         "store_credit": "compensation", "shipping_change": "compensation", "exception": "exception"}


@dataclass
class Limits:
    """Per-action limits for auto mode, from the policy file's [execution] table."""
    auto_refund_max: float = 0.0          # refunds up to this amount execute in auto mode; above -> approval
    auto_discount_max_percent: float = 0.0
    auto_handoff: bool = True             # hand-offs are always sent at once in approval/auto mode if True
    auto_exchange: bool = False           # exchanges are manual work: queued unless True
    mode: str = "dry_run"

    @classmethod
    def from_policy_file(cls, path: str | Path) -> "Limits":
        ex = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("execution") or {}
        lim = cls(**{k: ex[k] for k in cls.__dataclass_fields__ if k in ex})
        if lim.mode not in MODES:
            raise ValueError(f"[execution] mode must be one of {MODES}")
        return lim


def action_key(conversation_id: str, order_id: str, action: dict) -> str:
    t = action.get("type", "")
    amt = f"{float(action['amount']):.2f}" if action.get("amount") is not None else ""
    pct = f"{float(action['percent']):g}" if action.get("percent") is not None else ""
    return hashlib.sha256(f"{conversation_id}|{order_id}|{t}|{amt}|{pct}".encode()).hexdigest()[:32]


class Executor:
    def __init__(self, store: ActionStore, backend, orders, policy: Policy, limits: Limits, notifier,
                 mode: str | None = None, allow_live_orders: bool = False, clock=time.time, consent_fn=None):
        # consent_fn(action, transcript, conversation_id=, order_id=) -> (ok, why). None = the v6.5 transcript rule (unchanged);
        # v7 passes tenthlook.panel.guard.v7.consent_v7(store), which checks the exact confirmed action instead of reading words.
        self.consent_fn = consent_fn
        self.store, self.backend, self.orders, self.policy, self.limits = store, backend, orders, policy, limits
        self.notifier, self.allow_live, self.clock = notifier, allow_live_orders, clock
        self.mode = mode or limits.mode
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")

    # ---------------------------------------------------------------- decisions
    def auto_decision(self, action: dict) -> tuple[bool, str]:
        t = action.get("type")
        if t == "refund":
            amt = float(action.get("amount") or 0)
            ok = amt <= self.limits.auto_refund_max
            return ok, f"refund ${amt:.2f} {'<=' if ok else '>'} auto limit ${self.limits.auto_refund_max:.2f}"
        if t == "discount":
            pct = float(action.get("percent") or 0)
            ok = pct <= self.limits.auto_discount_max_percent
            return ok, f"discount {pct:g}% {'<=' if ok else '>'} auto limit {self.limits.auto_discount_max_percent:g}%"
        if t == "handoff_to_human":
            return self.limits.auto_handoff, "hand-off " + ("sent automatically" if self.limits.auto_handoff else "needs approval")
        if t == "exchange":
            return self.limits.auto_exchange, "exchange " + ("auto" if self.limits.auto_exchange else "needs approval (manual)")
        return False, f"{t}: no auto limit; needs approval"

    # ---------------------------------------------------------------- public API
    def submit(self, conversation_id: str, order: dict | None, action: dict, transcript: list[dict] | None = None) -> Record:
        order_id = (order or {}).get("order_id", "")
        key = action_key(conversation_id, order_id, action)
        if transcript is not None:
            self.store.save_transcript(conversation_id, transcript)
        existing = self.store.get(key)
        replaceable = ("dry_run", "needs_consent")
        if existing and existing.status not in replaceable:
            log.info("action %s already %s; not run again", key[:8], existing.status)
            return existing
        if self.consent_fn is None:
            ok, why_consent = consent(action, transcript or [])
        else:
            ok, why_consent = self.consent_fn(action, transcript or [], conversation_id=conversation_id, order_id=order_id)
        if not ok:  # checked here too, independently of the prompt and the guard (v6.3)
            rec = self._record(key, conversation_id, order_id, action, "needs_consent", self.mode, why_consent)
            if not existing:
                self.store.insert(rec)
            else:
                self.store.insert(rec, replace_status=existing.status)
            log.info("action %s not run: %s", key[:8], why_consent)
            return self.store.get(key)
        auto_ok, why = self.auto_decision(action)
        if self.mode == "dry_run":
            would = "would execute (auto mode)" if auto_ok else "would queue for approval"
            rec = self._record(key, conversation_id, order_id, action, "dry_run", "dry_run", f"{would}: {why}")
            if not existing:
                self.store.insert(rec)
            log.info("dry run: %s %s", action.get("type"), rec.decision)
            return self.store.get(key)
        if self.mode == "approval" and not (action.get("type") == "handoff_to_human" and self.limits.auto_handoff):
            status, why = "queued", "approval mode"
        else:
            status = "auto" if auto_ok else "queued"
        rec = self._record(key, conversation_id, order_id, action, status, self.mode, why)
        won = self.store.insert(rec, replace_status=existing.status if existing else None)
        if not won:  # someone else recorded this key first: theirs stands
            return self.store.get(key)
        if status == "auto":
            return self._execute(key, order)
        return self.store.get(key)

    def _record(self, key: str, conversation_id: str, order_id: str, action: dict, status: str, mode: str,
                decision: str) -> Record:
        now = self.clock()
        return Record(key, conversation_id, order_id, action.get("type", ""), action.get("amount"), action.get("percent"),
                      status, mode, now, now, decision)

    def approve(self, key: str, who: str = "approver") -> Record:
        if self.store.transition(key, ("queued",), "approved", decided_by=who):
            return self._execute(key, None)
        return self.store.get(key)

    def reject(self, key: str, who: str = "approver", reason: str = "") -> Record:
        self.store.transition(key, ("queued",), "rejected", decided_by=who, error=reason)
        return self.store.get(key)

    def pending(self) -> list[Record]:
        return self.store.list(("queued",))

    # ---------------------------------------------------------------- execution
    def _execute(self, key: str, order_hint: dict | None) -> Record:
        if not self.store.transition(key, ("approved", "auto"), "executing"):
            return self.store.get(key)  # someone else is running or has run it
        rec = self.store.get(key)
        action = {"type": rec.type, "amount": rec.amount, "percent": rec.percent}
        try:
            order = self._fresh_order(rec.order_id) if rec.order_id else None
            if rec.type in ("refund", "discount"):
                if order is None:
                    return self._block(key, "order not found when re-checked")
                if not order.get("test") and not self.allow_live:
                    return self._block(key, "not a Shopify test order (set PANEL_ALLOW_LIVE_ORDERS=1 to allow live orders)")
                problem = self._recheck(rec, order)
                if problem:
                    return self._block(key, problem)
            if rec.type in ("handoff_to_human", "exchange"):
                reason = "customer hand-off" if rec.type == "handoff_to_human" else "approved exchange to process by hand"
                result = self.notifier.send(self._handoff_summary(rec, order or order_hint, reason))
            else:
                result = self.backend.execute(action, order, key)
        except Exception as e:  # recorded, never retried automatically
            self.store.transition(key, ("executing",), "failed", error=f"{type(e).__name__}: {e}"[:500])
            log.warning("action %s failed: %s", key[:8], type(e).__name__)
            self._handoff_after_failure(key)
            return self.store.get(key)
        self.store.transition(key, ("executing",), "executed", result=result or {})
        log.info("action %s executed (%s)", key[:8], rec.type)
        return self.store.get(key)

    def _recheck(self, rec: Record, order: dict) -> str | None:
        """Re-check a refund/discount against the policy on the fresh order; the reason it fails, or None."""
        claim = {"type": CLAIM[rec.type], "status": "promise",
                 **({"amount": rec.amount} if rec.amount is not None else {}),
                 **({"percent": rec.percent} if rec.percent is not None else {})}
        chk = check_reply([claim], self.policy, order, self.orders.today)
        if chk["ok"]:
            return None
        return "policy re-check failed: " + " ".join(v["reason"] for v in chk["violations"]) + " ".join(chk.get("unverified") or [])

    def _handoff_summary(self, rec: Record, order: dict | None, reason: str) -> dict:
        return summarize(rec.conversation_id, order, self.store.transcript(rec.conversation_id),
                         [r.to_dict() for r in self.store.list(conversation_id=rec.conversation_id)], reason)

    def _block(self, key: str, why: str) -> Record:
        self.store.transition(key, ("executing",), "blocked", error=why)
        log.warning("action %s blocked: %s", key[:8], why)
        return self.store.get(key)

    def _fresh_order(self, order_id: str) -> dict | None:
        try:
            return self.orders.get_order(order_id, fresh=True)
        except TypeError:  # lookups without a cache (JsonOrders)
            return self.orders.get_order(order_id)

    def _handoff_after_failure(self, key: str) -> None:
        rec = self.store.get(key)
        if rec.type == "handoff_to_human":
            return
        try:
            self.notifier.send(self._handoff_summary(rec, None, f"{rec.type} failed: {rec.error}"))
        except Exception:
            log.warning("could not send the failure hand-off for %s", key[:8])


class ShopifyBackend:
    """Refunds and discount codes through the Shopify Admin API."""

    def __init__(self, client):
        self.client = client

    def execute(self, action: dict, order: dict, key: str) -> dict:
        from tenthlook.panel.shopify.actions import create_discount_code, create_refund, shopify_key
        if action["type"] == "refund":
            return create_refund(self.client, order, float(action["amount"]), shopify_key(key),
                                 note=f"Panel refund (action {key[:12]})")
        if action["type"] == "discount":
            return create_discount_code(self.client, order, float(action["percent"]), code=f"PANEL-{key[:8].upper()}")
        raise ValueError(f"no Shopify backend for {action['type']}")


class FakeBackend:
    """For tests and local demos: records calls, changes nothing."""

    def __init__(self, fail: bool = False):
        self.calls, self.fail = [], fail

    def execute(self, action: dict, order: dict, key: str) -> dict:
        self.calls.append((action, (order or {}).get("order_id"), key))
        if self.fail:
            raise RuntimeError("backend down")
        if action["type"] == "discount":
            return {"code": f"PANEL-{key[:8].upper()}"}
        return {"refund_id": f"fake-{len(self.calls)}"}
