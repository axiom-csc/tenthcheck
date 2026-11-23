"""Load a store policy (TOML) and represent it in axiom-lab's entity-relation graph.

Each rule becomes an entity `rule:<id>` (type `rule`) with its kind, parameters and plain-English description,
and a `GOVERNS` relation to the claim type it applies to (`claim_type:<type>`). The checker reads rules from the
graph, so what is enforced is exactly what is in the graph.
"""
from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tenthlook.panel.guard._graph import Graph

CLAIM_TYPES = ("refund", "discount", "exchange", "exception", "compensation", "legal_admission", "escalate")
KINDS = {  # kind -> required parameters
    "time_window": ("days", "fact"),
    "max_amount": ("amount",),
    "max_of_fact": ("fact",),
    "escalate_over": ("amount",),
    "max_percent": ("percent",),
    "ineligible_if": ("fact", "equals"),
    "requires_fact": ("fact", "equals"),
    "never": (),
}


class PolicyError(ValueError):
    pass


@dataclass
class Policy:
    store: dict
    graph: Graph
    rule_ids: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.store.get("name", "store")

    @property
    def fallback_reply(self) -> str:
        return self.store["fallback_reply"]

    @property
    def unverified_blocks(self) -> bool:
        return self.store.get("unverified", "block") == "block"

    def rules_for(self, claim_type: str) -> list[dict]:
        out = []
        for rel in self.graph.rels("GOVERNS", dst=f"claim_type:{claim_type}"):
            e = self.graph[rel.src]
            out.append({"id": e.attrs["rule_id"], **{k: v for k, v in e.attrs.items() if k != "rule_id"}})
        return sorted(out, key=lambda r: r["id"])

    def rules_text(self) -> str:
        """Every rule's description, for the bot's system prompt."""
        return "\n".join(f"- {self.graph[r].attrs['description']}" for r in self.rule_ids)


def load_policy(path: str | Path) -> Policy:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    store = data.get("store") or {}
    if "fallback_reply" not in store:
        raise PolicyError("[store] needs a fallback_reply")
    if store.get("unverified", "block") not in ("block", "allow"):
        raise PolicyError("[store] unverified must be \"block\" or \"allow\"")
    g = Graph(meta={"policy": str(path), "store": store.get("name", "")})
    for ct in CLAIM_TYPES:
        g.add(f"claim_type:{ct}", "claim_type", name=ct)
    ids = []
    for i, r in enumerate(data.get("rules", [])):
        where = f"rule #{i + 1} ({r.get('id', 'no id')})"
        for key in ("id", "kind", "applies_to", "description"):
            if key not in r:
                raise PolicyError(f"{where}: missing `{key}`")
        if r["kind"] not in KINDS:
            raise PolicyError(f"{where}: unknown kind {r['kind']!r}; use one of {', '.join(KINDS)}")
        if r["applies_to"] not in CLAIM_TYPES:
            raise PolicyError(f"{where}: applies_to must be one of {', '.join(CLAIM_TYPES)}")
        for p in KINDS[r["kind"]]:
            if p not in r:
                raise PolicyError(f"{where}: kind {r['kind']} needs `{p}`")
        eid = f"rule:{r['id']}"
        if eid in g.entities:
            raise PolicyError(f"{where}: duplicate id")
        attrs = {k: v for k, v in r.items() if k not in ("id",)}
        g.add(eid, "rule", rule_id=r["id"], **attrs)
        g.relate("GOVERNS", eid, f"claim_type:{r['applies_to']}")
        ids.append(eid)
    return Policy(store=store, graph=g, rule_ids=ids)
