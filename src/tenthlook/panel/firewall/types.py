"""Shared value types. Everything the verdict depends on is plain data: no LLM, no network."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ALLOW, NEEDS_CONSENT, BLOCK = "allow", "needs_consent", "block"
MODES = ("dry_run", "approval", "auto")
RISKS = ("read", "write", "destructive")


class FirewallError(Exception):
    """Raised for configuration problems (never for a tool call: a bad call becomes a block verdict)."""


def canonical(obj: Any) -> str:
    """Canonical JSON used for every hash and signature: sorted keys, no spaces, no NaN/Infinity."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(s: str | bytes) -> str:
    return hashlib.sha256(s.encode("utf-8") if isinstance(s, str) else s).hexdigest()


@dataclass(frozen=True)
class ActionSchema:
    name: str                       # e.g. "stripe.refund"
    description: str
    params: dict                    # JSON-schema "properties"-style: {name: {"type": ..., "description": ...}}
    required: tuple = ()
    risk: str = "write"             # read | write | destructive
    claim_verbs: tuple = ()         # words an agent would use to claim this happened ("refund", "credit", ...)

    def input_schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": list(self.required)}


@dataclass
class Action:
    tool: str
    args: dict
    conversation_id: str = "default"
    idempotency_key: str | None = None   # explicit key from the caller, if any
    meta: dict = field(default_factory=dict)   # transport hints (e.g. progressToken); never hashed, never read for consent

    def sha(self) -> str:
        return sha256_hex(canonical({"tool": self.tool, "args": self.args}))

    def key(self) -> str:
        """Explicit idempotency key if given, else sha256(conversation + tool + args)."""
        if self.idempotency_key:
            return "k:" + self.idempotency_key
        return "a:" + sha256_hex(canonical({"c": self.conversation_id, "tool": self.tool, "args": self.args}))[:40]


@dataclass
class Verdict:
    decision: str                        # allow | needs_consent | block
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"decision": self.decision, "reasons": list(self.reasons)}


@dataclass
class Result:
    ok: bool
    result_id: str | None = None         # the system-of-record id (Shopify refund gid, Stripe re_..., sql row count hash)
    output: Any = None                   # what the agent gets back
    error: str = ""
    simulated: bool = False
    unknown: bool = False                # outcome unknown (timeout / upstream died): never auto-retried

    def to_dict(self) -> dict:
        return {"ok": self.ok, "result_id": self.result_id, "output": self.output, "error": self.error,
                "simulated": self.simulated, "unknown": self.unknown}


def merge(*verdicts: Verdict) -> Verdict:
    """block beats needs_consent beats allow. Reasons come from the verdicts that are not plain allows."""
    order = {ALLOW: 0, NEEDS_CONSENT: 1, BLOCK: 2}
    worst = max((v.decision for v in verdicts), key=order.__getitem__, default=ALLOW)
    if worst == ALLOW:
        return Verdict(ALLOW, [r for v in verdicts for r in v.reasons][:1] or ["no rule objected"])
    return Verdict(worst, [r for v in verdicts if v.decision == worst for r in v.reasons])
