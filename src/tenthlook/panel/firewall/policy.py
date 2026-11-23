"""The deployment policy (YAML) and the deterministic rule engine.

`evaluate(action, rule, facts, state, now)` is a PURE function: same inputs, same verdict. No clock reads, no I/O,
no LLM. `facts` are values a connector computed from the action (e.g. SQL statement kinds, row counts, a normalised
path); `state` is a snapshot of the ledger (what has already executed). Anything it cannot interpret is a block.

Rule keys (all optional except where noted), per action name under `actions:`:
  allowed: bool            false -> block (default true once the action is listed; unlisted actions are blocked)
  risk: read|write|destructive   (informational; `approval` mode lets only `read` skip consent)
  required_fields: [..]    missing/None -> block
  amount_field: str        which arg holds the dollar amount (default "amount")
  max_amount: number       amount > max -> block        (amount == max is allowed)
  consent_over_amount: n   amount > n  -> needs_consent (amount == n is allowed without consent)
  max_rows: int            facts.rows > max -> block
  consent_over_rows: int   facts.rows > n  -> needs_consent
  protected: {field: [glob, ..]}   arg value matches a glob -> block (case-insensitive)
  consent_if: [{field, op, value}] op in eq, ne, gt, gte, lt, lte, in, contains, matches -> needs_consent
  block_if:   [{field, op, value}] same operators -> block
  rate_limit: {max: n, per_seconds: s}        n executions already in the window -> block
  max_total_amount: {amount: n, per_seconds: s}   executed total + this amount > n -> block
  sql: {...}               SQL guard settings (see sqlguard.py)
"""
from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tenthlook.panel.firewall.types import ALLOW, BLOCK, FirewallError, NEEDS_CONSENT, MODES, Action, Verdict, sha256_hex

OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "matches")
RULE_KEYS = {"allowed", "risk", "required_fields", "amount_field", "max_amount", "consent_over_amount", "max_rows",
             "consent_over_rows", "protected", "consent_if", "block_if", "rate_limit", "max_total_amount", "sql",
             "path_args", "allowed_roots", "sql_arg", "note"}
TOP_KEYS = {"version", "name", "mode", "reviewed", "receipts", "actions", "sql", "consent_wait_seconds", "database",
            "session", "ledger", "connectors"}


@dataclass
class Policy:
    version: str
    mode: str
    actions: dict
    raw: dict
    sha256: str
    path: str = ""

    def rule_for(self, tool: str) -> dict | None:
        return self.actions.get(tool)


def load_policy(path: str | Path) -> Policy:
    p = Path(path)
    text = p.read_bytes()
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise FirewallError(f"policy {p}: not valid YAML: {e}") from e
    pol = parse_policy(raw, sha256_hex(text))
    pol.path = str(p)
    return pol


def parse_policy(raw: dict, sha: str = "") -> Policy:
    if not isinstance(raw, dict):
        raise FirewallError("policy must be a mapping")
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise FirewallError(f"policy: unknown top-level keys {sorted(unknown)}")
    mode = raw.get("mode", "approval")
    if mode not in MODES:
        raise FirewallError(f"policy: mode must be one of {MODES}")
    actions = raw.get("actions")
    if not isinstance(actions, dict) or not actions:
        raise FirewallError("policy: `actions` must be a non-empty mapping")
    for name, rule in actions.items():
        if not isinstance(rule, dict):
            raise FirewallError(f"policy: actions.{name} must be a mapping")
        bad = set(rule) - RULE_KEYS
        if bad:  # a typo like max_ammount must not silently disable a cap
            raise FirewallError(f"policy: actions.{name}: unknown keys {sorted(bad)}")
        for k in ("max_amount", "consent_over_amount", "max_rows", "consent_over_rows"):
            if k in rule and (isinstance(rule[k], bool) or not isinstance(rule[k], (int, float))
                              or not math.isfinite(rule[k]) or rule[k] < 0):
                raise FirewallError(f"policy: actions.{name}.{k} must be a non-negative number")
        for k in ("consent_if", "block_if"):
            for c in rule.get(k, []):
                if not isinstance(c, dict) or c.get("op") not in OPS or "field" not in c:
                    raise FirewallError(f"policy: actions.{name}.{k}: each item needs field, op in {OPS}, value")
        for k in ("rate_limit", "max_total_amount"):
            if k in rule and not (isinstance(rule[k], dict) and rule[k].get("per_seconds", 0) > 0):
                raise FirewallError(f"policy: actions.{name}.{k} needs a positive per_seconds")
    return Policy(str(raw.get("version", "0")), mode, actions, raw, sha)


@dataclass
class LedgerState:
    """Snapshot of executed actions for one tool: [(timestamp, amount_or_None)]. Built by the caller from the ledger."""
    executed: list = field(default_factory=list)


