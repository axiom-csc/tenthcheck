"""The Connector interface. A connector knows one system (Shopify, Stripe, a SQLite DB, an upstream MCP server).

describe_actions()          what it can do: name, params, risk level
validate(action, policy, ledger_state, now)   -> Verdict   deterministic; never calls an LLM, never changes anything
execute(action, mode)       -> Result          mode: dry_run (simulate, change nothing) | approval | auto
The firewall (engine.py) owns consent, idempotency, the ledger and receipts; a connector only knows its system.
"""
from __future__ import annotations

import abc

from tenthlook.panel.firewall.policy import LedgerState, Policy, evaluate
from tenthlook.panel.firewall.types import Action, ActionSchema, Result, Verdict


class Connector(abc.ABC):
    name: str = "connector"

    @abc.abstractmethod
    def describe_actions(self) -> list[ActionSchema]: ...

    def facts(self, action: Action, policy: Policy) -> dict:
        """Connector-specific values the rule engine needs (row counts, parsed SQL, normalised paths). Override."""
        return {}

    def validate(self, action: Action, policy: Policy, state: LedgerState | None = None, now: float = 0.0) -> Verdict:
        """Connector facts + the generic rule engine. Subclasses may extend; they must stay deterministic."""
        return evaluate(action, policy.rule_for(action.tool), self.facts(action, policy), state or LedgerState(), now)

    @abc.abstractmethod
    def execute(self, action: Action, mode: str) -> Result: ...

    def owns(self, tool: str) -> bool:
        return any(a.name == tool for a in self.describe_actions())
