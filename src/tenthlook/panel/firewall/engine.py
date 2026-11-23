"""The firewall: intercept -> verify -> consent -> execute (once) -> log -> receipt.

Fail closed: any exception while validating, any unknown tool, bad argument shape or ledger problem is a BLOCK with a
reason; nothing here passes a call through on error. Consent is only ever read from the ledger (written by
`usepanel approve` / the local UI), never from the tool call itself.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from tenthlook.panel.firewall.connector import Connector
from tenthlook.panel.firewall.ledger import Ledger, approval_id
from tenthlook.panel.firewall.policy import LedgerState, Policy, window_needed
from tenthlook.panel.firewall.receipts import Signer, make_receipt
from tenthlook.panel.firewall.types import (ALLOW, BLOCK, NEEDS_CONSENT, Action, FirewallError, Result, Verdict)

log = logging.getLogger("tenthlook.panel.firewall")


@dataclass
class Outcome:
    status: str                    # executed | replayed | blocked | needs_consent | dry_run | failed | in_flight
    verdict: Verdict
    result: Result | None = None
    approval_id: str | None = None
    receipt: dict | None = None
    idempotency_key: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "verdict": self.verdict.to_dict(), "approval_id": self.approval_id,
                "result": self.result.to_dict() if self.result else None, "idempotency_key": self.idempotency_key,
                "receipt": self.receipt}


class Firewall:
    def __init__(self, policy: Policy, ledger: Ledger, signer: Signer, connectors: list[Connector], mode: str | None = None,
                 clock=time.time, consent_wait: float = 0.0, sleep=time.sleep):
        if signer is None:
            raise FirewallError("a signing key is required (run `usepanel keygen`)")
        self.policy, self.ledger, self.signer, self.connectors = policy, ledger, signer, connectors
        self.mode = mode or policy.mode
        self.clock, self.consent_wait, self.sleep = clock, consent_wait, sleep

    # -------------------------------------------------------------- helpers
    def connector_for(self, tool: str) -> Connector | None:
        for c in self.connectors:
            if c.owns(tool):
                return c
        return None

    def _risk(self, tool: str, c: Connector | None) -> str:
        rule = self.policy.rule_for(tool) or {}
        if rule.get("risk"):
            return rule["risk"]
        if c:
            for a in c.describe_actions():
                if a.name == tool:
                    return a.risk
        return "write"

    def _state(self, tool: str, now: float) -> LedgerState:
        w = window_needed(self.policy.rule_for(tool) or {})
        return LedgerState(self.ledger.executed_for(tool, now - w) if w else [])

    def _amount(self, action: Action) -> float | None:
        rule = self.policy.rule_for(action.tool) or {}
        v = action.args.get(rule.get("amount_field", "amount")) if isinstance(action.args, dict) else None
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def _receipt(self, action: Action, key: str, verdict: Verdict, outcome: str, result_id, approved_by=None) -> dict | None:
        try:
            ts = self.clock()
            return self.ledger.append_receipt(key, action.conversation_id, action.tool, lambda prev: make_receipt(
                self.signer, action, key, self.policy.sha256, self.policy.version, verdict, outcome, result_id, ts, approved_by, prev))
        except Exception as e:  # the action already happened or was refused; a missing receipt must be visible
            log.error("could not write receipt for %s: %s", key, type(e).__name__)
            return None

    def _blocked(self, action: Action, key: str, reasons: list[str], event="blocked") -> Outcome:
        v = Verdict(BLOCK, reasons)
        try:
            self.ledger.append(key, action.conversation_id, action.tool, event, {"reasons": reasons, "action_sha": _safe_sha(action)})
        except Exception:
            log.error("ledger unavailable while recording a block")
        rec = self._receipt(action, key, v, "blocked", None) if _signable(action) else None
        return Outcome("blocked", v, receipt=rec, idempotency_key=key)

    # -------------------------------------------------------------- main entry
    def handle(self, action: Action) -> Outcome:
        try:
            return self._handle(action)
        except Exception as e:  # fail closed
            log.exception("engine exception")
            try:
                key = action.key()
            except Exception:
                key = "invalid"
            return Outcome("blocked", Verdict(BLOCK, [f"internal error ({type(e).__name__}); blocked for safety"]), idempotency_key=key)

    def _handle(self, action: Action) -> Outcome:
        if not isinstance(action.tool, str) or not action.tool or not isinstance(action.args, dict):
            return Outcome("blocked", Verdict(BLOCK, ["malformed call: tool must be a string and arguments an object"]))
        try:
            key, sha = action.key(), action.sha()
        except (TypeError, ValueError) as e:
            return Outcome("blocked", Verdict(BLOCK, [f"arguments are not valid JSON data ({type(e).__name__})"]))
        led, now = self.ledger, self.clock()
        known = led.action_sha_for_key(key)
        if action.idempotency_key and known and known != sha:
            return self._blocked(action, key, ["idempotency key was already used for a different action"])
        conn_early = self.connector_for(action.tool)
        is_read = conn_early is not None and self._risk(action.tool, conn_early) == "read"
        prior = None if is_read else led.prior_execution(key)   # reads are never replayed: their answer changes
        if prior is not None:  # a repeat of something already done: replay, never run again
            res = Result(True, prior.get("result_id"), prior.get("output"))
            return Outcome("replayed", Verdict(ALLOW, ["already executed; returning the recorded result"]), res,
                           idempotency_key=key)
        conn = self.connector_for(action.tool)
        if conn is None:
            return self._blocked(action, key, [f"unknown action '{action.tool}': no connector handles it (default deny)"])
        try:
            v = conn.validate(action, self.policy, self._state(action.tool, now), now)
        except Exception as e:
            return self._blocked(action, key, [f"validation failed ({type(e).__name__}: {e}); blocked for safety"])
        led.append(key, action.conversation_id, action.tool, "requested",
                   {"action_sha": sha, "args": action.args, "verdict": v.to_dict(), "mode": self.mode})
        if v.decision == BLOCK:
            return self._blocked(action, key, v.reasons)
        if self.mode == "dry_run":
            r = conn.execute(action, "dry_run")
            led.append(key, action.conversation_id, action.tool, "dry_run",
                       {"action_sha": sha, "verdict": v.to_dict(), "output": r.output})
            return Outcome("dry_run", v, r, idempotency_key=key)
        if v.decision == ALLOW and self.mode == "approval" and self._risk(action.tool, conn) != "read":
            v = Verdict(NEEDS_CONSENT, ["approval mode: every non-read action needs a human"])
        approver = None
        if v.decision == NEEDS_CONSENT:
            if led.consent_state(key) == "denied":
                return self._blocked(action, key, ["a human denied this action"])
            ap = led.approved_for(key, sha)
            if ap is None:
                aid = approval_id(key)
                if led.consent_state(key) != "pending":
                    led.append(key, action.conversation_id, action.tool, "pending",
                               {"approval_id": aid, "action_sha": sha, "args": action.args, "reasons": v.reasons})
                ap = self._wait_for_approval(key, sha)
                if ap is None:
                    if led.consent_state(key) == "denied":
                        return self._blocked(action, key, ["a human denied this action"])
                    return Outcome("needs_consent", v, approval_id=aid, idempotency_key=key)
            approver = ap.get("who")
        if self._risk(action.tool, conn) == "read":
            return self._execute_read(action, key, sha, conn, v)
        return self._execute(action, key, sha, conn, v, approver)

    def _execute_read(self, action: Action, key: str, sha: str, conn: Connector, v: Verdict) -> Outcome:
        try:
            res = conn.execute(action, self.mode)
        except Exception as e:
            res = Result(False, error=f"{type(e).__name__}: {e}"[:500])
        if not res.ok:
            self.ledger.append(key, action.conversation_id, action.tool, "failed", {"action_sha": sha, "error": res.error})
            return Outcome("failed", v, res, idempotency_key=key)
        self.ledger.append(key, action.conversation_id, action.tool, "read",
                           {"action_sha": sha, "result_id": res.result_id, "amount": None})
        return Outcome("executed", v, res, receipt=self._receipt(action, key, v, "executed", res.result_id), idempotency_key=key)

    def _wait_for_approval(self, key: str, sha: str):
        deadline = time.monotonic() + self.consent_wait
        while True:
            ap = self.ledger.approved_for(key, sha)
            if ap or self.ledger.consent_state(key) == "denied" or time.monotonic() >= deadline:
                return ap
            self.sleep(0.2)

    def _execute(self, action: Action, key: str, sha: str, conn: Connector, v: Verdict, approver) -> Outcome:
        led = self.ledger
        status, prior = led.claim(key, action.conversation_id, action.tool)
        if status == "executed":
            return Outcome("replayed", Verdict(ALLOW, ["already executed"]),
                           Result(True, prior.get("result_id"), prior.get("output")), idempotency_key=key)
        if status == "in_flight":
            return Outcome("in_flight", Verdict(BLOCK, ["this action is already running (or a previous run's outcome is unknown); not run again"]),
                           idempotency_key=key)
        try:
            res = conn.execute(action, self.mode)
        except Exception as e:
            res = Result(False, error=f"{type(e).__name__}: {e}"[:500])
        if not res.ok and res.unknown:   # may or may not have happened: leave the key 'claimed' (no automatic retry)
            led.append(key, action.conversation_id, action.tool, "unknown_outcome", {"action_sha": sha, "error": res.error})
            return Outcome("failed", v, res, idempotency_key=key)
        if not res.ok:
            led.append(key, action.conversation_id, action.tool, "failed", {"action_sha": sha, "error": res.error})
            return Outcome("failed", v, res, idempotency_key=key)  # no 'executed' event: the key can be retried
        led.append(key, action.conversation_id, action.tool, "executed",
                   {"action_sha": sha, "result_id": res.result_id, "output": res.output, "amount": self._amount(action),
                    "approved_by": approver})
        rec = self._receipt(action, key, v, "executed", res.result_id, approver)
        return Outcome("executed", v, res, receipt=rec, idempotency_key=key)


def _safe_sha(action: Action) -> str | None:
    try:
        return action.sha()
    except Exception:
        return None


def _signable(action: Action) -> bool:
    return _safe_sha(action) is not None