def window_needed(rule: dict) -> float:
    return max(float((rule.get("rate_limit") or {}).get("per_seconds", 0)),
               float((rule.get("max_total_amount") or {}).get("per_seconds", 0)))


def _number(v):
    """A real finite non-negative number, else None. Strings, bools, NaN/inf, negatives are rejected."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return None
    return float(v)


def _cond(c: dict, args: dict) -> bool | None:
    """True/False; None if the field is present but the wrong type for the operator. An ABSENT field means the
    condition does not apply (use required_fields to demand a field)."""
    if c["field"] not in args:
        return False
    a, b, op = args[c["field"]], c.get("value"), c["op"]
    try:
        if op == "eq":
            return a == b
        if op == "ne":
            return a != b
        if op in ("gt", "gte", "lt", "lte"):
            if isinstance(a, bool) or not isinstance(a, (int, float)):
                return None
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        if op == "in":
            return a in b
        if op == "contains":
            return b in a
        if op == "matches":
            return re.search(b, str(a)) is not None
    except (TypeError, re.error):
        return None
    return None


def evaluate(action: Action, rule: dict | None, facts: dict, state: LedgerState, now: float) -> Verdict:
    """The decision. Pure. `rule` None means the action is not in the policy -> block."""
    if rule is None:
        return Verdict(BLOCK, [f"unknown action '{action.tool}': not listed in the policy (default deny)"])
    if not isinstance(action.args, dict):
        return Verdict(BLOCK, ["arguments must be a JSON object"])
    blocks: list[str] = []
    consent: list[str] = []
    args = action.args
    if rule.get("allowed", True) is not True:
        blocks.append(f"'{action.tool}' is not allowed by policy")
    for f in rule.get("required_fields", []):
        if args.get(f) is None:
            blocks.append(f"missing required field '{f}'")
    amount = None
    needs_amount = any(k in rule for k in ("max_amount", "consent_over_amount", "max_total_amount"))
    if needs_amount:
        af = rule.get("amount_field", "amount")
        if af not in args or args[af] is None:
            blocks.append(f"missing amount field '{af}'")
        else:
            amount = _number(args[af])
            if amount is None:
                blocks.append(f"'{af}' must be a finite non-negative number (got {args[af]!r})")
    if amount is not None:
        if "max_amount" in rule and amount > rule["max_amount"]:
            blocks.append(f"amount {amount:g} exceeds the maximum {rule['max_amount']:g}")
        elif "consent_over_amount" in rule and amount > rule["consent_over_amount"]:
            consent.append(f"amount {amount:g} is over {rule['consent_over_amount']:g}; a human must approve")
    if "max_rows" in rule or "consent_over_rows" in rule:
        rows = facts.get("rows")
        if rows is None:
            blocks.append("row count could not be determined")
        else:
            if "max_rows" in rule and rows > rule["max_rows"]:
                blocks.append(f"would affect {rows} rows; the maximum is {rule['max_rows']}")
            elif "consent_over_rows" in rule and rows > rule["consent_over_rows"]:
                consent.append(f"would affect {rows} rows; over {rule['consent_over_rows']} needs approval")
    for fld, globs in (rule.get("protected") or {}).items():
        if fld in args and args[fld] is not None:
            val = str(args[fld]).lower()
            for g in globs:
                if fnmatch.fnmatchcase(val, str(g).lower()):
                    blocks.append(f"'{fld}'={args[fld]!r} matches protected resource '{g}'")
    for key, bucket, label in (("block_if", blocks, "blocked"), ("consent_if", consent, "needs approval")):
        for c in rule.get(key, []):
            r = _cond(c, args)
            if r is None:
                if key == "block_if":
                    blocks.append(f"cannot evaluate condition on '{c['field']}' (wrong type)")
                else:
                    consent.append(f"cannot evaluate condition on '{c['field']}'; asking a human")
            elif r:
                bucket.append(f"{label}: {c['field']} {c['op']} {c.get('value')!r}")
    rl = rule.get("rate_limit")
    if rl:
        n = sum(1 for ts, _ in state.executed if ts > now - rl["per_seconds"])
        if n >= rl["max"]:
            blocks.append(f"rate limit: {n} already executed in the last {rl['per_seconds']:g}s (max {rl['max']})")
    mt = rule.get("max_total_amount")
    if mt and amount is not None:
        total = sum(a or 0 for ts, a in state.executed if ts > now - mt["per_seconds"])
        if total + amount > mt["amount"]:
            blocks.append(f"cumulative {total + amount:g} in {mt['per_seconds']:g}s would exceed {mt['amount']:g}")
    blocks += facts.get("block_reasons", [])
    consent += facts.get("consent_reasons", [])
    if blocks:
        return Verdict(BLOCK, blocks)
    if consent:
        return Verdict(NEEDS_CONSENT, consent)
    return Verdict(ALLOW, ["within policy"])
